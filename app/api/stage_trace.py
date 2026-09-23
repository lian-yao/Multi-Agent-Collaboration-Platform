"""阶段执行轨迹：把状态存储里的阶段状态还原成弹窗可渲染的形态（`doc/api.md` §5.17）。

数据来源是编排层在**每个阶段（或计划步骤）完成后**写入的状态：

- 静态链路：`app.workflows.pipeline::_record_checkpoint` 写
  `agentrun:workflow:{workflow_id}:{stage}`，其 `results[stage]` 即该阶段的执行载荷
  （`content` 产出、`previous` 上游正文、`tool_calls` 工具调用）；
- 动态链路：`app.workflows.dynamic::_persist_step_outcome` 写
  `agentrun:workflow:{workflow_id}:dyn:{step_id}`，载荷即 ``StepOutcome``
  （`content` / `tool_calls` / `status`），步骤顺序与角色取自 ``checkpoint.plan``。

**本模块只读**：不写状态存储、不建表、不改任何 Workflow 数据。读不到就如实说读不到——
「没有轨迹」有互不相同的原因（还没轮到、正在跑、跑完了但状态已被清理、计划尚未落盘），
把它们混成一句「暂无数据」等于让用户去猜自己的任务执行到哪一步了。

模型内部的隐藏推理（reasoning / thinking 块）**不在本接口的范围内**：编排层只把
「推理 → 行动 → 观察 → 结论」里的行动与结论落盘，本模块不伪造中间过程。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.orchestration.pipeline import (
    PIPELINE_STEPS,
    PipelineStage,
    deserialize_pipeline_state,
)
from app.orchestration.pipeline_graph import role_for_stage
from app.workflows.state import read_step_result

STAGE_TRACE_MAX_OUTPUT_CHARS = 8_000
"""单段正文（上游输入 / 本阶段产出）的字符上限。

上限不是「省钱」而是「别把一次弹窗变成几 MB 的响应」：报告阶段动辄数千字，
一旦模型跑飞可能上万字，而弹窗只需要读到人能判断的量。
"""

STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS = 4_000
"""单次工具调用入参或出参的序列化长度上限。

读文件类工具（`read_session_file`）的出参就是文件正文，不设上限等于把附件内容
经由这条只读接口再传一遍。
"""

ReadStep = Callable[[str, str], dict[str, Any] | None]
"""``(workflow_id, stage) -> 状态载荷`` 的读取函数签名；测试可注入替身，不连 Dapr。"""


def _read_dynamic_traces(
    workflow_id: str,
    summary: dict[str, Any],
    reader: ReadStep,
) -> dict[str, Any]:
    """动态链路的逐步骤轨迹（`doc/api.md` §5.17 的 `mode=dynamic` 分支）。

    步骤顺序与角色来自 ``checkpoint.plan`` 的声明顺序（不是静态 ``PIPELINE_STEPS``）；
    每个步骤的载荷读状态存储 key ``dyn:{step_id}``（``_persist_step_outcome`` 写入的
    ``StepOutcome``）。上游输入按 ``depends_on`` 从已完成步骤的 ``content`` 拼出。

    与静态链路唯一的分歧在「顺序的权威来源」：静态靠固定阶段名，动态靠计划声明顺序——
    其余（input / output / tool_calls / reason 语义）完全对齐，前端无需区分两套规则。
    """

    plan = summary.get("plan")
    if not isinstance(plan, list) or not plan:
        # 计划尚未落盘（规划活动还没跑到）：如实说「还没规划」，而不是伪装成空轨迹。
        return {
            "workflow_id": workflow_id,
            "mode": "dynamic",
            "task": None,
            "availability": "not_integrated",
            "reason": "本次执行还在规划阶段，计划尚未落盘。",
            "items": [],
        }

    completed = {str(step) for step in (summary.get("completed_steps") or [])}
    current = summary.get("current_step")

    # 先读一遍所有步骤载荷，产出「步骤 id → content」映射，供拼上游输入。
    outcomes: dict[str, dict[str, Any]] = {}
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or "")
        if not step_id:
            continue
        payload = reader(workflow_id, f"dyn:{step_id}")
        outcome = _load_outcome(payload)
        if outcome is not None:
            outcomes[step_id] = outcome

    items: list[dict[str, Any]] = []
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or "")
        if not step_id:
            continue
        role = str(raw.get("role") or "")
        depends_on = [str(dep) for dep in (raw.get("depends_on") or [])]
        outcome = outcomes.get(step_id)

        item: dict[str, Any] = {
            "stage": step_id,
            "role": role,
            "input": None,
            "input_from": None,
            "output": None,
            "tool_calls": [],
            "truncated": False,
            "reason": None,
        }

        if outcome is None:
            # 载荷还没写：四种原因（已跳过 / 正在跑 / 已完成但状态被清 / 还没轮到）。
            status = str(raw.get("status") or "")
            if status == "skipped":
                item["reason"] = "上游步骤未成功完成，本步骤已跳过。"
            elif status in ("completed", "failed"):
                item["reason"] = "该步骤已完成，但它的执行状态已不在状态存储中（状态可能已被清理）。"
            elif current == step_id or status == "running":
                item["reason"] = (
                    "该步骤正在执行：轨迹在步骤完成后写入状态存储，完成后再打开即可看到；"
                    "当下想跟进工具调用可以走「任务记录」页。"
                )
            else:
                item["reason"] = "该步骤尚未开始。"
            items.append(item)
            continue

        # 上游输入：按 depends_on 从已完成步骤的 content 拼出（多依赖用分节标注）。
        upstream = [
            (dep, outcomes[dep].get("content") or "")
            for dep in depends_on
            if isinstance(outcomes.get(dep, {}).get("content"), str)
        ]
        if upstream:
            joined = "\n\n".join(f"【{dep}】\n{content}" for dep, content in upstream)
            item["input"], cut = _clip_text(joined, STAGE_TRACE_MAX_OUTPUT_CHARS)
            item["truncated"] = cut
            item["input_from"] = ", ".join(dep for dep, _ in upstream)

        content = outcome.get("content")
        if isinstance(content, str):
            item["output"], cut = _clip_text(content, STAGE_TRACE_MAX_OUTPUT_CHARS)
            item["truncated"] = item["truncated"] or cut

        item["tool_calls"], cut = _tool_calls(outcome)
        item["truncated"] = item["truncated"] or cut
        items.append(item)

    return {
        "workflow_id": workflow_id,
        "mode": "dynamic",
        "task": None,
        "availability": "available",
        "reason": None,
        "items": items,
    }


def _load_outcome(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """把动态步骤载荷（``save_step_result`` 写入的 ``StepOutcome``）解析成 dict。

    ``save_step_result`` 包了一层 ``{"workflow_id", "step", "result": ...}``；``result``
    就是 ``StepOutcome.model_dump()``（字段：content / tool_calls / status / error）。
    解析失败返回 ``None``，由调用方按「还没写」处理。
    """

    if payload is None:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    if "content" not in result and "tool_calls" not in result:
        return None
    return result


def _clip_text(text: str, limit: int) -> tuple[str, bool]:
    """超长截断；返回 `(文本, 是否被截断)`，绝不静默截断。"""

    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _clip_payload(value: Any) -> tuple[Any, bool]:
    """工具入参 / 出参按序列化长度设上限，超限换成带预览的说明对象。

    返回结构刻意保留「JSON 值」的形状（而不是直接给字符串）：调用方拿到的要么是
    原值，要么是一个写明 `truncated` / `bytes` / `preview` 的对象，不需要靠类型猜。
    """

    if value is None:
        return None, False
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return value, False
    if len(encoded) <= STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS:
        return value, False
    return (
        {
            "truncated": True,
            "bytes": len(encoded),
            "preview": encoded[:STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS],
        },
        True,
    )


def _tool_calls(result: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """归一化阶段载荷里的工具调用记录；返回 `(调用列表, 是否有截断)`。"""

    records = result.get("tool_calls")
    if not isinstance(records, list):
        return [], False

    items: list[dict[str, Any]] = []
    clipped = False
    for record in records:
        if not isinstance(record, dict):
            continue
        tool_input, trimmed_input = _clip_payload(record.get("input") or {})
        output, trimmed_output = _clip_payload(record.get("output"))
        clipped = clipped or trimmed_input or trimmed_output
        items.append(
            {
                "call_id": str(record.get("call_id") or ""),
                "tool_name": str(record.get("tool_name") or ""),
                "status": str(record.get("status") or "running"),
                "input": tool_input,
                "output": output,
                "error": record.get("error"),
            }
        )
    return items, clipped


def _missing_reason(stage: str, completed: list[str], current: str | None) -> str:
    """没有轨迹时给**具体**原因——四件事的下一步动作完全不同。"""

    if stage in completed:
        return "该阶段已完成，但它的执行状态已不在状态存储中（状态可能已被清理）。"
    if current == stage:
        return (
            "该阶段正在执行：轨迹在阶段完成后写入状态存储，阶段结束再打开这里即可看到；"
            "当下想跟进工具调用可以走「任务记录」页。"
        )
    return "该阶段尚未开始。"


def _load_state(payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """把状态载荷解析成 dict；返回 `(状态, 失败原因)`，两者恰有一个非空。"""

    if payload is None:
        return None, None
    raw = payload.get("result")
    if raw is None:
        return None, "状态存储里的该阶段载荷缺少结果字段。"
    if isinstance(raw, (str, bytes, dict)):
        try:
            state = deserialize_pipeline_state(raw)
        except Exception as exc:  # noqa: BLE001 - 载荷损坏的形态很多，统一降级为「解析不了」
            return None, f"状态存储里的该阶段载荷无法解析（{type(exc).__name__}）。"
        return state.model_dump(mode="json"), None
    return None, "状态存储里的该阶段载荷格式不受支持。"


def _stage_item(
    stage: PipelineStage,
    state: dict[str, Any] | None,
    *,
    parse_error: str | None,
    completed: list[str],
    current: str | None,
) -> dict[str, Any]:
    """组装单个阶段的轨迹条目（缺什么就写明缺什么，不留空字段让人猜）。"""

    item: dict[str, Any] = {
        "stage": stage.value,
        "role": role_for_stage(stage).value,
        "input": None,
        "input_from": None,
        "output": None,
        "tool_calls": [],
        "truncated": False,
        "reason": None,
    }

    if state is None:
        item["reason"] = parse_error or _missing_reason(stage.value, completed, current)
        return item

    result = (state.get("results") or {}).get(stage.value)
    if not isinstance(result, dict):
        item["reason"] = f"状态存储里的 {stage.value} 阶段状态缺少该阶段的结果。"
        return item

    # `previous` 是上游阶段的完整结果载荷：`content` 即本阶段实际读到的输入正文，
    # `step` 告诉我们它来自哪一步。根阶段没有上游，`input` 保持 null（那就是原始任务）。
    previous = result.get("previous")
    if isinstance(previous, dict) and isinstance(previous.get("content"), str):
        item["input"], cut = _clip_text(previous["content"], STAGE_TRACE_MAX_OUTPUT_CHARS)
        item["truncated"] = item["truncated"] or cut
        item["input_from"] = str(previous.get("step") or "") or None

    content = result.get("content")
    if isinstance(content, str):
        item["output"], cut = _clip_text(content, STAGE_TRACE_MAX_OUTPUT_CHARS)
        item["truncated"] = item["truncated"] or cut

    item["tool_calls"], cut = _tool_calls(result)
    item["truncated"] = item["truncated"] or cut
    return item


def read_stage_traces(
    workflow_id: str,
    *,
    checkpoint: dict[str, Any] | None = None,
    read_step: ReadStep | None = None,
) -> dict[str, Any]:
    """读取一次 Workflow 的逐阶段执行轨迹（`doc/api.md` §5.17）。

    ``checkpoint`` 传 `workflow_runs.checkpoint` 摘要：`mode` 决定链路形态，
    `completed_steps` / `current_step` 决定「没有轨迹」的原因措辞。

    ``read_step`` 可注入：单测据此覆盖各条分支而不需要 Dapr sidecar。

    状态存储不可用时**不在这里吞异常**——由调用方归一化成 `DATA_SOURCE_UNAVAILABLE`，
    否则「读不到」会被伪装成「这个阶段没有轨迹」。
    """

    summary = checkpoint or {}
    reader: ReadStep = read_step or read_step_result
    if summary.get("mode") == "dynamic":
        return _read_dynamic_traces(workflow_id, summary, reader)

    completed = [str(step) for step in (summary.get("completed_steps") or [])]
    current = summary.get("current_step")
    current_stage = str(current) if current else None

    task: str | None = None
    items: list[dict[str, Any]] = []
    for stage in PIPELINE_STEPS:
        payload = reader(workflow_id, stage.value)
        state, parse_error = _load_state(payload)
        if state is not None and task is None:
            raw_task = state.get("task")
            if isinstance(raw_task, str) and raw_task:
                task = raw_task
        items.append(
            _stage_item(
                stage,
                state,
                parse_error=parse_error,
                completed=completed,
                current=current_stage,
            )
        )

    return {
        "workflow_id": workflow_id,
        "mode": "static",
        "task": task,
        "availability": "available",
        "reason": None,
        "items": items,
    }

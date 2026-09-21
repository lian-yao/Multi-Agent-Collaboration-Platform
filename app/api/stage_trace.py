"""阶段执行轨迹：把状态存储里的阶段状态还原成弹窗可渲染的形态（`doc/api.md` §5.17）。

数据来源是 `app.workflows.pipeline::_record_checkpoint` 在**每个阶段完成后**写入的
Workflow 状态（key `agentrun:workflow:{workflow_id}:{stage}`）。那份状态里的
`results[stage]` 就是该阶段的执行载荷：`content` 是产出正文、`previous` 是本阶段收到的
上游正文、`tool_calls` 是本阶段内产生的工具调用（结构见
`app.orchestration.pipeline_graph::_stage_result`）。

**本模块只读**：不写状态存储、不建表、不改任何 Workflow 数据。读不到就如实说读不到——
「没有轨迹」有四种互不相同的原因（还没轮到、正在跑、跑完了但状态已被清理、动态链路
根本不落盘），把它们混成一句「暂无数据」等于让用户去猜自己的任务执行到哪一步了。

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

DYNAMIC_TRACE_REASON = (
    "本次执行走的是动态编排链路，它当前不落盘逐步骤执行轨迹（ADR-019）；"
    "可执行到的替代信息是各步骤的阶段状态与「任务记录」里的工具调用链路。"
)

ReadStep = Callable[[str, str], dict[str, Any] | None]
"""``(workflow_id, stage) -> 状态载荷`` 的读取函数签名；测试可注入替身，不连 Dapr。"""


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
    if summary.get("mode") == "dynamic":
        return {
            "workflow_id": workflow_id,
            "mode": "dynamic",
            "task": None,
            "availability": "not_integrated",
            "reason": DYNAMIC_TRACE_REASON,
            "items": [],
        }

    reader: ReadStep = read_step or read_step_result
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

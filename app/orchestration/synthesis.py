"""合成器与校验器（ADR-038 §4/§5）。

两个节点都是**平台级步骤**，但用现有角色与平台模型配置落地，不新增协作角色：

- **合成器**复用 `reporter` 角色（它本来就是「整合上游结果、产出面向使用者的交付物」），
  只在 system prompt 后面追加一段合成职责：多源合并、冲突消解、去重、按用户格式成稿，
  **以及失败子任务的显式告知**；
- **校验器**复用平台节点（`planner`）的模型配置——它不产出交付物，只回答
  「这份输出满足原始意图吗」，因此没有角色，只有一次结构化判定。

两个节点的失败口径与 ADR-019 §2 一致：**降级而不是抛错**。
合成失败（模型不可用）会让这一轮没有交付物，交由上层决定终态；校验失败则视为
「通过」（`source=fallback`）——校验是加分项，不该因为校验器自己坏了就推翻一份
本来可用的报告，更不该因此触发重编排循环。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.roles import RoleId, get_role
from app.attachments import AttachmentPayload, build_human_content
from app.config import AgentSettings
from app.memory import SessionMessage
from app.observability.instrumentation import observed_stage
from app.observability.logging import get_logger, log_event
from app.orchestration.context import conversation_block
from app.orchestration.intake import IntentResult, intent_block
from app.orchestration.llm import build_chat_model
from app.orchestration.pipeline_graph import content_with_tools, invoke_role_messages
from app.orchestration.rewrite import resolve_platform_settings
from app.orchestration.tools import ToolCaller, ToolCallRecord, ToolRegistry

SYNTHESIZE_NODE_ID = "synthesize"
"""合成器在流程序列里的节点 id。"""

VALIDATE_NODE_ID = "validate"
"""校验器在流程序列里的节点 id。"""

VALIDATION_SOURCE_MODEL = "model"
VALIDATION_SOURCE_FALLBACK = "fallback"

logger = get_logger("orchestration.synthesis")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ValidationResult(BaseModel):
    """校验器输出：这份交付物满足原始意图吗？"""

    satisfied: bool = True
    defects: list[str] = Field(default_factory=list)
    """不满足时**具体**的问题清单；重编排会把它原样交给编排器，含糊等于没提。"""

    missing: list[str] = Field(default_factory=list)
    """原始意图里有、但交付物没覆盖到的部分。"""

    source: str = VALIDATION_SOURCE_MODEL
    """`model` / `fallback`；`fallback` 表示校验器不可用，按「通过」处理。"""


class SynthesisOutcome(BaseModel):
    """合成结果。形状与 `StepOutcome` 接近，便于落盘与展示复用。"""

    status: str = "completed"
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0


SYNTHESIS_ADDENDUM = """

---

【本次是「合成器」节点】上游可能是**多个并行子任务**的产出，而不是单一路径的分析结论：
- 多条结论冲突时，指出冲突、给出取舍与依据，不要两边都抄一遍算完事；
- 重复信息合并一次，保留来源（哪个子任务/哪份文件/哪条链接）；
- 严格按使用者的约束（格式、语言、篇幅、交付物形态）成稿；
- **如果有子任务失败或被跳过，必须在正文开头用一段明确说明**：哪些子任务没跑成、
  原因是什么、因此哪些内容是缺失的或只能给部分结论。不要把失败悄悄吞掉，
  更不要把「部分结论」写成「完整结论」；
- 只输出交付物本身，不要写「我将……」这类过程叙述。\
"""


def synthesis_system_prompt() -> str:
    """合成器的 system prompt：reporter 角色 + 合成职责。"""

    return f"{get_role(RoleId.REPORTER).system_prompt}{SYNTHESIS_ADDENDUM}"


def _failure_lines(
    failed: Sequence[tuple[str, str, str]],
    skipped: Sequence[tuple[str, str, str]],
) -> str:
    """把失败/跳过的子任务渲染成一段清单；没有失败时返回空串。"""

    lines: list[str] = []
    for step_id, role, error in failed:
        lines.append(f"- {step_id}（{role}）：失败 —— {error or '未知错误'}")
    for step_id, role, error in skipped:
        lines.append(f"- {step_id}（{role}）：未执行 —— {error or '上游未成功'}")
    if not lines:
        return ""
    return "【失败与未执行的子任务】\n" + "\n".join(lines) + "\n"


def synthesis_input(
    task: str,
    results: Mapping[str, str],
    *,
    intent: IntentResult | None = None,
    failed: Sequence[tuple[str, str, str]] = (),
    skipped: Sequence[tuple[str, str, str]] = (),
    preferences: str = "",
    history: Sequence[SessionMessage] = (),
) -> str:
    """构造合成器的用户输入：约束 → 上下文 → 任务 → 各子任务产出 → 失败清单。

    ``results`` 是「子任务 id → 产出正文」的有序映射（调用方按计划顺序给）。
    """

    parts = [f"{preferences}{conversation_block(history)}{intent_block(intent)}"]
    parts.append(f"【用户任务】\n{task}")
    if results:
        body = "\n\n".join(
            f"【子任务 {step_id}】\n{content}" for step_id, content in results.items()
        )
        parts.append(f"【各子任务产出】\n\n{body}")
    else:
        parts.append("【各子任务产出】\n（本次没有任何子任务产出）")
    failures = _failure_lines(failed, skipped)
    if failures:
        parts.append(failures)
    parts.append("请按上面的要求合成面向使用者的最终交付物。")
    return "\n\n".join(part for part in parts if part)


def run_synthesis(
    task: str,
    results: Mapping[str, str],
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    intent: IntentResult | None = None,
    failed: Sequence[tuple[str, str, str]] = (),
    skipped: Sequence[tuple[str, str, str]] = (),
    caller: ToolCaller | None = None,
    registry: ToolRegistry | None = None,
    workflow_id: str | None = None,
    stage: str = SYNTHESIZE_NODE_ID,
    attachments: Sequence[AttachmentPayload] = (),
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
) -> SynthesisOutcome:
    """执行合成节点；异常收敛为 `failed` 结果，不向上抛。"""

    prompt = synthesis_input(
        task,
        results,
        intent=intent,
        failed=failed,
        skipped=skipped,
        preferences=preferences,
        history=history,
    )
    content = build_human_content(prompt, attachments)
    messages = [
        SystemMessage(content=synthesis_system_prompt()),
        HumanMessage(content=content),
    ]
    call = caller
    if call is None and registry is not None:
        call = ToolCaller(registry, scope=workflow_id, stage=stage)
    started = time.perf_counter()
    log_event(
        logger,
        "dynamic.synthesize.start",
        workflow_id=workflow_id,
        subtasks=len(results),
        failed=len(failed),
        skipped=len(skipped),
    )
    try:
        model = llm or build_chat_model(settings or resolve_platform_settings())
        with observed_stage(workflow_id=workflow_id, stage=stage, role=RoleId.REPORTER.value):
            text = invoke_role_messages(
                messages, model, call, stage=stage, role=RoleId.REPORTER
            )
    except Exception as exc:
        log_event(
            logger,
            "dynamic.synthesize.failed",
            level=logging.ERROR,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return SynthesisOutcome(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    records: Sequence[ToolCallRecord] = call.records if call is not None else ()
    outcome = SynthesisOutcome(
        status="completed",
        content=text,
        tool_calls=[record.model_dump(mode="json") for record in records],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    log_event(
        logger,
        "dynamic.synthesize.finish",
        workflow_id=workflow_id,
        chars=len(text),
        tool_calls=len(outcome.tool_calls),
        duration_ms=outcome.duration_ms,
    )
    return outcome


def validation_prompt() -> str:
    """校验器的 system prompt。"""

    return (
        "你是多智能体协作平台的「结果校验 Agent」。你**不写交付物**，只判定"
        "「这份交付物是否满足了用户的原始意图」，并把不满足的地方逐条说清楚。\n\n"
        "判定依据只有两份输入：结构化意图（目标 + 约束）与交付物正文。"
        "不要引入你自己的额外标准，不要因为「还能更好」就判不满足——"
        "只有**用户明确提出过**的目标或约束没被满足，才算不满足。\n\n"
        "输出要求（务必严格遵守）：\n"
        "- 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性文字；\n"
        '- 结构：{"satisfied": true/false, "defects": ["问题"], "missing": ["缺失"]}；\n'
        "- `defects` 写「哪里不满足、应该怎么补」，要具体到能照着改，不要写「不够好」这种话；\n"
        "- 满足时 `defects` 与 `missing` 都给空数组。"
    )


def validation_input(
    task: str,
    deliverable: str,
    *,
    intent: IntentResult | None = None,
    failed: Sequence[tuple[str, str, str]] = (),
) -> str:
    """构造校验器的用户输入。"""

    parts = [f"{intent_block(intent)}【用户任务】\n{task}"]
    if failed:
        lines = "\n".join(
            f"- {step_id}（{role}）：{error or '失败'}" for step_id, role, error in failed
        )
        parts.append(
            "【已知失败的子任务】（交付物里应已明确说明这些缺失，说了就算满足）\n" + lines
        )
    parts.append(f"【待校验的交付物】\n{deliverable}")
    return "\n\n".join(parts)


def parse_validation(text: str) -> ValidationResult | None:
    """解析校验结果；任一处不合法返回 ``None``（上层按「通过」处理）。"""

    candidate = (text or "").strip()
    if not candidate:
        return None
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    payload: Any = None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None

    flag = payload.get("satisfied")
    if not isinstance(flag, bool):
        return None
    defects_raw = payload.get("defects") or []
    missing_raw = payload.get("missing") or []
    if not isinstance(defects_raw, list) or not isinstance(missing_raw, list):
        return None
    return ValidationResult(
        satisfied=flag,
        defects=[str(item).strip() for item in defects_raw if str(item).strip()],
        missing=[str(item).strip() for item in missing_raw if str(item).strip()],
        source=VALIDATION_SOURCE_MODEL,
    )


def run_validation(
    task: str,
    deliverable: str,
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    intent: IntentResult | None = None,
    failed: Sequence[tuple[str, str, str]] = (),
    workflow_id: str | None = None,
    stage: str = VALIDATE_NODE_ID,
) -> ValidationResult:
    """执行校验节点；任何失败都退化为「通过」（`source=fallback`），不触发重编排。"""

    prompt = validation_input(task, deliverable, intent=intent, failed=failed)
    messages = [
        SystemMessage(content=validation_prompt()),
        HumanMessage(content=prompt),
    ]
    started = time.perf_counter()
    log_event(
        logger,
        "dynamic.validate.start",
        workflow_id=workflow_id,
        deliverable_chars=len(deliverable or ""),
    )
    try:
        model = llm or build_chat_model(settings or resolve_platform_settings())
        with observed_stage(workflow_id=workflow_id, stage=stage, role="validator"):
            response = model.invoke(messages)
        text = content_with_tools(response.content)
    except Exception as exc:
        log_event(
            logger,
            "dynamic.validate.failed",
            level=logging.WARNING,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return ValidationResult(satisfied=True, source=VALIDATION_SOURCE_FALLBACK)

    result = parse_validation(text)
    if result is None:
        log_event(
            logger,
            "dynamic.validate.unusable",
            level=logging.WARNING,
            workflow_id=workflow_id,
            chars=len(text or ""),
        )
        return ValidationResult(satisfied=True, source=VALIDATION_SOURCE_FALLBACK)
    log_event(
        logger,
        "dynamic.validate.finish",
        workflow_id=workflow_id,
        satisfied=result.satisfied,
        defects=len(result.defects),
        missing=len(result.missing),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return result


__all__ = [
    "SYNTHESIS_ADDENDUM",
    "SYNTHESIZE_NODE_ID",
    "VALIDATE_NODE_ID",
    "VALIDATION_SOURCE_FALLBACK",
    "VALIDATION_SOURCE_MODEL",
    "SynthesisOutcome",
    "ValidationResult",
    "parse_validation",
    "run_synthesis",
    "run_validation",
    "synthesis_input",
    "synthesis_system_prompt",
    "validation_input",
    "validation_prompt",
]

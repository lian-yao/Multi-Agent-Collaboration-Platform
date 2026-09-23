"""intake 节点：问题改写（ADR-037）+ 意图识别，合并成一次平台模型调用（ADR-038）。

为什么合并：改写与意图识别都是**平台节点**（不对应使用者可见的协作角色），
输入完全一样（长期偏好 + 会话历史 + 本轮附件名 + 本轮原话），位置也一样（执行最前面）。
分两次调用等于把同一份上下文喂给模型两遍，换来的只是「可以分别降级」；而这件事
**分段降级**就能做到：

- 文本拿不到（调用失败 / 空 / 跑偏成长文）→ 退回原文，`source=original`；
- 意图拿不到（不是合法 JSON / 字段不合法）→ `intent=None`，`intent_source=fallback`；
- 两者都拿不到**不是失败**：退回原文 + 按多 Agent 处理，是默认可用路径
  （与 ADR-019 §2 的降级口径一致）。

旧口径（只输出任务正文、没有 JSON）同样容忍：文本照用，意图按「需要多 Agent」处理——
这样升级后遇到「不吐 JSON」的模型，行为与 ADR-037 时期一致，不会比原来更差。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import AgentSettings
from app.memory import SessionMessage
from app.observability.instrumentation import observed_stage
from app.observability.logging import get_logger, log_event
from app.orchestration.llm import build_chat_model
from app.orchestration.pipeline_graph import content_with_tools, usage_tokens
from app.orchestration.rewrite import (
    MAX_REWRITE_CHARS,
    REWRITE_SOURCE_MODEL,
    REWRITE_SOURCE_ORIGINAL,
    resolve_platform_settings,
    rewrite_prompt_input,
)

INTENT_SOURCE_MODEL = "model"
"""意图来自模型输出的合法 JSON。"""

INTENT_SOURCE_FALLBACK = "fallback"
"""意图不可用（缺字段 / 不合法 / 没吐 JSON），下游按「需要多 Agent」处理。"""

DEFAULT_INTENT_TYPE = "general"
"""`intent_type` 缺失时的兜底分类名；它只用于展示与日志，不参与路由判定。"""

logger = get_logger("orchestration.intake")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class IntentResult(BaseModel):
    """结构化意图（ADR-038 §1）。

    字段与需求原文一致：`intent_type` / `user_goal` / `constraints` / `need_multi_subtask`。
    `need_multi_subtask=False` 才会走单 Agent 直答，而且只在**解析成功**时才算数——
    「解析不出来」一律按需要多 Agent 处理（安全默认）。
    """

    intent_type: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    need_multi_subtask: bool = True


def _extract_json(text: str) -> Any | None:
    """从模型输出里取出 JSON 负载，容忍 Markdown 围栏与解释性前后缀。"""

    candidate = (text or "").strip()
    if not candidate:
        return None
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_intent(text: str) -> IntentResult | None:
    """解析 `intent` 块；任一处不合法返回 ``None``（不猜、不补半份意图）。"""

    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("intent")
    if not isinstance(raw, dict):
        return None

    intent_type = str(raw.get("intent_type") or "").strip() or DEFAULT_INTENT_TYPE
    user_goal = str(raw.get("user_goal") or "").strip()
    if not user_goal:
        return None

    raw_constraints = raw.get("constraints") or []
    if not isinstance(raw_constraints, list):
        return None
    constraints = [
        str(item).strip() for item in raw_constraints if str(item).strip()
    ]

    flag = raw.get("need_multi_subtask")
    return IntentResult(
        intent_type=intent_type,
        user_goal=user_goal,
        constraints=constraints,
        # 没给或类型不对 → 默认「需要多 Agent」（判不准就不要省这一步）。
        need_multi_subtask=flag if isinstance(flag, bool) else True,
    )


def parse_rewritten(text: str, original: str) -> tuple[str, str]:
    """解析改写文本；返回 `(任务, source)`。

    取不到 `rewritten_task` 时把整段输出当改写结果（旧口径兼容），但整段看起来像
    JSON 或代码围栏时**不采用**——那种情况下模型是在回答别的问题，宁可退回原文。
    """

    payload = _extract_json(text)
    candidate = ""
    if isinstance(payload, dict):
        candidate = str(payload.get("rewritten_task") or "").strip()
    if not candidate:
        bare = (text or "").strip()
        if bare and not bare.startswith("{") and not bare.startswith("```"):
            candidate = bare

    if not candidate or len(candidate) > MAX_REWRITE_CHARS:
        return original, REWRITE_SOURCE_ORIGINAL
    if _same_meaning(original, candidate):
        return original, REWRITE_SOURCE_ORIGINAL
    return candidate, REWRITE_SOURCE_MODEL


def _same_meaning(original: str, rewritten: str) -> bool:
    """是否与原文没有实质差别（去掉空白与常见标点后比较）。

    与 `app.orchestration.rewrite` 同一判据：改写节点与 intake 节点对「有没有实质改动」
    必须给出一致答案，否则同一份模型输出在两条链路上会被记成不同的 `rewrite_source`。
    """

    def squash(value: str) -> str:
        return "".join(
            ch for ch in value if ch.strip() and ch not in "。！？，、；：,.!?;:"
        )

    return squash(original) == squash(rewritten)


def intake_prompt() -> str:
    """intake 节点的 system prompt：一次调用同时要「改写」与「意图」。"""

    return (
        "你是多智能体协作平台的「问题理解 Agent」。你要在一次回答里做两件事："
        "把用户**这一轮**的话结合会话上下文改写成一段**自包含**的任务描述，"
        "并给出这次任务的**结构化意图**。\n\n"
        "第一件事——改写：\n"
        "- 补全指代：把「它 / 这个 / 上面那份 / 重试 / 再详细一点」这类话，替换成上下文中"
        "明确指代的对象与原要求；\n"
        "- 写清目标：这次要产出什么（结论、代码、文档、对比、清单……）；\n"
        "- 保留约束：使用者在历史或长期记忆里给过的格式、范围、语言、篇幅等要求要带上；\n"
        "- 上下文里没有的信息不要编造，也不要替使用者做新增假设。\n\n"
        "第二件事——意图：\n"
        "- `intent_type`：一句话的任务类型（如 `question` / `report` / `code` / `compare`）；\n"
        "- `user_goal`：用户这次真正想要的结果，一句话；\n"
        "- `constraints`：用户明确提出的限制（格式、语言、篇幅、时间、范围……），没有就给空数组；\n"
        "- `need_multi_subtask`：**是否需要拆成多个子任务并行协作**。"
        "「查一个事实 / 解释一个概念 / 写一段短代码 / 改写一段文字」这类单点任务填 false；"
        "「多源检索后对比 / 先收集再分析再成文 / 需要分别核实多个方向」这类填 true。"
        "判不准就填 true。\n\n"
        "输出要求（务必严格遵守）：\n"
        "- 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性文字；\n"
        '- 结构：{"rewritten_task": "改写后的任务描述", "intent": '
        '{"intent_type": "...", "user_goal": "...", "constraints": [], '
        '"need_multi_subtask": true}}；\n'
        "- 用与用户输入相同的语言；rewritten_task 控制在几句话以内。"
    )


def intent_block(intent: IntentResult | None) -> str:
    """把意图渲染成下游提示词前缀；没有意图时返回空串（不产生空段）。"""

    if intent is None:
        return ""
    constraints = "；".join(intent.constraints) if intent.constraints else "无"
    return (
        "【意图识别】\n"
        f"- 类型：{intent.intent_type}\n"
        f"- 目标：{intent.user_goal}\n"
        f"- 约束：{constraints}\n"
    )


def _fallback(original: str, *, reason: str, workflow_id: str | None = None) -> dict[str, Any]:
    log_event(
        logger,
        "run.intake.skipped",
        workflow_id=workflow_id,
        reason=reason,
        task_chars=len(original),
    )
    return {
        "task": original,
        "source": REWRITE_SOURCE_ORIGINAL,
        "original_chars": len(original),
        "task_chars": len(original),
        "intent": None,
        "intent_source": INTENT_SOURCE_FALLBACK,
        "tokens": 0,
    }


def intake_task(
    task: str,
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
    attachment_names: Sequence[str] = (),
    workflow_id: str | None = None,
    stage_label: str = "intake",
) -> dict[str, Any]:
    """一次平台调用同时产出改写文本与意图，返回可直接进 checkpoint 的载荷。

    返回结构：`{"task", "source", "original_chars", "task_chars", "intent", "intent_source"}`
    （`intent` 为 JSON 对象或 `None`）。**任何异常都不外抛**：这一步是执行的第一个活动，
    把它变成异常就等于把「模型没配好」升级成「整次执行失败」。
    """

    original = (task or "").strip()
    if not original:
        return _fallback(original, reason="empty_input", workflow_id=workflow_id)

    prompt_input = rewrite_prompt_input(
        original,
        history=history,
        preferences=preferences,
        attachment_names=attachment_names,
    )
    started = time.perf_counter()
    try:
        # 模型解析必须在 try 里：它和调用一样会失败（模型没配、端点不可达……）。
        resolved_settings = settings or resolve_platform_settings()
        model = llm or build_chat_model(resolved_settings)
        with observed_stage(workflow_id=workflow_id, stage=stage_label, role="intake"):
            response = model.invoke(
                [
                    SystemMessage(content=intake_prompt()),
                    HumanMessage(content=prompt_input),
                ]
            )
        text = content_with_tools(response.content)
    except Exception as exc:  # intake 失败不该让一次协作失败
        log_event(
            logger,
            "run.intake.failed",
            level=logging.WARNING,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _fallback(original, reason="error", workflow_id=workflow_id)

    rewritten, source = parse_rewritten(text, original)
    intent = parse_intent(text)
    tokens = usage_tokens(response)
    log_event(
        logger,
        "run.intake.done",
        workflow_id=workflow_id,
        source=source,
        intent_source=INTENT_SOURCE_MODEL if intent else INTENT_SOURCE_FALLBACK,
        need_multi_subtask=intent.need_multi_subtask if intent else None,
        constraints=len(intent.constraints) if intent else 0,
        task_chars=len(rewritten),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return {
        "task": rewritten,
        "source": source,
        "original_chars": len(original),
        "task_chars": len(rewritten),
        "intent": intent.model_dump(mode="json") if intent else None,
        "intent_source": INTENT_SOURCE_MODEL if intent else INTENT_SOURCE_FALLBACK,
        "tokens": tokens,
    }


__all__ = [
    "DEFAULT_INTENT_TYPE",
    "INTENT_SOURCE_FALLBACK",
    "INTENT_SOURCE_MODEL",
    "IntentResult",
    "intake_prompt",
    "intake_task",
    "intent_block",
    "parse_intent",
    "parse_rewritten",
]

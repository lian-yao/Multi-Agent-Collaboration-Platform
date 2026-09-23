"""问题改写（ADR-037）：把用户这一轮的话，用会话上下文补成一份**自包含**的任务描述。

为什么要有这一步：用户的输入常常依赖上下文才有意义——「重试」「再详细一点」「换成中文」
这类话，单独拿出来谁也看不懂；而下游（规划 Agent、各角色）拿到的是**任务文本**，不是整段
对话。历史与长期记忆虽然已经作为前缀挂上了（ADR-019 / ADR-036），但那要求模型自己从上下文
里推断"这句指的是什么"，实测下来短输入最容易吃亏。

改写只做一件事：把"这句话在当前上下文里到底要什么"写成一段自包含的任务（补全指代、
写清目标与约束、点明期望交付物）。

**它是增强，不是必需**：模型调用失败、输出为空、或结果与原文没有实质差别时一律退回原文，
绝不阻塞执行、绝不抛错（与 ADR-019/036 的降级口径一致）。退没退，写在返回值的
`source` 里，随 checkpoint 落库可审计。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import AgentSettings, get_settings
from app.core.agent_config import resolve_agent_settings
from app.memory import SessionMessage
from app.observability.instrumentation import observed_stage
from app.observability.logging import get_logger, log_event
from app.orchestration.context import conversation_block
from app.orchestration.llm import build_chat_model

logger = get_logger("orchestration.rewrite")

PLATFORM_AGENT_ID = "planner"
"""平台节点共用的模型解析 id。

问题改写与任务规划都是**平台节点**（不对应使用者可见的协作角色），因此共用同一份解析：
「默认路由」或给 planner 的绑定会同时作用于两者。**不能用裸环境配置**——使用者在
「工具与配置」里配好的模型是通过数据库生效的，只看环境变量会得到"模型缺失"，
2026-09-23 实测就是这么炸掉整条执行的（改写是执行的第一个活动）。
要单独给改写换更便宜的模型，再另开一个 agent id 并在这里改。
"""

REWRITE_SOURCE_MODEL = "model"
"""改写结果来自模型。"""

REWRITE_SOURCE_ORIGINAL = "original"
"""退回原文（模型失败 / 空输出 / 没实质改动）。"""

MAX_REWRITE_CHARS = 2000
"""改写结果的长度上限；超了就退回原文——模型跑偏成一篇散文时不该把下游带偏。"""


def rewrite_prompt() -> str:
    """改写节点的 system prompt。"""

    return (
        "你是多智能体协作平台的「问题改写 Agent」。你的唯一职责是把用户**这一轮**的话，"
        "结合会话上下文，改写成一段**自包含**的任务描述，交给后续的规划与执行。\n\n"
        "要做的：\n"
        "- 补全指代：把「它 / 这个 / 上面那份 / 重试 / 再详细一点」这类话，替换成上下文中"
        "明确指代的对象与原要求；\n"
        "- 写清目标：这次要产出什么（结论、代码、文档、对比、清单……）；\n"
        "- 保留约束：使用者在历史或长期记忆里给过的格式、范围、语言、篇幅等要求要带上；\n"
        "- 上下文里没有的信息不要编造，也不要替使用者做新增假设。\n\n"
        "输出要求（务必严格遵守）：\n"
        "- 只输出改写后的任务描述本身，不要任何前后缀、不要解释你改了什么、不要 Markdown 标题；\n"
        "- 用与用户输入相同的语言；\n"
        "- 长度控制在几句话以内——它是任务说明，不是方案。"
    )


def rewrite_prompt_input(
    task: str,
    *,
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
    attachment_names: Sequence[str] = (),
) -> str:
    """构造改写节点的用户输入：长期记忆 + 会话历史 + 本轮附件名 + 本轮原文。

    段落顺序与其他节点一致（约束 → 上下文 → 本轮指令），见 `app/orchestration/context.py`。
    附件**只给文件名**：正文塞进改写提示词没有收益（改写不需要读内容），却会把 token 花在
    重复内容上。
    """

    parts: list[str] = []
    if preferences:
        parts.append(preferences)
    if history:
        parts.append(conversation_block(history))
    if attachment_names:
        names = "、".join(attachment_names)
        parts.append(f"【本轮附件（仅文件名）】\n{names}\n")
    parts.append(f"【用户这一轮的原话】\n{task}")
    return "\n".join(parts)


def rewrite_task(
    task: str,
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
    attachment_names: Sequence[str] = (),
    workflow_id: str | None = None,
    stage_label: str = "rewrite",
) -> dict[str, Any]:
    """把 `task` 改写成更完整的任务描述；任何异常都退回原文。

    返回 `{"task": ..., "source": "model" | "original", "original_chars": n, "task_chars": n}`。
    """

    original = (task or "").strip()
    if not original:
        return _fallback(original, reason="empty_input")

    prompt_input = rewrite_prompt_input(
        original,
        history=history,
        preferences=preferences,
        attachment_names=attachment_names,
    )
    started = time.perf_counter()
    try:
        # **模型解析必须在 try 里**：它和调用一样会失败（模型没配、端点不可达……），
        # 而这一步是执行的第一个活动——放在 try 外就等于把"配置问题"升级成"整次执行失败"。
        resolved_settings = settings or _resolve_settings()
        model = llm or build_chat_model(resolved_settings)
        with observed_stage(workflow_id=workflow_id, stage=stage_label, role="rewriter"):
            response = model.invoke(
                [
                    SystemMessage(content=rewrite_prompt()),
                    HumanMessage(content=prompt_input),
                ]
            )
        text = _plain_text(response.content)
    except Exception as exc:  # 改写失败不该让一次协作失败
        log_event(
            logger,
            "run.rewrite.failed",
            level=logging.WARNING,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _fallback(original, reason="error")

    rewritten = text.strip()
    if not rewritten or len(rewritten) > MAX_REWRITE_CHARS:
        return _fallback(original, reason="empty_or_too_long")
    if _same_meaning(original, rewritten):
        # 模型原样抄回来（或只加了标点）：记成"没实质改动"，但仍用它的输出——
        # 两者内容一致，用哪个都不影响，标成 original 更诚实。
        return _fallback(original, reason="unchanged")

    log_event(
        logger,
        "run.rewrite.done",
        workflow_id=workflow_id,
        original_chars=len(original),
        task_chars=len(rewritten),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return {
        "task": rewritten,
        "source": REWRITE_SOURCE_MODEL,
        "original_chars": len(original),
        "task_chars": len(rewritten),
    }


def _fallback(task: str, *, reason: str) -> dict[str, Any]:
    log_event(
        logger,
        "run.rewrite.skipped",
        reason=reason,
        task_chars=len(task),
    )
    return {
        "task": task,
        "source": REWRITE_SOURCE_ORIGINAL,
        "original_chars": len(task),
        "task_chars": len(task),
    }


def _resolve_settings() -> AgentSettings:
    """按平台节点的口径解析模型配置；解析失败退回环境配置（与规划节点同口径）。"""

    try:
        return resolve_agent_settings(PLATFORM_AGENT_ID)
    except Exception:  # 配置查询失败不该让改写失败，更不该让执行失败
        return get_settings()


def _plain_text(content: str | list[Any]) -> str:
    """把 `AIMessage.content` 归一化成纯文本（与编排层其它节点同一处理）。"""

    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _same_meaning(original: str, rewritten: str) -> bool:
    """是否与原文没有实质差别（去掉空白与常见标点后比较）。"""

    def squash(value: str) -> str:
        return "".join(ch for ch in value if ch.strip() and ch not in "。！？，、；：,.!?;:")

    return squash(original) == squash(rewritten)


__all__ = [
    "MAX_REWRITE_CHARS",
    "PLATFORM_AGENT_ID",
    "REWRITE_SOURCE_MODEL",
    "REWRITE_SOURCE_ORIGINAL",
    "rewrite_prompt",
    "rewrite_prompt_input",
    "rewrite_task",
]

"""会话历史 → 提示词前缀（ADR-019）。

**两条编排链路共用这一份**：固定三步（``pipeline_graph``）与动态编排
（``dynamic_graph``）。这不是为了少写几行——这一段渲染一旦各写一份，就会出现
「静态链路记得上一轮、动态链路不记得」这种只有用户能发现的差异：2026-09-23 的
实测反馈「同一个会话不记得我之前说过什么」正是这么来的（当时动态图没有接线）。

输出形态（无历史时返回空串，不产生空段落——单轮行为因此与接线前逐字一致）：:

    【会话历史（最近 N 条，供多轮上下文继承）】
    user: …
    assistant: …

"""

from __future__ import annotations

from collections.abc import Sequence

from app.memory import SessionMessage

CONVERSATION_CONTEXT_LIMIT = 10
"""注入提示词的历史条数上限（按「最近 N 条」读，见 ADR-019 的读点）。"""


def conversation_block(history: Sequence[SessionMessage]) -> str:
    """把会话历史渲染成提示词前缀；无有效内容时返回空串（不加空段）。"""

    lines = [
        f"{message.role.value}: {message.content.strip()}"
        for message in history
        if message.content and message.content.strip()
    ]
    if not lines:
        return ""
    header = f"【会话历史（最近 {len(lines)} 条，供多轮上下文继承）】"
    return "\n".join([header, *lines, ""])


__all__ = ["CONVERSATION_CONTEXT_LIMIT", "conversation_block"]

"""问题改写（ADR-037）：提示词带上下文、以及**任何异常都退回原文**。

这一步是增强而不是必需：模型失败、输出为空、跑偏成长文、或原样抄回来，都必须退回原文，
不阻塞执行、不抛错——否则一次改写抖动就会让整次协作失败。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.memory import MessageRole, SessionMessage
from app.orchestration.rewrite import (
    MAX_REWRITE_CHARS,
    REWRITE_SOURCE_MODEL,
    REWRITE_SOURCE_ORIGINAL,
    rewrite_prompt,
    rewrite_prompt_input,
    rewrite_task,
)


class ScriptedChatModel(BaseChatModel):
    """按脚本回复的假模型（与 test_dynamic_pipeline 同形），并记录收到的消息。"""

    replies: list[str] = Field(default_factory=list)
    error: Exception | None = None
    calls: list[list[object]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-rewrite-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        content = self.replies.pop(0) if self.replies else ""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _history(*pairs: tuple[MessageRole, str]) -> tuple[SessionMessage, ...]:
    return tuple(
        SessionMessage(session_id="s-1", role=role, content=content) for role, content in pairs
    )


def test_rewrite_prompt_input_carries_context_in_order():
    """长期记忆 → 会话历史 → 本轮附件名 → 本轮原话，与其它节点同一段落顺序（ADR-019/036/037）。"""

    text = rewrite_prompt_input(
        "再详细一点",
        history=_history(
            (MessageRole.USER, "帮我写一份季度复盘"),
            (MessageRole.ASSISTANT, "已给出大纲。"),
        ),
        preferences="【长期记忆（跨会话，1 条）】\n偏好.语言: 中文\n",
        attachment_names=("2026-Q3.xlsx",),
    )

    assert text.index("长期记忆") < text.index("会话历史") < text.index("附件")
    assert text.index("附件") < text.index("用户这一轮的原话")
    assert "2026-Q3.xlsx" in text
    assert "再详细一点" in text


def test_rewrite_prompt_input_omits_empty_sections():
    """没有附件/历史时不产生空段落（与 ADR-019 同口径）。"""

    text = rewrite_prompt_input("只做这一件事")

    assert text == "【用户这一轮的原话】\n只做这一件事"
    assert "会话历史" not in text


def test_rewrite_prompt_forbids_meta_commentary():
    prompt = rewrite_prompt()

    assert "自包含" in prompt
    assert "补全指代" in prompt
    # 输出必须是任务本身，不能夹带"我改了什么"
    assert "不要" in prompt
    assert "JSON" not in prompt, "改写输出是自然语言任务描述，不是结构化载荷"


def test_rewrite_task_returns_model_output_and_marks_source():
    model = ScriptedChatModel(
        replies=[
            "把上一轮给出的季度复盘大纲扩写成完整报告：包含实际数据、关键结论与改进建议，"
            "用中文，篇幅控制在一页以内。"
        ]
    )

    result = rewrite_task(
        "再详细一点",
        llm=model,
        history=_history((MessageRole.ASSISTANT, "已给出大纲。")),
    )

    assert result["source"] == REWRITE_SOURCE_MODEL
    assert result["task"].startswith("把上一轮给出的季度复盘大纲扩写成完整报告")
    assert result["original_chars"] == len("再详细一点")
    assert model.calls[0][0].content == rewrite_prompt()


def test_rewrite_task_falls_back_when_the_model_raises():
    model = ScriptedChatModel(error=RuntimeError("provider unreachable"))

    result = rewrite_task("再详细一点", llm=model)

    assert result["source"] == REWRITE_SOURCE_ORIGINAL
    assert result["task"] == "再详细一点"


def test_rewrite_task_degrades_when_the_model_cannot_even_be_built():
    """**模型解析/构造失败也必须退回原文**。

    这条是 2026-09-23 实测事故的回归：改写是执行的**第一个活动**，而它当时用裸环境配置
    解析模型（使用者在界面上配好的模型在数据库里，看不到），于是在第一步就把整条执行炸掉。
    降级保护必须覆盖"构造模型"这一步，而不只是"调用模型"。
    """

    from app.config import AgentSettings

    # provider=openai 但没给模型名 —— 构造阶段就会抛错（与线上事故同形）
    broken = AgentSettings(_env_file=None, llm_provider="openai", openai_model="")

    result = rewrite_task("再详细一点", settings=broken)

    assert result["source"] == REWRITE_SOURCE_ORIGINAL
    assert result["task"] == "再详细一点"


def test_rewrite_task_falls_back_on_empty_output():
    result = rewrite_task("再详细一点", llm=ScriptedChatModel(replies=["   "]))

    assert result["source"] == REWRITE_SOURCE_ORIGINAL
    assert result["task"] == "再详细一点"


def test_rewrite_task_falls_back_when_the_output_is_a_rambling_essay():
    """模型跑偏成长文时退回原文：那已经不是任务说明了，塞给下游只会把链路带偏。"""

    result = rewrite_task("再详细一点", llm=ScriptedChatModel(replies=["长" * MAX_REWRITE_CHARS + "多"]))

    assert result["source"] == REWRITE_SOURCE_ORIGINAL


def test_rewrite_task_falls_back_when_nothing_meaningfully_changed():
    """原样抄回来（或只补了标点）时记成 original：内容一致，标成 model 是虚报。"""

    result = rewrite_task("写出冒泡排序", llm=ScriptedChatModel(replies=["写出冒泡排序。"]))

    assert result["source"] == REWRITE_SOURCE_ORIGINAL
    assert result["task"] == "写出冒泡排序"


def test_rewrite_task_with_empty_input_does_not_call_the_model():
    model = ScriptedChatModel(replies=["不该被用到"])

    result = rewrite_task("   ", llm=model)

    assert result["task"] == ""
    assert result["source"] == REWRITE_SOURCE_ORIGINAL
    assert model.calls == [], "空输入没必要花一次模型调用"

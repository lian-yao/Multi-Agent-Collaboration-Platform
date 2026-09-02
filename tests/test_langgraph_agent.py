import pytest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import AgentSettings
from app.orchestration.graph import build_langgraph_agent, run_agent
from app.orchestration.llm import build_chat_model


class FakeChatModel(BaseChatModel):
    """不依赖真实模型，用于验证图结构与状态流转。"""

    replies: list[str] = ["fake reply"]

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        content = self.replies.pop(0) if self.replies else "done"
        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_single_agent_graph_returns_reply():
    agent = build_langgraph_agent(llm=FakeChatModel())

    result = agent.invoke({"messages": [HumanMessage(content="你好")]})

    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "fake reply"


def test_run_agent_returns_reply():
    messages = run_agent("你好", llm=FakeChatModel())

    assert messages[-1].content == "fake reply"


def test_run_agent_keeps_conversation_messages():
    fake = FakeChatModel(replies=["first", "second"])
    agent = build_langgraph_agent(llm=fake)

    state = {"messages": [HumanMessage(content="第一轮")]}
    state = agent.invoke(state)
    state = agent.invoke(
        {"messages": [*state["messages"], HumanMessage(content="第二轮")]}
    )

    assert [message.content for message in state["messages"]] == [
        "第一轮",
        "first",
        "第二轮",
        "second",
    ]


def test_default_settings_use_ollama_from_doc():
    settings = AgentSettings(_env_file=None)

    assert settings.llm_provider == "ollama"
    assert settings.ollama_model == "qwen2.5-coder:7b"


def test_openai_requires_explicit_model():
    settings = AgentSettings(llm_provider="openai", _env_file=None)

    with pytest.raises(ValueError, match="AGENT_OPENAI_MODEL"):
        build_chat_model(settings)

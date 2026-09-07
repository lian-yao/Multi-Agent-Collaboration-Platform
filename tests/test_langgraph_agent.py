import pytest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

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


def test_factory_ollama_branch_builds_chat_ollama():
    settings = AgentSettings(
        llm_provider="ollama",
        ollama_base_url="http://ollama.test:11434",
        ollama_model="qwen2.5-coder:7b",
        temperature=0.4,
        _env_file=None,
    )

    model = build_chat_model(settings)

    assert isinstance(model, ChatOllama)
    assert model.base_url == "http://ollama.test:11434"
    assert model.model == "qwen2.5-coder:7b"
    assert model.temperature == 0.4


def test_factory_openai_branch_builds_chat_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = AgentSettings(
        llm_provider="openai",
        openai_model="gpt-4o-mini",
        temperature=0.3,
        _env_file=None,
    )

    model = build_chat_model(settings)

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4o-mini"
    assert model.temperature == 0.3


def test_factory_rejects_unknown_provider():
    settings = AgentSettings(_env_file=None)
    settings.llm_provider = "anthropic"  # 绕过 Literal 校验，测试工厂自身防御

    with pytest.raises(ValueError, match="不支持的模型提供方"):
        build_chat_model(settings)

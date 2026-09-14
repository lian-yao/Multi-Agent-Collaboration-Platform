from langchain_core.language_models.chat_models import BaseChatModel

from app.config import AgentSettings
from app.observability.callbacks import ObservabilityCallbackHandler


def build_chat_model(settings: AgentSettings) -> BaseChatModel:
    """按配置创建 Ollama 或 OpenAI 聊天模型。

    模型接入属于成员 C 的职责（`分工.md` §2「多模型接入」），因此这里同时挂上
    观测回调：每次模型调用生成 Span 并统计 Token 消耗（`app/observability/callbacks.py`）。
    回调由 LangChain 在实例上携带，`bind_tools` 后的绑定对象同样继承，
    不需要在调用点重复传参。
    """

    callbacks = [ObservabilityCallbackHandler()]

    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            temperature=settings.temperature,
            callbacks=callbacks,
        )

    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_model:
            raise ValueError(
                "llm_provider=openai 时必须设置 AGENT_OPENAI_MODEL"
            )
        return ChatOpenAI(
            model=settings.openai_model,
            temperature=settings.temperature,
            callbacks=callbacks,
        )

    raise ValueError(f"不支持的模型提供方: {settings.llm_provider}")

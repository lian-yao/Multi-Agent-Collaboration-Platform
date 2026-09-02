from langchain_core.language_models.chat_models import BaseChatModel

from app.config import AgentSettings


def build_chat_model(settings: AgentSettings) -> BaseChatModel:
    """按配置创建 Ollama 或 OpenAI 聊天模型。"""

    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            temperature=settings.temperature,
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
        )

    raise ValueError(f"不支持的模型提供方: {settings.llm_provider}")

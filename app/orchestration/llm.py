from langchain_core.language_models.chat_models import BaseChatModel

from app.config import AgentSettings
from app.core.provider_config import openai_api_key
from app.observability.callbacks import ObservabilityCallbackHandler


def build_chat_model(settings: AgentSettings) -> BaseChatModel:
    """按配置创建 OpenAI 兼容 API 或 Ollama 聊天模型。

    模型接入属于成员 C 的职责（`分工.md` §2「多模型接入」），因此这里同时挂上
    观测回调：每次模型调用生成 Span 并统计 Token 消耗（`app/observability/callbacks.py`）。
    回调由 LangChain 在实例上携带，`bind_tools` 后的绑定对象同样继承，
    不需要在调用点重复传参。

    传入的 `settings` 应是**已合并运行期配置**的生效值（`resolve_provider_settings`
    → `resolve_agent_settings`）。缺少模型名或凭据时直接抛错（fail-fast，ADR-014）：
    不静默回退到其他提供方，避免「看起来成功」。
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
                "llm_provider=openai 时必须设置模型名：环境变量 AGENT_OPENAI_MODEL，"
                "或通过 PUT /api/v1/config/provider 写入 model"
            )
        api_key = openai_api_key(settings)
        if not api_key:
            raise ValueError(
                "llm_provider=openai 时必须提供凭据：环境变量 AGENT_OPENAI_API_KEY / "
                "OPENAI_API_KEY，或通过 PUT /api/v1/config/provider 写入 api_key"
            )
        kwargs: dict[str, object] = {
            "model": settings.openai_model,
            "temperature": settings.temperature,
            "api_key": api_key,
            "callbacks": callbacks,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(f"不支持的模型提供方: {settings.llm_provider}")

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import AgentSettings
from app.core.provider_config import openai_api_key
from app.observability.callbacks import ObservabilityCallbackHandler

SUPPORTED_OPENAI_API_TYPES = frozenset({"openai-compatible", "openai-responses"})
"""有客户端实现的 OpenAI 系协议族。

`anthropic` / `gemini` / `amazon-bedrock` 目前在 `pyproject.toml` 里没有对应
LangChain 集成包，因此**显式报错**而不是退化成 OpenAI 兼容请求
（退化成 OpenAI 请求会连到一个不认识的端点，错误信息难懂，见 ADR-017 §7）。
"""


def build_chat_model(settings: AgentSettings) -> BaseChatModel:
    """按生效配置创建聊天模型。

    模型接入属于成员 C 的职责（`分工.md` §2「多模型接入」），因此这里同时挂上
    观测回调：每次模型调用生成 Span 并统计 Token 消耗（`app/observability/callbacks.py`）。
    回调由 LangChain 在实例上携带，`bind_tools` 后的绑定对象同样继承，
    不需要在调用点重复传参。

    传入的 `settings` 应是**已合并运行期配置**的生效值（`resolve_provider_settings`
    → `resolve_agent_settings`）。缺少模型名或凭据时直接抛错（fail-fast，ADR-014）：
    不静默回退到其他提供方，避免「看起来成功」。

    特化调参（ADR-017）：`temperature` / `top_p` / `max_tokens` 与
    `custom_headers` 只在有取值时才传，避免把「未设置」变成「显式设为默认值」
    而覆盖服务端行为。
    """

    callbacks = [ObservabilityCallbackHandler()]

    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict[str, object] = {
            "base_url": settings.ollama_base_url,
            "model": settings.ollama_model,
            "temperature": settings.temperature,
            "callbacks": callbacks,
        }
        if not settings.ollama_model:
            raise ValueError(
                "llm_provider=ollama 时必须设置模型名：环境变量 AGENT_OLLAMA_MODEL，"
                "或通过 PUT /api/v1/config/provider 写入 model，"
                "或在「模型」页把该角色绑定到一个 ollama 模型条目"
            )
        if settings.top_p is not None:
            kwargs["top_p"] = settings.top_p
        if settings.custom_headers:
            kwargs["client_kwargs"] = {"headers": dict(settings.custom_headers)}
        return ChatOllama(**kwargs)

    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        if settings.api_type not in SUPPORTED_OPENAI_API_TYPES:
            raise ValueError(
                f"协议族 api_type={settings.api_type} 暂无客户端实现，"
                "当前支持 openai-compatible / openai-responses；"
                "请把该 Provider 的 api_type 改为受支持的取值"
            )
        if not settings.openai_model:
            raise ValueError(
                "llm_provider=openai 时必须设置模型名：环境变量 AGENT_OPENAI_MODEL，"
                "或通过 PUT /api/v1/config/provider 写入 model，"
                "或在「模型」页把该角色绑定到一个模型条目"
            )
        api_key = openai_api_key(settings)
        if not api_key:
            raise ValueError(
                "llm_provider=openai 时必须提供凭据：环境变量 AGENT_OPENAI_API_KEY / "
                "OPENAI_API_KEY，或通过 PUT /api/v1/config/provider 写入 api_key，"
                "或在「Provider」页为该 Provider 配置 api_key"
            )
        kwargs = {
            "model": settings.openai_model,
            "temperature": settings.temperature,
            "api_key": api_key,
            "callbacks": callbacks,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        if settings.top_p is not None:
            kwargs["top_p"] = settings.top_p
        if settings.max_tokens is not None:
            kwargs["max_tokens"] = settings.max_tokens
        if settings.custom_headers:
            kwargs["default_headers"] = dict(settings.custom_headers)
        return ChatOpenAI(**kwargs)

    raise ValueError(f"不支持的模型提供方: {settings.llm_provider}")

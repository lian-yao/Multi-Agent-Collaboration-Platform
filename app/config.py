from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProviderKind = Literal["ollama", "openai"]
"""运行期**构造方式**：本地 Ollama 与 OpenAI 兼容客户端。"""


class AgentSettings(BaseSettings):
    """Agent 编排层配置，环境变量统一使用 AGENT_ 前缀。

    这里是**环境回退值**：运行期可用 `provider_configs` 的默认路由
    （`default_llm_model_id` → `llm_providers` / `llm_models`，ADR-017）覆盖，
    也可被其 legacy 五列覆盖，每个角色的模型与调参还可被 `agent_configs`
    再覆盖一层（ADR-013、ADR-017 §2）。

    `llm_provider` 仍然只有 `ollama` / `openai` 两个取值——它是**怎么连**，
    不是**连谁**：`preset_type` / `api_type` 记录后者。协议族为
    `anthropic` / `gemini` / `amazon-bedrock` 时 `llm_provider` 取 `openai`，
    由 `app/orchestration/llm.py` 判断是否具备对应实现并给出明确错误。
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )

    llm_provider: LlmProviderKind = "openai"
    openai_model: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    temperature: float = 0.2

    # ---- ADR-017：注册表带来的路由与调参 ----
    preset_type: str = "openai-compatible"
    """配置层预设族（`openai` / `deepseek` / `ollama` …），仅用于展示与默认值。"""

    api_type: str = "openai-compatible"
    """运行期协议族，决定 `build_chat_model` 用哪个客户端实现。"""

    llm_model_id: str = ""
    """当前生效的模型条目 id；空表示未绑定注册表，走 legacy 列或环境配置。"""

    provider_name: str = ""
    """展示用的 Provider 名称。"""

    top_p: float | None = None
    """nucleus sampling；None 表示不显式发送该参数。"""

    max_tokens: int | None = None
    """单次输出上限；None 表示不显式发送该参数。"""

    reasoning_type: str = "none"
    """推理模式：`none` / `openai` / `gemini` / `anthropic`。"""

    custom_headers: dict[str, str] = Field(default_factory=dict)
    """Provider 级附加请求头，按原样透传给模型客户端。"""

    # ---- 编排模式（ADR-019）----
    orchestration_mode: Literal["static", "dynamic"] = "static"
    """编排模式：`static` 为固定三步流水线，`dynamic` 由规划节点按任务决定角色与顺序。

    **默认 static**：动态编排是新链路，默认关闭可保证既有演示链路与 Dapr 断点续跑
    行为零变化；要启用需显式设 `AGENT_ORCHESTRATION_MODE=dynamic`，或在单次请求上
    带 `orchestration_mode` 覆盖（`doc/api.md` §4.4）。
    """

    max_plan_steps: int = 6
    """动态编排单次执行允许的最大步骤数；既是成本上限，也是回退判据。"""


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()

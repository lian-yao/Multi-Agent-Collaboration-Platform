from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent 编排层配置，环境变量统一使用 AGENT_ 前缀。

    这里是**环境回退值**：运行期可用 `provider_configs` 覆盖
    （`app/core/provider_config.py`、ADR-014），每个角色的模型与温度还可被
    `agent_configs` 再覆盖一层（ADR-013）。默认提供方为 OpenAI 兼容 API，
    本地 Ollama 通过 `AGENT_LLM_PROVIDER=ollama` 显式启用。
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )

    llm_provider: Literal["ollama", "openai"] = "openai"
    openai_model: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    temperature: float = 0.2


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()

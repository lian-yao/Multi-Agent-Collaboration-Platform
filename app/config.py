from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent 编排层配置，环境变量统一使用 AGENT_ 前缀。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )

    llm_provider: Literal["ollama", "openai"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    openai_model: str = ""
    temperature: float = 0.2


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()


class AdminSettings(BaseSettings):
    """管理面配置：配置写入接口的权限边界（`doc/api.md` §5.7）。

    字段名不带前缀，因此环境变量就是 `ADMIN_TOKEN`。默认空值表示**禁用**写接口
    （fail-closed，见 ADR-013）：忘记配置只是功能不可用，不会把写权限暴露出去。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_token: str = ""


@lru_cache
def get_admin_settings() -> AdminSettings:
    return AdminSettings()

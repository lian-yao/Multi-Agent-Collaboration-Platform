"""MCP 接入配置（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3「MCP工具集成」——
工具注册（MCP Server 暴露）、动态发现、工具执行。

三种接入方式由 `MCP_TRANSPORT` 选择：

- `inprocess`（默认）：工具在编排进程内直接调用，不依赖外部进程，演示与本地开发友好；
- `stdio`：流水线通过 MCP 客户端启动 `python -m app.mcp.server` 子进程并走标准协议；
- `http`：连接已部署的 MCP Server（streamable HTTP）。

环境变量统一使用 `MCP_` 前缀。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

McpTransport = Literal["inprocess", "stdio", "http"]


class McpSettings(BaseSettings):
    """MCP 客户端接入参数。"""

    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        env_file=".env",
        extra="ignore",
    )

    transport: McpTransport = "inprocess"
    server_name: str = "macp-tools"
    server_command: str = ""
    """stdio 模式下的可执行文件；留空使用当前解释器（`sys.executable`）。"""

    server_url: str = "http://localhost:8081/mcp"
    call_timeout_seconds: float = 30.0

    def resolved_command(self) -> str:
        return self.server_command or sys.executable


@lru_cache
def get_mcp_settings() -> McpSettings:
    return McpSettings()

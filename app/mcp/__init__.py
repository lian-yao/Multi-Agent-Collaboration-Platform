"""MCP 工具集成：注册、发现与调用（见 `doc/architecture.md` 模块划分）。"""

from app.mcp.client import McpToolRegistry
from app.mcp.config import McpSettings, get_mcp_settings
from app.mcp.registry import (
    CompositeToolRegistry,
    InstrumentedToolRegistry,
    build_tool_registry,
    reset_registered_servers_cache,
    reset_tool_registry_cache,
    tool_catalog,
)
from app.mcp.server import build_mcp_server

__all__ = [
    "CompositeToolRegistry",
    "InstrumentedToolRegistry",
    "McpSettings",
    "McpToolRegistry",
    "build_mcp_server",
    "build_tool_registry",
    "get_mcp_settings",
    "reset_registered_servers_cache",
    "reset_tool_registry_cache",
    "tool_catalog",
]

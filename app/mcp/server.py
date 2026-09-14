"""MCP Server：按标准格式暴露内置工具（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3「工具注册：MCP Server
按标准格式暴露工具（名称、描述、JSON Schema定义）」。

服务端只做协议适配：工具清单来自 `app/tools/registry.py`，
`list_tools` 返回 `name` / `description` / `inputSchema`，
`call_tool` 返回 JSON 文本（成功为 `{"status","output"}`，失败为 `{"status","error"}`），
与 `app/mcp/client.py` 的解析约定成对。

单独启动：`python -m app.mcp.server`（stdio 传输）。
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from app.mcp.config import McpSettings, get_mcp_settings
from app.orchestration.tools import ToolCall
from app.observability.logging import get_logger, log_event
from app.tools.base import ToolExecutionError
from app.tools.registry import BuiltinToolRegistry, build_builtin_registry

logger = get_logger("mcp.server")


def build_mcp_server(
    registry: BuiltinToolRegistry | None = None,
    *,
    settings: McpSettings | None = None,
) -> Server:
    """把内置工具注册表包装成 MCP Server。"""

    resolved = settings or get_mcp_settings()
    tools = registry if registry is not None else build_builtin_registry()
    server: Server = Server(resolved.server_name)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        log_event(logger, "mcp.list_tools", count=len(tools.list_tools()))
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema or {"type": "object", "properties": {}},
            )
            for spec in tools.list_tools()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None):
        log_event(logger, "mcp.call_tool", tool_name=name)
        try:
            output = tools.call(
                ToolCall(call_id=f"mcp:{name}", tool_name=name, arguments=dict(arguments or {}))
            )
        except ToolExecutionError as exc:
            payload: dict[str, Any] = {"status": "failed", "error": str(exc)}
        except Exception as exc:  # 未预期异常也转成协议内错误，不打断会话
            payload = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        else:
            payload = {"status": "succeeded", "output": _jsonable(output)}
        return [types.TextContent(type="text", text=_dump(payload))]

    return server


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _serve_stdio(server: Server) -> None:  # pragma: no cover - 进程入口
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:  # pragma: no cover - 进程入口，由 MCP 客户端以子进程方式启动
    """以 stdio 传输运行 MCP Server。"""

    import anyio

    anyio.run(_serve_stdio, build_mcp_server())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["build_mcp_server", "main"]

"""MCP 工具注册表接入点（成员 C D7-8，契约见 ADR-009）。

`build_tool_registry()` 是编排层 `default_tool_registry()` 的默认解析目标：
流水线（`app/workflows/pipeline.py` 的阶段活动）**无需改动**即可发现并调用这里的工具。

职责分工：

- 工具清单与实现 → `app/tools`；
- MCP 协议适配（Server/Client）→ `app/mcp/server.py`、`app/mcp/client.py`；
- 本模块负责**选择接入方式**（`MCP_TRANSPORT`）并给注册表套上指标/追踪/日志。

接入方式（`app/mcp/config.py`）：

- `inprocess`（默认）：进程内直接调用，注册表来自 `app/tools`；
- `stdio`：通过 MCP 客户端子进程调用 `python -m app.mcp.server`；
- `http`：调用已部署的 MCP Server。

注册表按进程缓存（ADR-009 要求 `build_tool_registry()` 保持廉价、不重复建连接）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from app.mcp.client import McpToolRegistry, build_http_session_factory, build_stdio_session_factory
from app.mcp.config import McpSettings, get_mcp_settings
from app.observability.logging import get_logger, log_event
from app.observability.metrics import get_metrics_collector, record_tool_call
from app.observability.tracing import record_exception, span
from app.orchestration.tools import ToolCall, ToolSpec
from app.tools.base import BuiltinTool
from app.tools.registry import BuiltinToolRegistry, build_builtin_registry
from app.tools.session_files import session_file_tools

logger = get_logger("mcp.registry")


class InstrumentedToolRegistry:
    """给任意 `ToolRegistry` 套上指标（成功率/耗时）、Span 与结构化日志。

    编排层 `ToolCaller` 已负责「失败不中断流水线」的归一化，这里只在调用前后
    采集观测数据，异常原样抛出。
    """

    def __init__(self, registry: Any, *, source: str) -> None:
        self._registry = registry
        self.source = source

    def list_tools(self) -> Sequence[ToolSpec]:
        return self._registry.list_tools()

    def call(self, request: ToolCall) -> Any:
        started = time.perf_counter()
        with span(
            "tool.call",
            tool_name=request.tool_name,
            call_id=request.call_id,
            source=self.source,
        ) as current:
            try:
                output = self._registry.call(request)
            except Exception as exc:
                duration_ms = _elapsed_ms(started)
                record_exception(current, exc)
                _observe(request, "failed", duration_ms, error=f"{type(exc).__name__}: {exc}")
                raise
            _observe(request, "succeeded", _elapsed_ms(started))
            return output

    def close(self) -> None:
        closer = getattr(self._registry, "close", None)
        if callable(closer):
            closer()


class SessionFileRegistry:
    """在基础注册表上追加「读本次会话附件」的工具（`app/tools/session_files.py`）。

    为什么是装饰器而不是往 `build_tool_registry()` 里塞：这两个工具需要 `session_id`，
    而那个注册表是**进程级缓存**的（ADR-009 要求它廉价可复用）。会话级的东西必须按
    执行临时拼，不能进进程缓存。

    基础工具在 `MCP_TRANSPORT=stdio/http` 时来自远端 MCP Server，这两个工具仍在平台
    进程内执行——它们读的是平台自己的附件表，本来就没有"远端"可言。
    """

    def __init__(self, registry: Any, tools: Sequence[BuiltinTool]) -> None:
        self._registry = registry
        self._tools = {tool.name: tool for tool in tools}

    def list_tools(self) -> tuple[ToolSpec, ...]:
        base = tuple(self._registry.list_tools())
        extra = tuple(
            tool.spec() for name, tool in sorted(self._tools.items())
        )
        return base + extra

    def call(self, request: ToolCall) -> Any:
        tool = self._tools.get(request.tool_name)
        if tool is not None:
            return tool.invoke(request.arguments)
        return self._registry.call(request)

    def close(self) -> None:
        closer = getattr(self._registry, "close", None)
        if callable(closer):
            closer()


def with_session_files(registry: Any, session_id: str | None) -> Any:
    """给注册表挂上会话文件工具；没绑定会话或功能关闭时原样返回。"""

    tools = session_file_tools(session_id)
    if not tools:
        return registry
    return SessionFileRegistry(registry, tools)


def tool_catalog() -> list[dict[str, Any]]:
    """`GET /api/v1/tools` 的目录数据（`doc/api.md` §5.3）。

    返回 name / description / input_schema / status，由 API 层注入
    `app/api/inspection.py::InspectionStore(tool_catalog=...)`。
    """

    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "status": "available",
        }
        for spec in build_tool_registry().list_tools()
    ]


def build_tool_registry(settings: McpSettings | None = None) -> Any:
    """按 `MCP_TRANSPORT` 构建注册表；同一进程内复用同一实例。"""

    global _registry
    if _registry is not None:
        return _registry

    resolved = settings or get_mcp_settings()
    _registry = InstrumentedToolRegistry(_build_backend(resolved), source=resolved.transport)
    log_event(
        logger,
        "registry.ready",
        transport=resolved.transport,
        tools=len(_registry.list_tools()),
    )
    return _registry


def reset_tool_registry_cache() -> None:
    """关闭并丢弃缓存注册表（测试与配置热更新使用）。"""

    global _registry
    if _registry is not None:
        _registry.close()
        _registry = None


def _build_backend(settings: McpSettings) -> Any:
    if settings.transport == "inprocess":
        return build_builtin_registry()
    if settings.transport == "stdio":
        return McpToolRegistry(build_stdio_session_factory(settings), settings=settings)
    return McpToolRegistry(build_http_session_factory(settings), settings=settings)


def _observe(
    request: ToolCall,
    status: str,
    duration_ms: float,
    error: str | None = None,
) -> None:
    log_event(
        logger,
        "tool.observed",
        call_id=request.call_id,
        tool_name=request.tool_name,
        status=status,
        duration_ms=duration_ms,
        error=error,
    )
    record_tool_call(
        request.tool_name,
        status,
        duration_ms,
        call_id=request.call_id,
        collector=get_metrics_collector(),
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


_registry: InstrumentedToolRegistry | None = None


__all__ = [
    "BuiltinToolRegistry",
    "InstrumentedToolRegistry",
    "SessionFileRegistry",
    "build_tool_registry",
    "reset_tool_registry_cache",
    "tool_catalog",
    "with_session_files",
]

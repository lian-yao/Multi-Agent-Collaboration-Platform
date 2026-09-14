"""MCP 客户端注册表：把 MCP 会话接成编排层的 `ToolRegistry`（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3「动态发现」与「工具执行」；
`app/orchestration/tools.py` 的注册表协议是**同步**的，而 MCP Python SDK 是异步的，
因此这里用一个后台事件循环线程把会话包成同步门面（`_SessionBridge`）。

会话在首次使用时惰性建立并复用（ADR-009 要求 `build_tool_registry()` 保持廉价），
进程退出或测试清理时调用 `close()` 释放子进程/连接。
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Sequence

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.mcp.config import McpSettings, get_mcp_settings
from app.orchestration.tools import ToolCall, ToolSpec
from app.observability.logging import get_logger, log_event
from app.tools.base import ToolExecutionError

logger = get_logger("mcp.client")

SessionFactory = Callable[[], Any]


class McpToolRegistry:
    """通过 MCP 协议发现与调用工具的注册表。"""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        settings: McpSettings | None = None,
    ) -> None:
        self._settings = settings or get_mcp_settings()
        self._factory = session_factory or _factory_for(self._settings)
        self._bridge = _SessionBridge(self._factory, self._settings.call_timeout_seconds)
        self._specs: tuple[ToolSpec, ...] | None = None

    def list_tools(self) -> Sequence[ToolSpec]:
        """发现远端工具；结果在同一次执行内缓存（工具目录变化需重建注册表）。"""

        if self._specs is None:
            result = self._bridge.run(lambda session: session.list_tools())
            self._specs = tuple(_to_spec(tool) for tool in getattr(result, "tools", []))
            log_event(logger, "mcp.discovered", count=len(self._specs))
        return self._specs

    def call(self, request: ToolCall) -> Any:
        arguments = dict(request.arguments or {})
        result = self._bridge.run(
            lambda session: session.call_tool(request.tool_name, arguments)
        )
        return _unwrap(request.tool_name, result)

    def close(self) -> None:
        self._bridge.close()


def build_stdio_session_factory(settings: McpSettings) -> SessionFactory:
    """构造 stdio 会话工厂：以子进程方式启动 `python -m app.mcp.server`。"""

    parameters = StdioServerParameters(
        command=settings.resolved_command(),
        args=["-m", "app.mcp.server"],
    )

    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    return factory


def build_http_session_factory(settings: McpSettings) -> SessionFactory:
    """构造 streamable HTTP 会话工厂：连接已部署的 MCP Server。"""

    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(settings.server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    return factory


def _factory_for(settings: McpSettings) -> SessionFactory:
    if settings.transport == "stdio":
        return build_stdio_session_factory(settings)
    return build_http_session_factory(settings)


def _to_spec(tool: Any) -> ToolSpec:
    schema = getattr(tool, "inputSchema", None)
    return ToolSpec(
        name=str(getattr(tool, "name", "")),
        description=str(getattr(tool, "description", "") or ""),
        input_schema=schema if isinstance(schema, dict) else {},
    )


def _unwrap(tool_name: str, result: Any) -> Any:
    """解析 `app.mcp.server` 的 JSON 文本约定，并把协议内错误还原成异常。"""

    if getattr(result, "isError", False):
        raise ToolExecutionError(f"MCP 工具 {tool_name} 返回错误: {_text(result)}")
    text = _text(result)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict) and payload.get("status") == "failed":
        raise ToolExecutionError(f"MCP 工具 {tool_name} 执行失败: {payload.get('error')}")
    if isinstance(payload, dict) and "output" in payload:
        return payload["output"]
    return payload


def _text(result: Any) -> str:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


class _SessionBridge:
    """在后台线程的事件循环里维持一个 MCP 会话，对同步调用方暴露 `run`。"""

    def __init__(self, factory: SessionFactory, timeout: float) -> None:
        self._factory = factory
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._context: Any = None
        self._lock = threading.Lock()

    def run(self, operation: Callable[[ClientSession], Any], timeout: float | None = None) -> Any:
        with self._lock:
            session = self._ensure_session()
        future = asyncio.run_coroutine_threadsafe(
            _await(operation(session)), self._loop
        )
        return future.result(timeout if timeout is not None else self._timeout)

    def _ensure_session(self) -> ClientSession:
        if self._session is not None:
            return self._session
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="macp-mcp-bridge", daemon=True
        )
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._open_session(), self._loop)
        self._session = future.result(self._timeout)
        return self._session

    async def _open_session(self) -> ClientSession:
        self._context = self._factory()
        return await self._context.__aenter__()

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            if self._context is not None:
                future = asyncio.run_coroutine_threadsafe(
                    self._context.__aexit__(None, None, None), loop
                )
                future.result(self._timeout)
        except Exception as exc:
            log_event(
                logger,
                "mcp.session_close_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._context = None
            self._session = None
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=self._timeout)
            self._loop = None
            self._thread = None


async def _await(value: Any) -> Any:
    """`operation(session)` 可能返回协程（如 `session.list_tools()`）。"""

    if asyncio.iscoroutine(value):
        return await value
    return value

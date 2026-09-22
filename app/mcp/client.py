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
import os
import shutil
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


# --------------------------------------------------------------------------- #
# 注册表条目 → 会话工厂（`doc/api.md` §5.11、ADR-017）
#
# 与上面两个工厂的区别：参数来自 `mcp_server_registry` 的**行**（用户配置的多 Server），
# 而不是全局 `MCP_*` 环境变量。编排层的默认传输仍由 `app/mcp/registry.py` 决定。
# --------------------------------------------------------------------------- #


class McpTransportUnsupported(RuntimeError):
    """该条目当前建立不了连接：transport 没有客户端实现，或 stdio 的 command 不可用。

    两种情况在调用方是同一件事——「这条 Server 现在连不上」，因此共用一种错误：
    `discover` 归一化为 502 `MCP_DISCOVERY_FAILED`，合并进工具目录时跳过并记
    `registry.server_skipped`（`doc/api.md` §5.11）。
    """


_SERVER_INFO_ATTRIBUTE = "_macp_server_info"
"""握手返回的 `serverInfo` 在会话对象上的暂存名。

MCP SDK 的 `ClientSession.initialize()` 只把 `InitializeResult` 作为返回值，
不在会话上保留；配置页需要展示 Server 自称的名称与版本，所以在工厂里显式暂存。
"""


def _capture_server_info(session: ClientSession, result: Any) -> None:
    info = getattr(result, "serverInfo", None)
    payload: dict[str, Any] = {}
    for key in ("name", "title", "version"):
        value = getattr(info, key, None)
        if isinstance(value, str):
            payload[key] = value
    setattr(session, _SERVER_INFO_ATTRIBUTE, payload)


def build_stdio_session_factory_for(
    *,
    command: str,
    args: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> SessionFactory:
    parameters = StdioServerParameters(
        command=_resolve_stdio_command(command),
        args=list(args or []),
        env=dict(env) if env else None,
        cwd=cwd,
    )

    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                _capture_server_info(session, await session.initialize())
                yield session

    return factory


def _resolve_stdio_command(command: str) -> str:
    """把 stdio 条目的 `command` 解析成绝对路径（`doc/api.md` §5.11）。

    裸名（`python` / `npx`）在这里按 `PATH` 解析**一次**并固定下来：子进程启动不再
    依赖当时的 `PATH`，失败现象也从子进程里的 `ModuleNotFoundError: No module named
    'mcp'`（看不出是解释器选错了）变成明确说出「找不到可执行文件 + 写绝对路径」。
    """

    resolved = (command or "").strip()
    if not resolved:
        raise McpTransportUnsupported("stdio 条目的 command 为空，无法建立连接")
    if os.sep in resolved or (os.altsep and os.altsep in resolved):
        return resolved
    found = shutil.which(resolved)
    if found is None:
        raise McpTransportUnsupported(
            f"找不到可执行文件：{resolved}；stdio 条目的 command 需要是绝对路径，"
            "或位于启动 backend 那个进程的 PATH 中（容器内可用 /app/.venv/bin/python）"
        )
    return found


def build_streamable_http_session_factory_for(
    *, url: str, headers: dict[str, str] | None = None
) -> SessionFactory:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        from mcp.client.streamable_http import streamablehttp_client

        # 校验放在**打开会话时**，不是构造工厂时：合并工具目录那条路径要"零 IO"
        # （ADR-026），构造期做 DNS 会把目录变成网络可用性的函数。
        _ensure_egress_allowed(url, transport="http")
        async with streamablehttp_client(url, headers=dict(headers or {})) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                _capture_server_info(session, await session.initialize())
                yield session

    return factory


def build_sse_session_factory_for(
    *, url: str, headers: dict[str, str] | None = None
) -> SessionFactory:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        from mcp.client.sse import sse_client

        _ensure_egress_allowed(url, transport="sse")
        async with sse_client(url, headers=dict(headers or {})) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                _capture_server_info(session, await session.initialize())
                yield session

    return factory


def _ensure_egress_allowed(url: str, *, transport: str) -> None:
    """远程 MCP Server 也要过出网策略（ADR-034）。

    拒绝复用既有语义：`McpTransportUnsupported` → `discover` 归一化为 502、
    合并工具目录时跳过并记日志。**注意**：这里只校验 URL（解析 + 私网判定），
    MCP SDK 自己建连接时仍会再解析一次 DNS——策略还没法把连接钉死在那个 IP 上
    （SDK 不接受自定义 httpx transport），这段残余窗口记在 ADR-034 的修订里。
    """

    from app.security.egress import EgressDenied, get_egress_policy

    try:
        get_egress_policy().evaluate(url, purpose="mcp")
    except EgressDenied as exc:
        raise McpTransportUnsupported(
            f"{transport} MCP Server 被出网策略拒绝（{exc.reason}）：{exc.detail}"
        ) from exc


def build_registry_session_factory(entry: dict[str, Any]) -> SessionFactory:
    """按注册表条目构造会话工厂。

    `ws` 目前没有官方客户端实现，显式抛 `McpTransportUnsupported`，
    而不是静默退回别的传输（避免把用户配的地址连错）。
    """

    transport = str(entry.get("transport") or "")
    if transport == "stdio":
        return build_stdio_session_factory_for(
            command=str(entry.get("command") or ""),
            args=entry.get("args") or [],
            env=entry.get("env") or {},
            cwd=entry.get("cwd"),
        )
    if transport == "http":
        return build_streamable_http_session_factory_for(
            url=str(entry.get("url") or ""), headers=entry.get("headers") or {}
        )
    if transport == "sse":
        return build_sse_session_factory_for(
            url=str(entry.get("url") or ""), headers=entry.get("headers") or {}
        )
    raise McpTransportUnsupported(
        f"transport={transport or '未设置'} 暂不支持连接，请改用 stdio / http / sse"
    )


def discover_registry_server(
    entry: dict[str, Any], *, timeout: float = 15.0
) -> dict[str, Any]:
    """连接注册表条目、握手并列出工具，返回 `{server_info, tools}`。

    这是 `POST /api/v1/config/mcp/servers/{id}/discover` 的协议侧实现；
    落库与错误码映射由上层负责。工具条目保留 `input_schema`，
    由目录接口决定是否下发（§5.11 的紧凑目录默认不内联）。
    """

    factory = build_registry_session_factory(entry)
    bridge = _SessionBridge(factory, timeout)
    try:
        result = bridge.run(lambda session: session.list_tools(), timeout=timeout)
        tools = [
            {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": _input_schema(tool),
            }
            for tool in getattr(result, "tools", [])
        ]
        server_info = getattr(bridge.session, _SERVER_INFO_ATTRIBUTE, {}) or {}
        return {"server_info": dict(server_info), "tools": tools}
    finally:
        bridge.close()


def _input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    return schema if isinstance(schema, dict) else {}



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

    @property
    def session(self) -> ClientSession | None:
        """已建立的会话；未建立时为 None（供发现流程读取握手信息）。"""

        return self._session

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

"""MCP 工具注册表接入点（成员 C D7-8，契约见 ADR-009；注册条目接入见 ADR-017）。

`build_tool_registry()` 是编排层 `default_tool_registry()` 的默认解析目标：
流水线（`app/workflows/pipeline.py` 的阶段活动）**无需改动**即可发现并调用这里的工具。

职责分工：

- 工具清单与实现 → `app/tools`；
- MCP 协议适配（Server/Client）→ `app/mcp/server.py`、`app/mcp/client.py`；
- 「已注册 MCP Server」的配置与发现缓存 → `app/core/mcp_registry.py`；
- 本模块负责**选择接入方式**（`MCP_TRANSPORT`）、**合并已注册 Server 的工具**，
  并给注册表套上指标/追踪/日志。

接入方式（`app/mcp/config.py`）：

- `inprocess`（默认）：进程内直接调用，注册表来自 `app/tools`；
- `stdio`：通过 MCP 客户端子进程调用 `python -m app.mcp.server`；
- `http`：调用已部署的 MCP Server。

在接入方式之上，配置页注册的 MCP Server 也可以并入同一个注册表
（`MCP_INCLUDE_REGISTERED_SERVERS`，默认关闭）：

- 工具目录取自各条目的**最近一次发现缓存**（`discovered`），因此列目录**不建连接**
  —— 与 `app/core/mcp_registry.py` 的「目录是配置的函数」保持一致；
- 连接在**首次调用**该 Server 的工具时才惰性建立并复用；
- 名字冲突时先到者胜（内置工具优先，其后按 server id 升序）；
- 不支持连接的 transport（如 `ws`）在合并时就被跳过，不对外暴露调不通的工具；
- 读取注册条目本身需要连配置库，因此放在**有界超时**的后台线程里并按进程缓存：
  配置库不可达时只是「没有额外工具」，不会拖住阶段活动或 `GET /api/v1/tools`。

注册表按进程缓存（ADR-009 要求 `build_tool_registry()` 保持廉价、不重复建连接）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any, Callable

from app.mcp.client import (
    McpToolRegistry,
    McpTransportUnsupported,
    build_http_session_factory,
    build_registry_session_factory,
    build_stdio_session_factory,
)
from app.mcp.config import McpSettings, get_mcp_settings
from app.observability.logging import get_logger, log_event
from app.observability.metrics import get_metrics_collector, record_tool_call
from app.observability.tracing import record_exception, span
from app.orchestration.tools import ToolCall, ToolSpec
from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.registry import BuiltinToolRegistry, build_builtin_registry
from app.tools.session_files import session_file_tools

logger = get_logger("mcp.registry")

REGISTERED_SERVERS_LOAD_TIMEOUT_SECONDS = 2.0
"""读取 `mcp_server_registry` 的超时上限。

配置库不可达时 SQLAlchemy 建连会一直等在 `select` 上（不是抛错），
所以这次查询放在后台线程里并设上限；超时一律按「没有额外工具」处理。
读取结果按进程缓存，因此这个上限每个进程最多付一次。
"""

_FALLBACK_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


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


class CompositeToolRegistry:
    """把接入方式注册表与「已注册 MCP Server」的工具合并成一个 `ToolRegistry`。

    合并的只是**目录**：`list_tools()` 完全由配置与发现缓存推导，不建立任何连接。
    真正要调用某个 Server 的工具时才惰性建立会话，并且同一 Server 只建一次。
    """

    def __init__(
        self,
        base: Any,
        servers: Sequence[dict[str, Any]],
        *,
        settings: McpSettings | None = None,
    ) -> None:
        self._base = base
        self._settings = settings or get_mcp_settings()
        self._base_names = {spec.name for spec in base.list_tools()}
        self._factories: dict[str, Any] = {}
        self._owner: dict[str, str] = {}
        self._clients: dict[str, McpToolRegistry] = {}
        self._lock = threading.Lock()
        self._specs = self._collect(servers)

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def call(self, request: ToolCall) -> Any:
        if request.tool_name in self._base_names:
            return self._base.call(request)
        server_id = self._owner.get(request.tool_name)
        if server_id is None:
            # 模型请求了未注册的工具：原样重试同样找不到（ADR-009 修订 3）。
            raise ToolExecutionError(
                f"未注册的工具: {request.tool_name}", retryable=False
            )
        return self._client_for(server_id).call(request)

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
        closer = getattr(self._base, "close", None)
        if callable(closer):
            closer()

    # -- 内部 --------------------------------------------------------------- #

    def _collect(self, servers: Sequence[dict[str, Any]]) -> tuple[ToolSpec, ...]:
        """由发现缓存推导目录；调不通的 Server 在合并阶段就被排除。"""

        specs = list(self._base.list_tools())
        for row in servers:
            server_id = str(row.get("id") or "")
            if not server_id:
                continue
            if row.get("enabled") is False:
                # 加载器（`list_mcp_servers(enabled=True)`）已过滤，但 `server_rows`
                # 注入路径绕过了它，这里兜底：停用的 Server 一律不合成。
                continue
            cached = _discovered_tools(row)
            if cached is None:
                continue
            try:
                factory = build_registry_session_factory(row)
            except McpTransportUnsupported as exc:
                log_event(
                    logger,
                    "registry.server_skipped",
                    server_id=server_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                continue

            names, schemas, descriptions = cached
            options = row.get("tool_options") or {}
            added: list[str] = []
            for name in names:
                if name in self._base_names or name in self._owner:
                    log_event(
                        logger,
                        "registry.tool_name_conflict",
                        server_id=server_id,
                        tool_name=name,
                    )
                    continue
                if _tool_disabled(options, name):
                    continue
                self._owner[name] = server_id
                self._factories[server_id] = factory
                added.append(name)
                specs.append(
                    ToolSpec(
                        name=name,
                        description=str(descriptions.get(name) or ""),
                        input_schema=_normalized_schema(schemas.get(name)),
                    )
                )
            log_event(
                logger,
                "registry.server_merged",
                server_id=server_id,
                tools=len(added),
            )
        return tuple(specs)

    def _client_for(self, server_id: str) -> McpToolRegistry:
        with self._lock:
            client = self._clients.get(server_id)
            if client is None:
                client = McpToolRegistry(
                    self._factories[server_id], settings=self._settings
                )
                self._clients[server_id] = client
            return client


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


def build_tool_registry(
    settings: McpSettings | None = None,
    *,
    server_rows: Sequence[dict[str, Any]] | None = None,
) -> Any:
    """按 `MCP_TRANSPORT` 构建注册表；同一进程内复用同一实例。

    `server_rows` 供测试或已持有条目的调用方直接注入注册条目——传入时不再连配置库，
    也不受 `MCP_INCLUDE_REGISTERED_SERVERS` 开关影响（开关只决定**默认**是否读取）。
    """

    global _registry
    if _registry is not None:
        return _registry

    resolved = settings or get_mcp_settings()
    base = _build_backend(resolved)
    rows = _resolve_server_rows(resolved, server_rows)
    backend: Any = base
    if rows:
        backend = CompositeToolRegistry(base, rows, settings=resolved)
    _registry = InstrumentedToolRegistry(backend, source=resolved.transport)
    log_event(
        logger,
        "registry.ready",
        transport=resolved.transport,
        tools=len(_registry.list_tools()),
        registered_servers=len(rows),
    )
    return _registry


def reset_tool_registry_cache() -> None:
    """关闭并丢弃缓存注册表（测试与配置热更新使用）。"""

    global _registry
    if _registry is not None:
        _registry.close()
        _registry = None


def reset_registered_servers_cache() -> None:
    """丢弃「已注册 Server 条目」的进程缓存（配置变更后重新读取用）。

    与 `reset_tool_registry_cache()` 分开，是因为条目读取在最好的情况下也要连一次
    配置库：注册表缓存是廉价的，条目缓存不是。
    """

    global _registered_servers_cache
    _registered_servers_cache = None


def _resolve_server_rows(
    settings: McpSettings, server_rows: Sequence[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    if server_rows is not None:
        return [row for row in server_rows if isinstance(row, dict)]
    if not settings.include_registered_servers:
        return []
    return _registered_servers()


def _registered_servers() -> list[dict[str, Any]]:
    """已启用的注册条目；读取有界超时，结果按进程缓存。"""

    global _registered_servers_cache
    if _registered_servers_cache is None:
        _registered_servers_cache = _load_registered_servers()
    return _registered_servers_cache


def _load_registered_servers(
    loader: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """在后台线程里读取注册条目；超时或异常一律退化为「没有额外工具」。"""

    if loader is None:
        # 延迟导入：只在真的开启开关时才引入配置库链路。
        from app.core import checkpoint as checkpoint_module

        def load_from_checkpoint() -> list[dict[str, Any]]:
            return checkpoint_module.list_mcp_servers(enabled=True)

        load = load_from_checkpoint
    else:
        load = loader

    box: list[Any] = []

    def run() -> None:
        try:
            box.append(list(load()))
        except BaseException as exc:  # 配置库不可达/表未建都不该影响工具接入
            box.append(exc)

    thread = threading.Thread(target=run, name="macp-mcp-servers-load", daemon=True)
    thread.start()
    thread.join(REGISTERED_SERVERS_LOAD_TIMEOUT_SECONDS)

    if not box:
        log_event(
            logger,
            "registry.servers_unavailable",
            reason="timeout",
            timeout_seconds=REGISTERED_SERVERS_LOAD_TIMEOUT_SECONDS,
        )
        return []
    result = box[0]
    if isinstance(result, BaseException):
        log_event(
            logger,
            "registry.servers_unavailable",
            reason=f"{type(result).__name__}: {result}",
        )
        return []
    return [row for row in result if isinstance(row, dict)]


def _discovered_tools(
    row: dict[str, Any],
) -> tuple[list[str], dict[str, Any], dict[str, Any]] | None:
    """读取条目里的发现缓存；没有缓存就返回 None（不建连接去现发现）。"""

    discovered = row.get("discovered")
    if not isinstance(discovered, dict):
        return None
    raw_names = discovered.get("tool_names")
    if not isinstance(raw_names, list):
        return None
    names = [str(name) for name in raw_names if str(name).strip()]
    if not names:
        return None
    schemas = discovered.get("tool_schemas")
    descriptions = discovered.get("tool_descriptions")
    return (
        names,
        schemas if isinstance(schemas, dict) else {},
        descriptions if isinstance(descriptions, dict) else {},
    )


def _tool_disabled(options: Any, tool_name: str) -> bool:
    """条目级 `tool_options[tool].disabled` 在目录里也生效（`doc/api.md` §5.11）。"""

    if not isinstance(options, dict):
        return False
    entry = options.get(tool_name)
    return isinstance(entry, dict) and bool(entry.get("disabled", False))


def _normalized_schema(schema: Any) -> dict[str, Any]:
    """发现缓存里缺 Schema 时补一个空对象 Schema。

    `doc/api.md` §5.3 约定目录项带 `input_schema`，`as_openai_tool` 也要求它可解析；
    这里只在缓存没给出可用 Schema 时兜底，不伪造字段。
    """

    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return dict(_FALLBACK_SCHEMA)


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
_registered_servers_cache: list[dict[str, Any]] | None = None


__all__ = [
    "BuiltinToolRegistry",
    "CompositeToolRegistry",
    "InstrumentedToolRegistry",
    "SessionFileRegistry",
    "build_tool_registry",
    "reset_registered_servers_cache",
    "reset_tool_registry_cache",
    "tool_catalog",
    "with_session_files",
]

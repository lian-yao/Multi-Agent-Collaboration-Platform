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

用户在配置页登记的 Server（`mcp_server_registry` 表）由 `RegistryServersToolRegistry`
叠在上面（ADR-026）。两者是正交的：上面三种方式决定「平台自己的工具怎么暴露」，
登记表决定「用户另外挂了哪些 Server」，合并后才是 Agent 真正能用的工具集。

注册表按进程缓存（ADR-009 要求 `build_tool_registry()` 保持廉价、不重复建连接）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

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


class RegistryServersToolRegistry:
    """把 `mcp_server_registry` 里启用的 Server 合成进工具集（ADR-026）。

    背景：配置页允许用户登记外部 MCP Server 并「发现」它们的工具，但在此之前那份
    注册表**只有 API 层在读**——编排层拿到的永远是内置的那几个工具，用户在界面上
    看到的工具和 Agent 实际能用的工具是两回事。

    为什么是**合成**而不是 `build_tool_registry()` 里的一个分支：`MCP_TRANSPORT`
    选的是「平台自己怎么暴露内置工具」（进程内 / 子进程 / 远端），与「用户另外登记了
    哪些 Server」是两件正交的事，两者要能同时成立。

    为什么**目录取自已发现的缓存、不重新握手**：与 §5.11 同一条语义——「目录是配置的
    函数」。用户点过「发现」之后目录就定了；Server 离线时它的工具仍在目录里，真被调用
    才报连接错误。好处是列工具不产生任何超时——而流水线**每次执行**都要列一次工具。

    条目本身也来自**进程内快照**（`registry_server_entries`），配置面变更时主动刷新：
    这条路径上不能有 IO，见 `_registry_servers` 的注解。

    为什么**名字冲突时内置优先**：内置工具是平台自身能力，被用户登记的 Server 顶掉
    属于静默改变既有行为；冲突记一条日志，用户在配置页看得见自己那个工具没生效。

    `tool_options.disabled` 在这里生效（停用的工具不进目录）。`allowAutoExecution`
    仍然不消费——它的语义是「要不要人工确认」，而人工确认（HITL）还没做，
    这里不假装支持（详见 ADR-026）。
    """

    def __init__(self, base: Any, *, settings: McpSettings) -> None:
        self._base = base
        self._settings = settings
        self._clients: dict[str, McpToolRegistry] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        self._disabled: set[str] = set()
        self._reported: set[str] = set()

    def list_tools(self) -> tuple[ToolSpec, ...]:
        base_specs = list(self._base.list_tools())
        seen = {spec.name for spec in base_specs}
        specs = list(base_specs)
        self._entries.clear()
        self._owners.clear()
        self._disabled.clear()
        for entry in registry_server_entries():
            server_id = str(entry.get("id"))
            self._entries[server_id] = entry
            tools, disabled = _entry_tools(entry)
            self._disabled.update(disabled)
            for spec in tools:
                if spec.name in seen:
                    self._note_clash(server_id, spec.name)
                    continue
                self._owners[spec.name] = server_id
                seen.add(spec.name)
                specs.append(spec)
        log_event(
            logger,
            "mcp.registry_servers_listed",
            servers=len(self._entries),
            tools=len(specs),
            from_servers=len(self._owners),
            disabled=len(self._disabled),
        )
        return tuple(specs)

    def call(self, request: ToolCall) -> Any:
        if request.tool_name not in self._owners and request.tool_name not in self._disabled:
            # 归属未知：先按目录刷新一次（只读表，不连接）才知道这个名字归谁。
            self.list_tools()
        if request.tool_name in self._disabled:
            raise ToolExecutionError(
                f"MCP 工具 {request.tool_name} 已在配置里停用（tool_options.disabled）"
            )
        owner = self._owners.get(request.tool_name)
        if owner is None:
            # 目录里没有的名字交给基础注册表，由它按既有方式报「无此工具」。
            return self._base.call(request)
        client = self._client_for(owner)
        if client is None:
            raise ToolExecutionError(
                f"MCP 工具 {request.tool_name} 所属的 Server({owner}) 连不上"
            )
        return client.call(request)

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不该盖住正常退出
                log_event(
                    logger,
                    "mcp.registry_server_close_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
        self._clients.clear()
        closer = getattr(self._base, "close", None)
        if callable(closer):
            closer()

    def _client_for(self, server_id: str) -> McpToolRegistry | None:
        client = self._clients.get(server_id)
        if client is not None:
            return client
        entry = self._entries.get(server_id)
        if entry is None:
            return None
        try:
            factory = build_registry_session_factory(entry)
        except (McpTransportUnsupported, ValueError) as exc:
            self._note_unusable(server_id, f"{type(exc).__name__}: {exc}")
            return None
        client = McpToolRegistry(factory, settings=self._settings)
        self._clients[server_id] = client
        return client

    def _note_clash(self, server_id: str, tool_name: str) -> None:
        key = f"clash:{server_id}:{tool_name}"
        if key in self._reported:
            return
        self._reported.add(key)
        log_event(
            logger,
            "mcp.registry_tool_name_clash",
            server_id=server_id,
            tool_name=tool_name,
        )

    def _note_unusable(self, server_id: str, error: str) -> None:
        """配置本身就用不了（传输不支持 / 字段非法）——每次都会走到这里，只报一次。"""

        key = f"unusable:{server_id}"
        if key in self._reported:
            return
        self._reported.add(key)
        log_event(logger, "mcp.registry_server_unusable", server_id=server_id, error=error)


def _entry_tools(entry: dict[str, Any]) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
    """把一条注册表记录的**已发现**结果展开成 (可用工具, 被停用的工具名)。

    只读 `discovered` 缓存，不连接。没发现过（或发现失败）的 Server 在这里贡献 0 个工具
    ——用户在配置页点一次「发现」就有了，与 §5.11 目录的前置条件是同一套。
    """

    discovered = entry.get("discovered")
    if not isinstance(discovered, dict):
        return (), ()
    names = discovered.get("tool_names")
    if not isinstance(names, list):
        return (), ()
    descriptions = discovered.get("tool_descriptions")
    descriptions = descriptions if isinstance(descriptions, dict) else {}
    schemas = discovered.get("tool_schemas")
    schemas = schemas if isinstance(schemas, dict) else {}
    options = entry.get("tool_options")
    options = options if isinstance(options, dict) else {}

    specs: list[ToolSpec] = []
    disabled: list[str] = []
    for raw_name in names:
        tool_name = str(raw_name)
        if bool((options.get(tool_name) or {}).get("disabled", False)):
            disabled.append(tool_name)
            continue
        schema = schemas.get(tool_name)
        specs.append(
            ToolSpec(
                name=tool_name,
                description=str(descriptions.get(tool_name) or ""),
                input_schema=schema if isinstance(schema, dict) else {},
            )
        )
    return tuple(specs), tuple(disabled)


_registry_servers: tuple[dict[str, Any], ...] | None = None
"""注册表 Server 的条目快照；`None` 表示还没加载过。

**为什么不每次现读表**：这条路径在每次执行时都会被走到（枚举工具）。
存储不可达时一次 TCP 超时要几十秒，而「读不到配置」的正确含义是「没有额外工具」，
不是「整条流水线卡住」——所以只加载一次，之后热路径纯内存。
配置变更由配置面调 `refresh_registry_server_entries()` 主动告知。
"""


def registry_server_entries() -> list[dict[str, Any]]:
    """当前生效的注册表 Server 条目（纯内存，不产生 IO）。

    首次调用会尝试加载一次；失败就当作「没有额外 Server」并且**不再重试**。
    """

    if _registry_servers is None:
        refresh_registry_server_entries()
    return list(_registry_servers or ())


def refresh_registry_server_entries(
    rows: Sequence[dict[str, Any]] | None = None,
) -> int:
    """重新读一次注册表并替换快照，返回生效条数。

    由配置面调用：Server 的增删改与「发现」之后（`app/core/mcp_registry.py`），
    以及列出配置时——那种情况下它可以把**已经读到的行**直接传进来，省一次查询。

    读不到就置空：配置面故障的正确含义是「没有额外工具」，不是拦路虎。
    """

    global _registry_servers
    if rows is None:
        try:
            from app.core import checkpoint

            rows = checkpoint.list_mcp_servers(enabled=True)
        except Exception as exc:  # noqa: BLE001 - 见上
            log_event(
                logger,
                "mcp.registry_servers_unavailable",
                error=f"{type(exc).__name__}: {exc}",
            )
            _registry_servers = ()
            return 0
    _registry_servers = tuple(
        row
        for row in rows
        if isinstance(row.get("id"), str) and row.get("enabled", True)
    )
    return len(_registry_servers)


def reset_registry_server_cache() -> None:
    """丢弃快照，让下一次读取重新加载（测试用）。"""

    global _registry_servers
    _registry_servers = None


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
    """按 `MCP_TRANSPORT` 构建注册表，再叠上用户登记的 MCP Server；进程内复用。

    `build_tool_registry()` 仍然廉价：**不读注册表、不连接任何 Server**。登记进来的
    Server 要等到真的列工具时才按目录展开（`RegistryServersToolRegistry`），
    所以这里可以照旧按进程缓存（ADR-009）。

    注册表条目变化（新增 / 停用 / 重新发现）**不需要**失效这个缓存——
    目录是每次列工具时现读的。
    """

    global _registry
    if _registry is not None:
        return _registry

    resolved = settings or get_mcp_settings()
    base = _build_backend(resolved)
    _registry = InstrumentedToolRegistry(
        RegistryServersToolRegistry(base, settings=resolved),
        source=resolved.transport,
    )
    log_event(
        logger,
        "registry.ready",
        transport=resolved.transport,
        tools=_base_tool_count(base),
        registry_servers="auto",
    )
    return _registry


def _base_tool_count(base: Any) -> int:
    """基础后端的工具数。

    只数基础后端：注册表里的 Server 要等到真的列工具时才连。探测失败按 0 记并留日志，
    不让一条观测语句把注册表构建整个带崩。
    """

    try:
        return len(base.list_tools())
    except Exception as exc:  # noqa: BLE001 - 见上
        log_event(
            logger,
            "mcp.registry_probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return 0


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
    "RegistryServersToolRegistry",
    "SessionFileRegistry",
    "build_tool_registry",
    "refresh_registry_server_entries",
    "registry_server_entries",
    "reset_registry_server_cache",
    "reset_tool_registry_cache",
    "tool_catalog",
    "with_session_files",
]

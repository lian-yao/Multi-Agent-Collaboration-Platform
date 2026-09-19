"""MCP Server 注册表与工具目录（`doc/api.md` §5.11、ADR-017 §6）。

职责边界：

- 表结构 → `app/core/checkpoint.py::McpServerRecord`；
- 协议连接与握手 → `app/mcp/client.py`（本模块只调用 `discover_registry_server`）；
- 校验、发现缓存、紧凑目录 → 本模块；
- HTTP 契约与错误码映射 → `app/api/main.py`。

设计要点：

1. **目录是配置的函数**：`discovered` 缓存最近一次发现结果，Server 离线时其工具仍出现在
   目录里，`available=true`；调用期失败报连接错误，而不是「无此工具」。
2. **紧凑优先**：`GET /api/v1/config/mcp/tools` 默认不下发 `input_schema`
   （体积大且不影响「这个工具要不要开」的判断），需要时走 §5.3 的 `GET /api/v1/tools`。
3. **发现只写缓存**：不修改 `enabled`，不改动用户配置。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.mcp.client import (
    McpTransportUnsupported,
    discover_registry_server,
)
from app.observability.logging import get_logger, log_event

logger = get_logger("core.mcp_registry")

SERVER_ID_MAX_LENGTH = 50
SERVER_NAME_MAX_LENGTH = 100
COMMAND_MAX_LENGTH = 500
URL_MAX_LENGTH = 500
CWD_MAX_LENGTH = 500
ARGS_MAX_ITEMS = 64
HEADERS_MAX_ITEMS = 20
HEADER_VALUE_MAX_LENGTH = 500

TRANSPORTS = ("stdio", "http", "sse", "ws")
STDIO_TRANSPORTS = frozenset({"stdio"})
REMOTE_TRANSPORTS = frozenset({"http", "sse", "ws"})

SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

TOOL_OPTION_KEYS = ("disabled", "allowAutoExecution")

DISCOVERY_TIMEOUT_SECONDS = 15.0

__all__ = [
    "McpDiscoveryError",
    "McpRegistryError",
    "McpServerNotFoundError",
    "TRANSPORTS",
    "compact_tool_catalog",
    "create_server",
    "delete_server",
    "discover_server",
    "get_server",
    "list_servers",
    "update_server",
]


class McpRegistryError(ValueError):
    """取值不合法（API 层据此返回 422）。"""


class McpServerNotFoundError(McpRegistryError):
    """条目不存在（API 层据此返回 404）。"""

    code = "MCP_SERVER_NOT_FOUND"

    def __init__(self, server_id: str) -> None:
        super().__init__(f"MCP Server 不存在：{server_id}")
        self.server_id = server_id


class McpDiscoveryError(RuntimeError):
    """发现失败（API 层据此返回 502 `MCP_DISCOVERY_FAILED`）。"""


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _validate_identifier(value: Any, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise McpRegistryError(f"{field} 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise McpRegistryError(f"{field} 不能为空")
    if len(resolved) > max_length:
        raise McpRegistryError(f"{field} 不能超过 {max_length} 个字符")
    if not SERVER_ID_PATTERN.match(resolved):
        raise McpRegistryError(f"{field} 只能包含字母、数字、点、下划线与短横线")
    return resolved


def _validate_label(value: Any) -> str:
    if not isinstance(value, str):
        raise McpRegistryError("name 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise McpRegistryError("name 不能为空")
    if len(resolved) > SERVER_NAME_MAX_LENGTH:
        raise McpRegistryError(f"name 不能超过 {SERVER_NAME_MAX_LENGTH} 个字符")
    return resolved


def _validate_transport(value: Any) -> str:
    if not isinstance(value, str):
        raise McpRegistryError("transport 必须是字符串")
    resolved = value.strip()
    if resolved not in TRANSPORTS:
        raise McpRegistryError(f"transport 必须是 {' / '.join(TRANSPORTS)}")
    return resolved


def _validate_optional_text(value: Any, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpRegistryError(f"{field} 必须是字符串")
    resolved = value.strip()
    if not resolved:
        return None
    if len(resolved) > max_length:
        raise McpRegistryError(f"{field} 不能超过 {max_length} 个字符")
    return resolved


def _validate_string_list(value: Any, *, field: str, max_items: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise McpRegistryError(f"{field} 必须是数组")
    if len(value) > max_items:
        raise McpRegistryError(f"{field} 最多 {max_items} 项")
    resolved: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise McpRegistryError(f"{field} 的每一项必须是字符串")
        resolved.append(item)
    return resolved


def _validate_string_map(value: Any, *, field: str, max_items: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpRegistryError(f"{field} 必须是对象")
    if len(value) > max_items:
        raise McpRegistryError(f"{field} 最多 {max_items} 项")
    resolved: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise McpRegistryError(f"{field} 的键必须是非空字符串")
        if not isinstance(item, str):
            raise McpRegistryError(f"{field} 的值必须是字符串")
        if field == "headers" and len(item) > HEADER_VALUE_MAX_LENGTH:
            raise McpRegistryError(
                f"{field} 的值不能超过 {HEADER_VALUE_MAX_LENGTH} 个字符"
            )
        resolved[key.strip()] = item
    return resolved


def _validate_tool_options(value: Any) -> dict[str, dict[str, bool]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpRegistryError("tool_options 必须是对象")
    resolved: dict[str, dict[str, bool]] = {}
    for tool_name, options in value.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise McpRegistryError("tool_options 的键必须是非空字符串")
        if not isinstance(options, dict):
            raise McpRegistryError(f"tool_options[{tool_name}] 必须是对象")
        unknown = set(options) - set(TOOL_OPTION_KEYS)
        if unknown:
            raise McpRegistryError(
                f"tool_options[{tool_name}] 不支持字段：{'、'.join(sorted(unknown))}"
            )
        entry: dict[str, bool] = {}
        for key in TOOL_OPTION_KEYS:
            if key in options:
                if not isinstance(options[key], bool):
                    raise McpRegistryError(f"tool_options[{tool_name}].{key} 必须是布尔值")
                entry[key] = options[key]
        resolved[tool_name.strip()] = entry
    return resolved


def _validate_transport_fields(
    transport: str,
    *,
    command: Any,
    args: Any,
    env: Any,
    cwd: Any,
    url: Any,
    headers: Any,
) -> dict[str, Any]:
    """传输方式与参数字段必须匹配，避免配出「连不上但看不出来」的条目。"""

    resolved_command = _validate_optional_text(
        command, field="command", max_length=COMMAND_MAX_LENGTH
    )
    resolved_cwd = _validate_optional_text(cwd, field="cwd", max_length=CWD_MAX_LENGTH)
    resolved_url = _validate_optional_text(url, field="url", max_length=URL_MAX_LENGTH)
    resolved_args = _validate_string_list(args, field="args", max_items=ARGS_MAX_ITEMS)
    resolved_env = _validate_string_map(env, field="env", max_items=HEADERS_MAX_ITEMS)
    resolved_headers = _validate_string_map(
        headers, field="headers", max_items=HEADERS_MAX_ITEMS
    )

    if transport in STDIO_TRANSPORTS:
        if not resolved_command:
            raise McpRegistryError("transport=stdio 时必须提供 command")
        if resolved_url:
            raise McpRegistryError("transport=stdio 时不能提供 url")
    else:
        if not resolved_url:
            raise McpRegistryError(f"transport={transport} 时必须提供 url")
        if transport in {"http", "sse"}:
            if not resolved_url.startswith(("http://", "https://")):
                raise McpRegistryError(f"transport={transport} 的 url 必须是 http(s) 地址")
        if transport == "ws" and not resolved_url.startswith(("ws://", "wss://")):
            raise McpRegistryError("transport=ws 的 url 必须是 ws(s) 地址")
        if resolved_command or resolved_args or resolved_env or resolved_cwd:
            raise McpRegistryError(
                f"transport={transport} 不能同时提供 command/args/env/cwd"
            )

    return {
        "command": resolved_command,
        "args": resolved_args,
        "env": resolved_env,
        "cwd": resolved_cwd,
        "url": resolved_url,
        "headers": resolved_headers,
    }


# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #


def _discovered_summary(discovered: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(discovered, dict):
        return {"tool_count": 0, "discovered_at": None, "server_info": None}
    tool_names = discovered.get("tool_names")
    return {
        "tool_count": len(tool_names) if isinstance(tool_names, list) else 0,
        "discovered_at": discovered.get("discovered_at"),
        "server_info": discovered.get("server_info"),
    }


def _server_view(row: dict[str, Any]) -> dict[str, Any]:
    summary = _discovered_summary(row.get("discovered"))
    return {
        "id": row["id"],
        "name": row["name"],
        "transport": row["transport"],
        "command": row.get("command"),
        "args": row.get("args") or [],
        "env": row.get("env") or {},
        "cwd": row.get("cwd"),
        "url": row.get("url"),
        "headers": row.get("headers") or {},
        "enabled": row["enabled"],
        "tool_options": row.get("tool_options") or {},
        "tool_count": summary["tool_count"],
        "discovered_at": summary["discovered_at"],
        "server_info": summary["server_info"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


def _sync_orchestration_tools(rows: list[dict[str, Any]] | None = None) -> None:
    """把登记表的变更告诉编排层（ADR-026）。

    编排层不读数据库——工具枚举在**每次执行**时都会发生，那条路径上不能有 IO
    （见 `app/mcp/registry.py` 里 `_registry_servers` 的注解）。所以配置面必须在
    改动之后主动刷新它，否则用户新建的 Server 要等进程重启才生效。

    延迟 import，且失败只记日志：登记表已经改完了，通知不到不该让配置保存本身报错。
    """

    try:
        from app.mcp.registry import refresh_registry_server_entries
    except ModuleNotFoundError:  # pragma: no cover - app.mcp 缺失时的既有回退
        return
    refresh_registry_server_entries(rows)


def list_servers(*, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    server_rows = checkpoint.list_mcp_servers() if rows is None else rows
    items = [_server_view(row) for row in server_rows]
    # 顺带同步一次：用户打开「工具与配置」就是在看配置，此刻刷新最不容易漏。
    _sync_orchestration_tools(server_rows)
    return {"items": items, "total": len(items)}


def get_server(server_id: str) -> dict[str, Any]:
    row = checkpoint.get_mcp_server(server_id)
    if row is None:
        raise McpServerNotFoundError(server_id)
    return _server_view(row)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def create_server(
    *,
    server_id: Any,
    name: Any,
    transport: Any,
    command: Any = None,
    args: Any = None,
    env: Any = None,
    cwd: Any = None,
    url: Any = None,
    headers: Any = None,
    enabled: Any = True,
    tool_options: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    resolved_id = _validate_identifier(
        server_id, field="id", max_length=SERVER_ID_MAX_LENGTH
    )
    resolved_name = _validate_label(name)
    resolved_transport = _validate_transport(transport)
    if not isinstance(enabled, bool):
        raise McpRegistryError("enabled 必须是布尔值")
    fields = _validate_transport_fields(
        resolved_transport,
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        url=url,
        headers=headers,
    )
    if checkpoint.get_mcp_server(resolved_id) is not None:
        raise McpRegistryError(f"MCP Server id 已存在：{resolved_id}")

    row = checkpoint.create_mcp_server(
        server_id=resolved_id,
        name=resolved_name,
        transport=resolved_transport,
        enabled=enabled,
        tool_options=_validate_tool_options(tool_options),
        updated_by=actor,
        **fields,
    )
    log_event(
        logger,
        "config.mcp_server.created",
        server_id=resolved_id,
        transport=resolved_transport,
        actor=actor,
    )
    _sync_orchestration_tools()
    return _server_view(row)


def update_server(
    server_id: str,
    *,
    name: Any = UNSET,
    transport: Any = UNSET,
    command: Any = UNSET,
    args: Any = UNSET,
    env: Any = UNSET,
    cwd: Any = UNSET,
    url: Any = UNSET,
    headers: Any = UNSET,
    enabled: Any = UNSET,
    tool_options: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    """部分更新；传输方式与参数字段的匹配校验在合并后的取值上进行。"""

    current = checkpoint.get_mcp_server(server_id)
    if current is None:
        raise McpServerNotFoundError(server_id)

    resolved_transport = (
        current["transport"] if transport is UNSET else _validate_transport(transport)
    )
    fields = _validate_transport_fields(
        resolved_transport,
        command=current.get("command") if command is UNSET else command,
        args=current.get("args") if args is UNSET else args,
        env=current.get("env") if env is UNSET else env,
        cwd=current.get("cwd") if cwd is UNSET else cwd,
        url=current.get("url") if url is UNSET else url,
        headers=current.get("headers") if headers is UNSET else headers,
    )
    fields["transport"] = resolved_transport
    if name is not UNSET:
        fields["name"] = _validate_label(name)
    if enabled is not UNSET:
        if not isinstance(enabled, bool):
            raise McpRegistryError("enabled 必须是布尔值")
        fields["enabled"] = enabled
    if tool_options is not UNSET:
        fields["tool_options"] = _validate_tool_options(tool_options)

    row = checkpoint.update_mcp_server(server_id, updated_by=actor, **fields)
    if row is None:
        raise McpServerNotFoundError(server_id)
    log_event(
        logger,
        "config.mcp_server.updated",
        server_id=server_id,
        actor=actor,
        fields=",".join(sorted(fields)) or "none",
    )
    _sync_orchestration_tools()
    return _server_view(row)


def delete_server(server_id: str, *, actor: str | None = None) -> None:
    if not checkpoint.delete_mcp_server(server_id):
        raise McpServerNotFoundError(server_id)
    log_event(logger, "config.mcp_server.deleted", server_id=server_id, actor=actor)
    _sync_orchestration_tools()


# --------------------------------------------------------------------------- #
# 发现与目录
# --------------------------------------------------------------------------- #


def discover_server(
    server_id: str,
    *,
    discoverer: Callable[..., dict[str, Any]] | None = None,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """连接 Server 并缓存工具目录（`doc/api.md` §5.11）。

    `discoverer` 供测试注入，默认走 `app/mcp/client.py::discover_registry_server`。
    任何连接/协议异常都归一化为 `McpDiscoveryError`；发现结果**只**写 `discovered`。
    """

    entry = checkpoint.get_mcp_server(server_id)
    if entry is None:
        raise McpServerNotFoundError(server_id)

    run = discoverer or discover_registry_server
    try:
        result = run(entry, timeout=timeout)
    except McpTransportUnsupported as exc:
        raise McpDiscoveryError(str(exc)) from exc
    except McpDiscoveryError:
        raise
    except Exception as exc:
        raise McpDiscoveryError(
            f"连接 MCP Server 失败：{type(exc).__name__}: {exc}"
        ) from exc

    tools = result.get("tools") or []
    discovered = {
        "server_info": result.get("server_info") or {},
        "tool_names": [str(tool.get("name")) for tool in tools],
        "tool_schemas": {
            str(tool.get("name")): tool.get("input_schema") or {} for tool in tools
        },
        "tool_descriptions": {
            str(tool.get("name")): str(tool.get("description") or "") for tool in tools
        },
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    row = checkpoint.update_mcp_server(server_id, discovered=discovered)
    if row is None:
        raise McpServerNotFoundError(server_id)
    log_event(
        logger,
        "config.mcp_server.discovered",
        server_id=server_id,
        tools=len(discovered["tool_names"]),
    )
    _sync_orchestration_tools()
    return {
        "server_id": server_id,
        "server_info": discovered["server_info"],
        "tools": [
            {
                "name": str(tool.get("name")),
                "description": str(tool.get("description") or ""),
            }
            for tool in tools
        ],
        "total": len(tools),
        "discovered_at": discovered["discovered_at"],
    }


def compact_tool_catalog(
    *, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """按 Server 分组的紧凑工具目录（`doc/api.md` §5.11）。

    不内联 `input_schema`；`available` 表示工具在最近一次发现结果里存在，
    是**配置的函数**而不是实时连接状态。
    """

    server_rows = checkpoint.list_mcp_servers() if rows is None else rows
    items: list[dict[str, Any]] = []
    servers: list[dict[str, Any]] = []
    for row in server_rows:
        discovered = row.get("discovered")
        tool_names = (
            discovered.get("tool_names")
            if isinstance(discovered, dict)
            else None
        ) or []
        descriptions = (
            discovered.get("tool_descriptions")
            if isinstance(discovered, dict)
            else None
        ) or {}
        tool_options = row.get("tool_options") or {}
        for tool_name in tool_names:
            options = tool_options.get(tool_name) or {}
            items.append(
                {
                    "server_id": row["id"],
                    "server_name": row["name"],
                    "enabled": row["enabled"],
                    "name": str(tool_name),
                    "description": str(descriptions.get(tool_name, "")),
                    "tool_enabled": not bool(options.get("disabled", False)),
                    "available": True,
                }
            )
        servers.append(
            {
                "id": row["id"],
                "name": row["name"],
                "transport": row["transport"],
                "enabled": row["enabled"],
                "tool_count": len(tool_names),
                "discovered_at": (
                    discovered.get("discovered_at")
                    if isinstance(discovered, dict)
                    else None
                ),
            }
        )
    items.sort(key=lambda item: (item["server_id"], item["name"]))
    return {"items": items, "total": len(items), "servers": servers}

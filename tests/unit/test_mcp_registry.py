"""MCP Server 注册表、发现与紧凑工具目录（`doc/api.md` §5.11、ADR-017）。

单元级证据：传输方式与参数字段的匹配校验、工具开关、发现结果只写 `discovered`、
紧凑目录**不内联** `input_schema`、失败归一化为 `McpDiscoveryError`。

存储与协议都用替身：`mcp_registry` 只依赖 `checkpoint` 的 dict 读写与一个
可注入的 `discoverer` 回调（默认实现走 stdio/http 真实连接）。
"""

from __future__ import annotations

import pytest

from app.core import checkpoint, mcp_registry
from app.core.checkpoint import UNSET


class FakeStore:
    """复现 `checkpoint` 里 MCP 注册表相关函数的读写语义。"""

    def __init__(self) -> None:
        self.servers: dict[str, dict] = {}

    def install(self, monkeypatch) -> "FakeStore":
        for name in (
            "get_mcp_server",
            "list_mcp_servers",
            "create_mcp_server",
            "update_mcp_server",
            "delete_mcp_server",
        ):
            monkeypatch.setattr(checkpoint, name, getattr(self, name))
        return self

    def get_mcp_server(self, server_id: str) -> dict | None:
        row = self.servers.get(server_id)
        return dict(row) if row else None

    def list_mcp_servers(self, *, enabled=None) -> list[dict]:
        rows = sorted(self.servers.values(), key=lambda r: r["id"])
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is enabled]
        return [dict(row) for row in rows]

    def create_mcp_server(self, *, server_id: str, **fields) -> dict:
        row = {
            "id": server_id,
            "name": fields["name"],
            "transport": fields["transport"],
            "command": fields.get("command"),
            "args": list(fields.get("args") or []),
            "env": dict(fields.get("env") or {}),
            "cwd": fields.get("cwd"),
            "url": fields.get("url"),
            "headers": dict(fields.get("headers") or {}),
            "enabled": fields.get("enabled", True),
            "tool_options": dict(fields.get("tool_options") or {}),
            "discovered": None,
            "created_at": None,
            "updated_at": None,
            "updated_by": fields.get("updated_by"),
        }
        self.servers[server_id] = row
        return dict(row)

    def update_mcp_server(self, server_id: str, **fields) -> dict | None:
        row = self.servers.get(server_id)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not UNSET:
                row[key] = value
        return dict(row)

    def delete_mcp_server(self, server_id: str) -> bool:
        return self.servers.pop(server_id, None) is not None


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    return FakeStore().install(monkeypatch)


def _stdio_server(server_id: str = "filesystem", **overrides) -> dict:
    body = {
        "server_id": server_id,
        "name": overrides.pop("name", "本地文件系统"),
        "transport": overrides.pop("transport", "stdio"),
        "command": overrides.pop("command", "npx"),
        "args": overrides.pop("args", ["-y", "@modelcontextprotocol/server-filesystem"]),
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# 传输方式与参数字段
# --------------------------------------------------------------------------- #


def test_create_stdio_server_keeps_command_and_args(store):
    row = mcp_registry.create_server(**_stdio_server())

    assert row["transport"] == "stdio"
    assert row["command"] == "npx"
    assert row["args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert row["url"] is None
    # 从未发现过：目录里没有工具
    assert row["tool_count"] == 0
    assert row["discovered_at"] is None


def test_create_http_server_keeps_url_and_headers(store):
    row = mcp_registry.create_server(
        server_id="remote",
        name="远端服务",
        transport="http",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer token-1"},
    )

    assert row["url"] == "https://mcp.example.com/mcp"
    assert row["headers"] == {"Authorization": "Bearer token-1"}
    assert row["command"] is None
    assert row["args"] == []


def test_stdio_without_command_is_rejected(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="必须提供 command"):
        mcp_registry.create_server(server_id="s", name="s", transport="stdio")


def test_stdio_with_url_is_rejected(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="不能提供 url"):
        mcp_registry.create_server(
            **_stdio_server(url="https://mcp.example.com")
        )


def test_remote_without_url_is_rejected(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="必须提供 url"):
        mcp_registry.create_server(server_id="s", name="s", transport="http")


def test_remote_with_command_is_rejected(store):
    """远程传输带了 stdio 字段：配得出来但连不上，必须显式拒绝。"""

    with pytest.raises(mcp_registry.McpRegistryError, match="不能同时提供"):
        mcp_registry.create_server(
            server_id="s",
            name="s",
            transport="http",
            url="https://mcp.example.com",
            command="npx",
        )


@pytest.mark.parametrize(
    "transport,url",
    [("http", "ftp://mcp.example.com"), ("sse", "not-a-url"), ("ws", "https://mcp.example.com")],
)
def test_remote_url_scheme_is_validated(store, transport, url):
    with pytest.raises(mcp_registry.McpRegistryError, match="必须是"):
        mcp_registry.create_server(
            server_id="s", name="s", transport=transport, url=url
        )


def test_unknown_transport_is_rejected(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="transport 必须是"):
        mcp_registry.create_server(server_id="s", name="s", transport="grpc")


@pytest.mark.parametrize("server_id", ["", "   ", "has space", "x" * 51])
def test_bad_server_id_is_rejected(store, server_id):
    with pytest.raises(mcp_registry.McpRegistryError):
        mcp_registry.create_server(
            **_stdio_server(server_id=server_id)
        )


def test_duplicate_server_id_is_rejected(store):
    mcp_registry.create_server(**_stdio_server())

    # §5.11 把重复 id 归到 422（与 §5.9 的 Provider 重复不同）
    with pytest.raises(mcp_registry.McpRegistryError, match="已存在"):
        mcp_registry.create_server(**_stdio_server())


def test_switch_transport_must_bring_matching_fields(store):
    """`stdio` → `http` 时旧 command 仍在库里，必须在合并后的取值上校验。"""

    mcp_registry.create_server(**_stdio_server())

    with pytest.raises(mcp_registry.McpRegistryError, match="不能同时提供"):
        mcp_registry.update_server("filesystem", transport="http", url="https://mcp.example.com")


def test_switch_transport_with_cleared_fields_succeeds(store):
    mcp_registry.create_server(**_stdio_server())

    row = mcp_registry.update_server(
        "filesystem",
        transport="http",
        url="https://mcp.example.com",
        command=None,
        args=None,
    )

    assert row["transport"] == "http"
    assert row["command"] is None
    assert row["args"] == []


def test_update_missing_server_is_not_found(store):
    with pytest.raises(mcp_registry.McpServerNotFoundError) as excinfo:
        mcp_registry.update_server("missing", name="x")

    assert excinfo.value.code == "MCP_SERVER_NOT_FOUND"


def test_delete_missing_server_is_not_found(store):
    with pytest.raises(mcp_registry.McpServerNotFoundError):
        mcp_registry.delete_server("missing")


# --------------------------------------------------------------------------- #
# 工具开关
# --------------------------------------------------------------------------- #


def test_tool_options_are_validated(store):
    row = mcp_registry.create_server(
        **_stdio_server(
            tool_options={"read_file": {"disabled": True, "allowAutoExecution": False}}
        )
    )

    assert row["tool_options"] == {
        "read_file": {"disabled": True, "allowAutoExecution": False}
    }


def test_tool_options_reject_unknown_keys(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="不支持字段"):
        mcp_registry.create_server(
            **_stdio_server(tool_options={"read_file": {"autoRun": True}})
        )


def test_tool_options_reject_non_boolean_values(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="必须是布尔值"):
        mcp_registry.create_server(
            **_stdio_server(tool_options={"read_file": {"disabled": "yes"}})
        )


def test_blank_env_key_is_rejected(store):
    with pytest.raises(mcp_registry.McpRegistryError, match="键必须是"):
        mcp_registry.create_server(**_stdio_server(env={"  ": "v"}))


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #


def _discoverer(*, server_info=None, tools=None):
    def discover(entry, *, timeout=None):
        return {
            "server_info": server_info or {"name": "filesystem", "version": "1.0.0"},
            "tools": tools
            or [
                {
                    "name": "read_file",
                    "description": "读取文件内容",
                    "input_schema": {"type": "object", "properties": {"path": {}}},
                }
            ],
        }

    return discover


def test_discover_caches_result_and_reports_tools(store):
    mcp_registry.create_server(**_stdio_server())

    result = mcp_registry.discover_server("filesystem", discoverer=_discoverer())

    assert result["server_id"] == "filesystem"
    assert result["total"] == 1
    assert result["tools"] == [{"name": "read_file", "description": "读取文件内容"}]
    assert result["server_info"]["name"] == "filesystem"
    assert result["discovered_at"]

    row = store.get_mcp_server("filesystem")
    assert row["discovered"]["tool_names"] == ["read_file"]
    assert row["discovered"]["tool_schemas"]["read_file"]["type"] == "object"
    # 发现不改变启用状态
    assert row["enabled"] is True

    view = mcp_registry.get_server("filesystem")
    assert view["tool_count"] == 1
    assert view["discovered_at"] == result["discovered_at"]


def test_discover_unsupported_transport_becomes_discovery_error(store, monkeypatch):
    """`ws` 没有可用客户端实现：显式报错，而不是静默退回别的传输。"""

    from app.mcp.client import McpTransportUnsupported

    mcp_registry.create_server(
        server_id="ws-server", name="WS", transport="ws", url="wss://mcp.example.com"
    )

    def unsupported(entry, *, timeout=None):
        raise McpTransportUnsupported("transport=ws 暂不支持连接")

    with pytest.raises(mcp_registry.McpDiscoveryError, match="ws"):
        mcp_registry.discover_server("ws-server", discoverer=unsupported)


def test_discover_wraps_connection_failure(store):
    mcp_registry.create_server(**_stdio_server())

    def broken(entry, *, timeout=None):
        raise ConnectionRefusedError("no route to host")

    with pytest.raises(mcp_registry.McpDiscoveryError, match="连接 MCP Server 失败"):
        mcp_registry.discover_server("filesystem", discoverer=broken)


def test_discover_keeps_existing_cache_when_refresh_fails(store):
    mcp_registry.create_server(**_stdio_server())
    mcp_registry.discover_server("filesystem", discoverer=_discoverer())
    before = store.get_mcp_server("filesystem")["discovered"]

    def broken(entry, *, timeout=None):
        raise TimeoutError("handshake timed out")

    with pytest.raises(mcp_registry.McpDiscoveryError):
        mcp_registry.discover_server("filesystem", discoverer=broken)

    # 失败不得清空上一次的成功目录
    assert store.get_mcp_server("filesystem")["discovered"] == before


def test_discover_missing_server_is_not_found(store):
    with pytest.raises(mcp_registry.McpServerNotFoundError):
        mcp_registry.discover_server("missing", discoverer=_discoverer())


# --------------------------------------------------------------------------- #
# 紧凑工具目录
# --------------------------------------------------------------------------- #


def test_compact_catalog_omits_input_schema(store):
    mcp_registry.create_server(**_stdio_server())
    mcp_registry.discover_server("filesystem", discoverer=_discoverer())

    catalog = mcp_registry.compact_tool_catalog()

    assert catalog["total"] == 1
    item = catalog["items"][0]
    assert set(item) == {
        "server_id",
        "server_name",
        "enabled",
        "name",
        "description",
        "tool_enabled",
        "available",
    }
    # Schema 体积大且不影响「开不开」的判断：不出现在紧凑目录里
    assert "input_schema" not in str(item)


def test_compact_catalog_reflects_tool_switch(store):
    mcp_registry.create_server(
        **_stdio_server(tool_options={"read_file": {"disabled": True}})
    )
    mcp_registry.discover_server("filesystem", discoverer=_discoverer())

    catalog = mcp_registry.compact_tool_catalog()

    assert catalog["items"][0]["tool_enabled"] is False


def test_compact_catalog_lists_servers_without_discovery(store):
    mcp_registry.create_server(**_stdio_server())
    mcp_registry.create_server(
        server_id="remote", name="远端服务", transport="http", url="https://mcp.example.com"
    )

    catalog = mcp_registry.compact_tool_catalog()

    servers = {server["id"]: server for server in catalog["servers"]}
    assert catalog["items"] == []  # 未发现过 = 目录里没有工具
    assert servers["filesystem"]["tool_count"] == 0
    assert servers["remote"]["tool_count"] == 0
    assert servers["remote"]["transport"] == "http"


def test_compact_catalog_groups_multiple_servers(store):
    mcp_registry.create_server(**_stdio_server())
    mcp_registry.create_server(
        server_id="remote", name="远端服务", transport="http", url="https://mcp.example.com"
    )
    mcp_registry.discover_server("filesystem", discoverer=_discoverer())
    mcp_registry.discover_server(
        "remote",
        discoverer=_discoverer(
            server_info={"name": "remote"},
            tools=[{"name": "search", "description": "检索", "input_schema": {}}],
        ),
    )

    catalog = mcp_registry.compact_tool_catalog()

    assert {(item["server_id"], item["name"]) for item in catalog["items"]} == {
        ("filesystem", "read_file"),
        ("remote", "search"),
    }
    assert catalog["total"] == 2


def test_list_servers_exposes_discovery_summary(store):
    mcp_registry.create_server(**_stdio_server())

    before = mcp_registry.list_servers()["items"][0]
    assert before["tool_count"] == 0
    assert before["server_info"] is None

    mcp_registry.discover_server("filesystem", discoverer=_discoverer())
    after = mcp_registry.list_servers()["items"][0]

    assert after["tool_count"] == 1
    assert after["server_info"] == {"name": "filesystem", "version": "1.0.0"}

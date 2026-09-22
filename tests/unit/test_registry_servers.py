"""用户登记的 MCP Server 如何进入编排层（ADR-026 / `doc/testing.md` §4.6）。

`mcp_server_registry` 表里的 Server 在此之前**只有配置页在读**：用户登记并「发现」了
工具，Agent 那边永远拿不到。这些用例钉住合成层（`CompositeToolRegistry`）的几条边界：

- 目录只读**已发现的缓存**，不重新握手（所以列工具不产生超时）；
- 停用的工具（`tool_options.disabled`）不进目录；
- 名字与内置工具冲突时**内置优先**，不静默顶掉平台自身能力；
- 单个 Server 配错 / 连不上只影响它自己，不拖垮整条流水线；
- 目录是配置的函数：换一版 `server_rows` 重建注册表即可生效，不依赖跨进程缓存失效。

假 Server 通过 `mcp.shared.memory` 与真实 MCP 协议往返，因此调用路径仍是真的
（JSON-RPC + 类型校验 + 返回值解包），只是不启子进程、不开端口。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp.config import McpSettings
from app.mcp.registry import (
    CompositeToolRegistry,
    build_tool_registry,
    reset_registered_servers_cache,
    reset_tool_registry_cache,
)
from app.mcp.server import build_mcp_server
from app.orchestration.tools import ToolCall, ToolSpec
from app.tools import BuiltinToolRegistry, ToolExecutionError

# 假 Server 暴露的就是内置那四个（`app/mcp/server.py`），所以合成用例要用其中的名字，
# 否则「缓存里说有、真调用时没有」——那是配置漂移，不是这里要测的行为。
SERVER_TOOL = "code_execution"

# 真实内置注册表（`app/tools/registry.py::build_builtin_registry`）暴露的工具名，
# 供走 `build_tool_registry`（真实基底）的用例对齐断言。
_BUILTIN_NAMES = ["calculator", "code_execution", "sql_query", "web_search"]


def _entry(
    server_id: str = "extra",
    *,
    enabled: bool = True,
    tool_names: Sequence[str] = (SERVER_TOOL,),
    tool_options: dict[str, Any] | None = None,
    discovered: bool = True,
    transport: str = "stdio",
) -> dict[str, Any]:
    """一条与 `checkpoint.list_mcp_servers()` 同形的注册表记录。"""

    payload = None
    if discovered:
        payload = {
            "server_info": {"name": "fake", "version": "1.0"},
            "tool_names": list(tool_names),
            "tool_schemas": {
                name: {"type": "object", "properties": {}} for name in tool_names
            },
            "tool_descriptions": {name: f"{name} 的说明" for name in tool_names},
            "discovered_at": "2026-09-19T00:00:00+00:00",
        }
    return {
        "id": server_id,
        "name": server_id,
        "transport": transport,
        "command": "python",
        "args": [],
        "env": {},
        "cwd": None,
        "url": None,
        "headers": {},
        "enabled": enabled,
        "tool_options": tool_options or {},
        "discovered": payload,
    }


class _StubRegistry:
    """假「内置注册表」；工具名可配，用来观察合成与冲突处理。

    默认给一个**真 Server 没有**的名字，这样真 Server 的工具都能合成进来；
    要测冲突就传一个与真 Server 同名的名字。
    """

    def __init__(self, *names: str) -> None:
        self._names = names or ("platform_only",)
        self.calls: list[str] = []
        self.closed = False

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=name,
                description=f"{name}（平台自带）",
                input_schema={"type": "object"},
            )
            for name in self._names
        )

    def call(self, request: ToolCall) -> Any:
        self.calls.append(request.tool_name)
        return {"handled_by": "base", "tool": request.tool_name}

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_caches():
    """两个进程级缓存（注册表实例、条目快照）都要在用例之间还原。"""

    reset_tool_registry_cache()
    reset_registered_servers_cache()
    yield
    reset_tool_registry_cache()
    reset_registered_servers_cache()


@pytest.fixture
def fake_server(monkeypatch):
    """把「按条目建会话」换成进程内直连的内置工具 Server（仍是真实 MCP 往返）。"""

    server = build_mcp_server(BuiltinToolRegistry())

    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(server) as session:
            yield session

    monkeypatch.setattr(
        "app.mcp.registry.build_registry_session_factory", lambda entry: factory
    )
    return factory


def _registry(rows: Sequence[dict[str, Any]], base: Any = None) -> CompositeToolRegistry:
    return CompositeToolRegistry(
        base if base is not None else _StubRegistry(),
        rows,
        settings=McpSettings(transport="inprocess"),
    )


def _names(registry: CompositeToolRegistry) -> list[str]:
    return [spec.name for spec in registry.list_tools()]


# --- 目录合成 -------------------------------------------------------------


def test_discovered_tools_are_merged_with_the_builtin_ones(fake_server):
    rows = [_entry("extra", tool_names=(SERVER_TOOL,))]

    assert _names(_registry(rows)) == ["platform_only", SERVER_TOOL]


def test_directory_comes_from_the_discovered_cache_without_connecting(fake_server):
    """没发现过就没有工具 —— 而且**不能**去连（用 ws 证明：它的工厂会直接抛）。"""

    rows = [_entry("extra", discovered=False, transport="ws")]

    assert _names(_registry(rows)) == ["platform_only"]


def test_disabled_tool_is_not_listed_and_says_why_when_called(fake_server):
    rows = [
        _entry(
            "extra",
            tool_names=(SERVER_TOOL,),
            tool_options={SERVER_TOOL: {"disabled": True}},
        )
    ]
    registry = _registry(rows)

    assert _names(registry) == ["platform_only"]
    with pytest.raises(ToolExecutionError) as failure:
        registry.call(ToolCall(call_id="c1", tool_name=SERVER_TOOL, arguments={}))

    # 停用的工具在目录里就没有，直接调用落到「未注册」分支（合成阶段已剔除）。
    assert "未注册" in str(failure.value)


def test_builtin_tool_wins_on_a_name_clash(fake_server):
    base = _StubRegistry(SERVER_TOOL)
    rows = [_entry("extra", tool_names=(SERVER_TOOL, "calculator"))]
    registry = _registry(rows, base)

    names = _names(registry)

    assert names.count(SERVER_TOOL) == 1, "冲突的名字不能出现两次"
    registry.call(ToolCall(call_id="c1", tool_name=SERVER_TOOL, arguments={}))
    assert base.calls == [SERVER_TOOL], "冲突时必须是内置工具处理"


def test_disabled_server_is_skipped(fake_server):
    rows = [_entry("off", enabled=False, tool_names=(SERVER_TOOL,))]

    assert _names(_registry(rows)) == ["platform_only"]


# --- 调用路由 -------------------------------------------------------------


def test_call_is_routed_to_the_registry_server(fake_server):
    rows = [_entry("extra", tool_names=("calculator",))]
    registry = _registry(rows)

    output = registry.call(
        ToolCall(call_id="c1", tool_name="calculator", arguments={"expression": "1+1"})
    )

    assert output == {"expression": "1+1", "value": 2}, "必须走真实 MCP 往返"


def test_unknown_tool_raises_a_clear_error(fake_server):
    """目录里没有、也非内置的名字，直接给明确错误，不静默落到基础注册表。"""

    base = _StubRegistry()
    registry = _registry([], base)

    with pytest.raises(ToolExecutionError) as failure:
        registry.call(ToolCall(call_id="c1", tool_name="not_registered", arguments={}))

    assert "未注册" in str(failure.value)
    assert base.calls == [], "未知工具不该落到基础注册表"


def test_unsupported_transport_is_skipped_at_collect():
    """`ws` 没有客户端实现：合成阶段就跳过，目录不含它的工具。

    注意这里**不**用 `fake_server`——那个 fixture 会把 `build_registry_session_factory`
    换成不检查 transport 的进程内工厂。本用例要验证的正是「transport 检查在合成阶段生效」。
    """

    rows = [_entry("extra", tool_names=(SERVER_TOOL,), transport="ws")]
    registry = _registry(rows)

    assert _names(registry) == ["platform_only"], "调不通的 transport 在合成时就被排除"


def test_unresolvable_stdio_command_is_skipped_at_collect():
    """`command` 在 PATH 里找不到：跳过并留日志，不把整条目录带崩（doc/api.md §5.11）。"""

    row = _entry("extra", tool_names=(SERVER_TOOL,))
    row["command"] = "macp-definitely-missing-binary"

    registry = _registry([row])

    assert _names(registry) == ["platform_only"], "连不上的条目只影响它自己"


def test_registry_table_failure_degrades_to_builtin_tools(monkeypatch):
    """配置面读不到时：流水线照常用内置工具，而且**只尝试一次**，不反复卡超时。"""

    attempts: list[int] = []

    def boom(*, enabled: bool | None = None) -> list[dict[str, Any]]:
        attempts.append(1)
        raise RuntimeError("database is down")

    monkeypatch.setattr("app.core.checkpoint.list_mcp_servers", boom)

    registry = build_tool_registry(
        McpSettings(transport="inprocess", include_registered_servers=True)
    )

    assert _names(registry) == _BUILTIN_NAMES, "读不到注册表时退回纯内置工具"
    assert len(attempts) == 1, "存储不可达只尝试一次，不把超时带进每次工具枚举"


# --- 缓存与生效 -----------------------------------------------------------


def test_directory_follows_the_rows_you_hand_it(fake_server):
    """目录是「传入的条目」的函数：换一版 rows 重建注册表即见新工具。"""

    assert _names(_registry([])) == ["platform_only"]

    rows = [_entry("extra", tool_names=(SERVER_TOOL,))]
    assert _names(_registry(rows)) == ["platform_only", SERVER_TOOL]


def test_entries_are_loaded_once_then_served_from_memory(monkeypatch):
    """热路径不能反复读表：条目读取按进程缓存，换 rows 重建也不该重复读。"""

    reads: list[bool | None] = []

    def counting_list(*, enabled: bool | None = None) -> list[dict[str, Any]]:
        reads.append(enabled)
        return [_entry("extra", tool_names=("custom_tool",))]

    monkeypatch.setattr("app.core.checkpoint.list_mcp_servers", counting_list)

    registry = build_tool_registry(
        McpSettings(transport="inprocess", include_registered_servers=True)
    )
    assert _names(registry) == _BUILTIN_NAMES + ["custom_tool"]

    assert len(reads) == 1, "条目只读一次，之后走进程缓存"


def test_build_tool_registry_stays_cheap_and_is_cached():
    first = build_tool_registry()

    assert build_tool_registry() is first, "仍按进程缓存（ADR-009）"


def test_close_releases_servers_and_the_base(fake_server):
    base = _StubRegistry()
    rows = [_entry("extra", tool_names=(SERVER_TOOL,))]
    registry = _registry(rows, base)
    registry.list_tools()

    registry.close()

    assert base.closed is True
    assert registry._clients == {}

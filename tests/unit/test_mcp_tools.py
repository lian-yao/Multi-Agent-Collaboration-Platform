"""成员 C D7-8：MCP 工具发现与调用（`doc/testing.md` §2.1 I-06 的单元级证据）。

对齐 `doc/15 AI Native多智能体协作平台.md` 模块 3「动态发现」与「工具执行」，
以及 ADR-009 的三个接入点：

- `build_mcp_server` 把内置工具暴露成 MCP Server（`list_tools` / `call_tool`）；
- `McpToolRegistry` 把异步 MCP 会话包成编排层要求的**同步** `ToolRegistry`；
- `build_tool_registry` 按 `MCP_TRANSPORT` 选后端并缓存，`InstrumentedToolRegistry`
  为每次调用采集 Span、耗时与成功率；
- `CompositeToolRegistry` 把「配置页注册并发现过的 MCP Server」的工具并进同一个
  注册表：目录取自发现缓存（不建连接），调用时才惰性建会话。

测试用 `mcp.shared.memory` 把 Server 与 Client 直接接起来，因此走的是**真实 MCP
协议往返**（JSON-RPC + 类型校验），既不启动子进程、不监听端口，也不请求外部模型
（`doc/testing.md` §1）。跨进程 stdio 的端到端验收见 `tests/e2e/test_mcp_stdio_e2e.py`。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, Field

from app.mcp import (
    CompositeToolRegistry,
    InstrumentedToolRegistry,
    McpSettings,
    McpToolRegistry,
    build_mcp_server,
    build_tool_registry,
    reset_registered_servers_cache,
    reset_tool_registry_cache,
    tool_catalog,
)
from app.mcp import registry as registry_module
from app.mcp.server import build_mcp_server as build_mcp_server_from_server_module
from app.orchestration.tools import ToolCall, ToolRegistry, ToolSpec
from app.tools import BuiltinToolRegistry, ToolExecutionError
from app.tools.base import BuiltinTool
from app.tools.registry import build_builtin_registry

EXPECTED_TOOL_NAMES = ["calculator", "code_execution", "sql_query", "web_search"]


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """`build_tool_registry` 有进程级缓存，避免用例之间互相泄漏。"""

    reset_tool_registry_cache()
    reset_registered_servers_cache()
    yield
    reset_tool_registry_cache()
    reset_registered_servers_cache()


@pytest.fixture
def mcp_registry():
    """通过真实 MCP 协议会话访问内置工具的注册表。"""

    server = build_mcp_server(BuiltinToolRegistry())

    @asynccontextmanager
    async def session_factory():
        async with create_connected_server_and_client_session(server) as session:
            yield session

    registry = McpToolRegistry(session_factory)
    try:
        yield registry
    finally:
        registry.close()


# --- MCP 协议往返 ---------------------------------------------------------


def test_mcp_server_exposes_the_four_builtin_tools(mcp_registry):
    specs = mcp_registry.list_tools()

    assert [spec.name for spec in specs] == EXPECTED_TOOL_NAMES
    for spec in specs:
        assert spec.description, spec.name
        assert spec.input_schema.get("type") == "object", spec.name


def test_mcp_discovery_is_cached_within_a_registry(mcp_registry):
    assert mcp_registry.list_tools() is mcp_registry.list_tools()


def test_mcp_call_returns_the_unwrapped_tool_output(mcp_registry):
    output = mcp_registry.call(
        ToolCall(call_id="c1", tool_name="calculator", arguments={"expression": "6*7"})
    )

    assert output == {"expression": "6*7", "value": 42}


def test_mcp_call_surfaces_tool_failure_as_tool_error(mcp_registry):
    """工具内部失败经 Server 的 JSON 约定传回，Client 还原成 `ToolExecutionError`。"""

    with pytest.raises(ToolExecutionError) as failure:
        mcp_registry.call(
            ToolCall(
                call_id="c2",
                tool_name="sql_query",
                arguments={"query": "delete from metrics"},
            )
        )

    assert "sql_query" in str(failure.value)


def test_mcp_call_reports_unknown_tool(mcp_registry):
    with pytest.raises(ToolExecutionError):
        mcp_registry.call(
            ToolCall(call_id="c3", tool_name="drop_database", arguments={})
        )


def test_mcp_call_rejects_invalid_arguments(mcp_registry):
    with pytest.raises(ToolExecutionError):
        mcp_registry.call(
            ToolCall(call_id="c4", tool_name="calculator", arguments={"expression": 1})
        )


def test_mcp_registry_satisfies_the_orchestration_protocol(mcp_registry):
    assert isinstance(mcp_registry, ToolRegistry)


# --- 默认注册表与目录 -------------------------------------------------------


def test_build_tool_registry_is_cached_and_resettable():
    first = build_tool_registry()
    second = build_tool_registry()

    assert first is second
    assert [spec.name for spec in first.list_tools()] == EXPECTED_TOOL_NAMES

    reset_tool_registry_cache()
    assert build_tool_registry() is not first


def test_tool_catalog_matches_the_api_contract():
    """`doc/api.md` §5.3：name / description / input_schema / status。"""

    catalog = tool_catalog()

    assert [item["name"] for item in catalog] == EXPECTED_TOOL_NAMES
    for item in catalog:
        assert item["status"] == "available"
        assert item["input_schema"].get("type") == "object"


def test_default_registry_is_available_once_mcp_is_implemented():
    """ADR-009：C 实现 `app.mcp.registry.build_tool_registry` 后流水线自动接入。"""

    from app.orchestration.tools import default_tool_registry

    registry = default_tool_registry()

    assert registry is not None
    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES


def test_server_builder_is_reexported():
    assert build_mcp_server is build_mcp_server_from_server_module


# --- 观测包装 --------------------------------------------------------------


class RecordingRegistry:
    """最小的 `ToolRegistry` 替身，用于验证包装层。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[ToolCall] = []
        self.closed = False
        self._error = error

    def list_tools(self):
        return (ToolSpec(name="calculator", description="", input_schema={}),)

    def call(self, request: ToolCall):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return {"value": 42}

    def close(self) -> None:
        self.closed = True


def test_instrumented_registry_records_success_metrics(memory_metrics):
    collector, sink = memory_metrics
    inner = RecordingRegistry()
    registry = InstrumentedToolRegistry(inner, source="inprocess")

    output = registry.call(
        ToolCall(call_id="c1", tool_name="calculator", arguments={"expression": "6*7"})
    )
    collector.flush()

    assert output == {"value": 42}
    calls = sink.samples("tool_calls")
    assert len(calls) == 1
    assert calls[0].labels["tool_name"] == "calculator"
    assert calls[0].labels["status"] == "succeeded"
    assert sink.samples("tool_call_duration_ms")


def test_instrumented_registry_records_failure_and_reraises(memory_metrics):
    collector, sink = memory_metrics
    inner = RecordingRegistry(error=RuntimeError("mcp server down"))
    registry = InstrumentedToolRegistry(inner, source="stdio")

    with pytest.raises(RuntimeError):
        registry.call(ToolCall(call_id="c9", tool_name="calculator", arguments={}))
    collector.flush()

    failures = sink.samples("tool_call_failures")
    assert len(failures) == 1
    assert failures[0].labels["status"] == "failed"


def test_instrumented_registry_deduplicates_replayed_calls(memory_metrics):
    """Dapr 活动重放会重复投递同一 call_id，指标不重复累计。"""

    collector, sink = memory_metrics
    registry = InstrumentedToolRegistry(RecordingRegistry(), source="inprocess")
    request = ToolCall(call_id="stable", tool_name="calculator", arguments={})

    registry.call(request)
    registry.call(request)
    collector.flush()

    assert len(sink.samples("tool_calls")) == 1


def test_instrumented_registry_closes_the_backend():
    inner = RecordingRegistry()
    registry = InstrumentedToolRegistry(inner, source="inprocess")

    registry.close()

    assert inner.closed is True


# --- 已注册 MCP Server 的合并（ADR-017 §6） -------------------------------


class EchoArgs(BaseModel):
    text: str = Field(min_length=1, description="要回显的文本")


class EchoTool(BuiltinTool):
    """只存在于测试里的工具：名字不与内置工具冲突，用于验证合并与路由。"""

    name = "echo"
    description = "回显输入文本"
    args_model = EchoArgs

    def run(self, args: EchoArgs) -> dict[str, Any]:
        return {"echo": args.text}


def _server_row(
    server_id: str, tool_names: list[str], **overrides: Any
) -> dict[str, Any]:
    """一条 `mcp_server_registry` 视图，形状对齐 `app/core/mcp_registry.py` 的 `_server_view`。"""

    row: dict[str, Any] = {
        "id": server_id,
        "name": server_id,
        "transport": "stdio",
        "command": "python",
        "args": [],
        "env": {},
        "cwd": None,
        "url": None,
        "headers": {},
        "enabled": True,
        "tool_options": {},
        "discovered": {
            "server_info": {"name": server_id},
            "tool_names": list(tool_names),
            "tool_schemas": {name: EchoTool().spec().input_schema for name in tool_names},
            "tool_descriptions": {name: "回显输入文本" for name in tool_names},
            "discovered_at": "2026-09-16T00:00:00+00:00",
        },
    }
    return {**row, **overrides}


def _memory_session_factory(tools: list[BuiltinTool]):
    """把 Server 与 Client 直接接起来的内存会话工厂（不起子进程）。"""

    server = build_mcp_server(BuiltinToolRegistry(tools))

    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(server) as session:
            yield session

    return factory


def test_composite_registry_merges_discovered_server_tools():
    registry = CompositeToolRegistry(
        build_builtin_registry(), [_server_row("echo-server", ["echo"])]
    )

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES + ["echo"]


def test_composite_registry_does_not_connect_while_listing():
    """列目录只读发现缓存：地址指向一个连不上的远端也必须能列出来。"""

    row = _server_row(
        "remote", ["echo"], transport="http", url="http://127.0.0.1:1/mcp", command=None
    )

    assert "echo" in [
        spec.name
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
    ]


def test_composite_registry_skips_transports_that_cannot_connect():
    """`ws` 没有客户端实现，合并阶段就排除——不对外暴露调不通的工具。"""

    row = _server_row(
        "legacy", ["echo"], transport="ws", url="ws://localhost:9/mcp", command=None
    )

    names = [
        spec.name
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
    ]
    assert names == EXPECTED_TOOL_NAMES


def test_composite_registry_requires_a_discovery_cache():
    """没发现过的 Server 不进目录（与配置页「available 是配置的函数」一致）。"""

    row = _server_row("never-discovered", ["echo"], discovered=None)

    names = [
        spec.name
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
    ]
    assert names == EXPECTED_TOOL_NAMES


def test_composite_registry_prefers_the_builtin_on_a_name_conflict():
    row = _server_row("shadow", ["calculator", "echo"])

    names = [
        spec.name
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
    ]
    assert names == EXPECTED_TOOL_NAMES + ["echo"]


def test_composite_registry_hides_tools_disabled_in_tool_options():
    row = _server_row("echo-server", ["echo"], tool_options={"echo": {"disabled": True}})

    names = [
        spec.name
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
    ]
    assert names == EXPECTED_TOOL_NAMES


def test_composite_registry_supplies_a_fallback_schema():
    """发现结果没带 Schema 时兜底空对象 Schema，目录项仍满足 `doc/api.md` §5.3。"""

    row = _server_row("echo-server", ["echo"])
    row["discovered"]["tool_schemas"] = {}

    spec = next(
        spec
        for spec in CompositeToolRegistry(build_builtin_registry(), [row]).list_tools()
        if spec.name == "echo"
    )
    assert spec.input_schema == {"type": "object", "properties": {}}


def test_composite_registry_routes_a_call_to_the_owning_server(monkeypatch):
    """注册 Server 的工具经**真实 MCP 协议往返**执行（内存会话，不起子进程）。"""

    monkeypatch.setattr(
        registry_module,
        "build_registry_session_factory",
        lambda row: _memory_session_factory([EchoTool()]),
    )
    registry = CompositeToolRegistry(
        build_builtin_registry(), [_server_row("echo-server", ["echo"])]
    )
    try:
        output = registry.call(
            ToolCall(call_id="c1", tool_name="echo", arguments={"text": "你好"})
        )
    finally:
        registry.close()

    assert output == {"echo": "你好"}


def test_composite_registry_reports_an_unregistered_tool():
    registry = CompositeToolRegistry(build_builtin_registry(), [])

    with pytest.raises(ToolExecutionError):
        registry.call(ToolCall(call_id="c1", tool_name="echo", arguments={}))


def test_composite_registry_closes_the_base_registry():
    inner = RecordingRegistry()

    CompositeToolRegistry(inner, []).close()

    assert inner.closed is True


# --- 注册条目的读取策略 -----------------------------------------------------


def test_registered_servers_are_ignored_by_default(monkeypatch):
    """默认不读配置库：`GET /api/v1/tools` 与阶段活动都不该依赖数据库可用。"""

    def explode(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("默认配置不应读取 mcp_server_registry")

    monkeypatch.setattr(registry_module, "_load_registered_servers", explode)

    registry = build_tool_registry(McpSettings(_env_file=None))

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES


def test_registered_servers_can_be_included_by_setting(monkeypatch):
    monkeypatch.setattr(
        registry_module,
        "_load_registered_servers",
        lambda loader=None: [_server_row("echo-server", ["echo"])],
    )

    registry = build_tool_registry(
        McpSettings(_env_file=None, include_registered_servers=True)
    )

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES + ["echo"]


def test_injected_server_rows_bypass_the_setting(monkeypatch):
    """显式传入条目时既不读配置库、也不受开关影响。"""

    def explode(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("注入条目时不应读取 mcp_server_registry")

    monkeypatch.setattr(registry_module, "_load_registered_servers", explode)

    registry = build_tool_registry(
        McpSettings(_env_file=None), server_rows=[_server_row("echo-server", ["echo"])]
    )

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES + ["echo"]


def test_registered_servers_lookup_is_fail_soft():
    """配置库不可用只等于「没有额外工具」，不让工具接入整体失败。"""

    def broken() -> list[dict[str, Any]]:
        raise RuntimeError("配置库不可用")

    assert registry_module._load_registered_servers(broken) == []


def test_registered_servers_lookup_is_bounded(monkeypatch):
    """建连挂在 `select` 上时按超时退化，不会拖住调用方。"""

    monkeypatch.setattr(registry_module, "REGISTERED_SERVERS_LOAD_TIMEOUT_SECONDS", 0.1)

    def hangs() -> list[dict[str, Any]]:
        time.sleep(5)
        return []

    started = time.perf_counter()
    assert registry_module._load_registered_servers(hangs) == []
    assert time.perf_counter() - started < 2.0


def test_tool_catalog_lists_merged_server_tools(monkeypatch):
    monkeypatch.setattr(
        registry_module,
        "_load_registered_servers",
        lambda loader=None: [_server_row("echo-server", ["echo"])],
    )
    monkeypatch.setattr(
        registry_module,
        "get_mcp_settings",
        lambda: McpSettings(_env_file=None, include_registered_servers=True),
    )

    catalog = tool_catalog()

    assert [item["name"] for item in catalog] == EXPECTED_TOOL_NAMES + ["echo"]
    assert {item["status"] for item in catalog} == {"available"}

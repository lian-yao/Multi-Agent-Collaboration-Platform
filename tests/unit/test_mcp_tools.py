"""成员 C D7-8：MCP 工具发现与调用（`doc/testing.md` §2.1 I-06 的单元级证据）。

对齐 `doc/15 AI Native多智能体协作平台.md` 模块 3「动态发现」与「工具执行」，
以及 ADR-009 的三个接入点：

- `build_mcp_server` 把内置工具暴露成 MCP Server（`list_tools` / `call_tool`）；
- `McpToolRegistry` 把异步 MCP 会话包成编排层要求的**同步** `ToolRegistry`；
- `build_tool_registry` 按 `MCP_TRANSPORT` 选后端并缓存，`InstrumentedToolRegistry`
  为每次调用采集 Span、耗时与成功率。

测试用 `mcp.shared.memory` 把 Server 与 Client 直接接起来，因此走的是**真实 MCP
协议往返**（JSON-RPC + 类型校验），既不启动子进程、不监听端口，也不请求外部模型
（`doc/testing.md` §1）。跨进程 stdio 与真实 PostgreSQL 的端到端验收仍属 I-06 缺口。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp import (
    InstrumentedToolRegistry,
    McpToolRegistry,
    build_mcp_server,
    build_tool_registry,
    reset_tool_registry_cache,
    tool_catalog,
)
from app.mcp.server import build_mcp_server as build_mcp_server_from_server_module
from app.orchestration.tools import ToolCall, ToolRegistry, ToolSpec
from app.tools import BuiltinToolRegistry, ToolExecutionError

EXPECTED_TOOL_NAMES = ["calculator", "code_execution", "sql_query", "web_search"]


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """`build_tool_registry` 有进程级缓存，避免用例之间互相泄漏。"""

    reset_tool_registry_cache()
    yield
    reset_tool_registry_cache()


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

"""成员 A D7-8：流水线接入 MCP 工具的验收测试。

覆盖 M4 中属于编排层的部分（`分工.md` §3「流水线接入 MCP 工具并验收」）：

- 流水线能发现注册表暴露的工具，契约对齐 `doc/api.md` §4.10（name/description/input_schema）；
- 角色节点按模型返回的 tool_calls 调用工具，并把观察结果回填给模型（ReAct 循环）；
- 调用记录字段对齐 `doc/data-model.md` §3 tool_calls，可随 PipelineState 一起序列化；
- 未知工具与注册表异常都记录为 failed，不中断整条流水线；
- 未接入注册表时行为与接入前保持一致（不绑定工具、不产生调用记录）。

测试替身按 `doc/testing.md` §1：MCP 工具使用内存版注册表，不请求真实 MCP Server，
也不请求任何外部模型服务。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.orchestration.pipeline import (
    PipelineStage,
    PipelineStatus,
    deserialize_pipeline_state,
    new_pipeline_state,
    serialize_pipeline_state,
    start,
)
from app.orchestration.pipeline_graph import (
    run_multi_agent_pipeline,
    run_role_stage,
)
from app.orchestration.tools import (
    ToolCall,
    ToolCaller,
    ToolCallStatus,
    ToolRegistry,
    ToolSpec,
    as_openai_tool,
    default_tool_registry,
    set_tool_registry_factory,
)

SEARCH_SPEC = ToolSpec(
    name="web_search",
    description="检索公开资料",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)


class MemoryToolRegistry:
    """内存版工具注册表（测试替身）：按名字返回固定结果，可注入统一异常。"""

    def __init__(
        self,
        specs: list[ToolSpec],
        *,
        results: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._specs = list(specs)
        self._results = dict(results or {})
        self._error = error
        self.calls: list[ToolCall] = []

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, request: ToolCall) -> Any:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if request.tool_name not in {spec.name for spec in self._specs}:
            raise ValueError(f"unknown tool: {request.tool_name}")
        return self._results.get(request.tool_name, {"ok": True})


class ToolCallingChatModel(BaseChatModel):
    """假模型：首次调用请求工具，收到工具观察后再给出结论。"""

    tool_name: str
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    final_reply: str = "最终结论"
    bound_tools: list[Any] = Field(default_factory=list, exclude=True)
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "tool-calling-chat-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain 钩子签名
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        if any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(content=self.final_reply)
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_arguments),
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture(autouse=True)
def _reset_tool_registry_factory():
    """避免默认注册表工厂在用例之间泄漏。"""

    yield
    set_tool_registry_factory(None)


def test_tool_spec_serializes_to_openai_tool_schema():
    assert as_openai_tool(SEARCH_SPEC) == {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "检索公开资料",
            "parameters": SEARCH_SPEC.input_schema,
        },
    }


def test_tool_registry_protocol_is_satisfied_by_memory_registry():
    assert isinstance(MemoryToolRegistry([SEARCH_SPEC]), ToolRegistry)


def test_tool_caller_exposes_discovered_tools():
    caller = ToolCaller(MemoryToolRegistry([SEARCH_SPEC]))

    assert caller.has_tools() is True
    assert caller.available() == (SEARCH_SPEC,)


def test_role_stage_discovers_tools_and_feeds_observation_back():
    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料一"]}},
    )
    model = ToolCallingChatModel(
        tool_name="web_search",
        tool_arguments={"query": "多 Agent 协作"},
    )

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="收集多 Agent 协作的资料",
        llm=model,
        tool_registry=registry,
    )

    assert result["content"] == "最终结论"
    assert model.bound_tools == [as_openai_tool(SEARCH_SPEC)]
    observation = model.calls[1][-1]
    assert isinstance(observation, ToolMessage)
    assert "资料一" in observation.content
    assert registry.calls[0].arguments == {"query": "多 Agent 协作"}


def test_tool_call_record_matches_tool_calls_table_fields():
    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": []}},
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "x"})

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )
    [record] = result["tool_calls"]

    assert set(record) == {"call_id", "tool_name", "input", "output", "status", "error"}
    assert record["tool_name"] == "web_search"
    assert record["input"] == {"query": "x"}
    assert record["output"] == {"hits": []}
    assert record["status"] == ToolCallStatus.SUCCEEDED.value
    assert record["error"] is None


def test_unknown_tool_is_recorded_as_failed_without_breaking_pipeline():
    registry = MemoryToolRegistry([SEARCH_SPEC])
    model = ToolCallingChatModel(tool_name="drop_database")

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.FAILED.value
    assert "unknown tool" in record["error"]
    assert result["content"] == "最终结论"


def test_registry_failure_is_recorded_as_failed():
    registry = MemoryToolRegistry([SEARCH_SPEC], error=RuntimeError("mcp server down"))
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "x"})

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.FAILED.value
    assert "mcp server down" in record["error"]


def test_role_stage_without_registry_does_not_bind_tools():
    model = ToolCallingChatModel(tool_name="web_search")

    result = run_role_stage(PipelineStage.COLLECT, task="任务", llm=model)

    assert model.bound_tools == []
    assert result["tool_calls"] == []


def test_default_tool_registry_prefers_explicit_factory():
    registry = MemoryToolRegistry([SEARCH_SPEC])
    set_tool_registry_factory(lambda: registry)

    assert default_tool_registry() is registry


def test_default_registry_factory_is_used_by_pipeline():
    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["默认来源"]}},
    )
    set_tool_registry_factory(lambda: registry)
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    result = run_role_stage(PipelineStage.COLLECT, task="任务", llm=model)

    assert [record["tool_name"] for record in result["tool_calls"]] == ["web_search"]


def test_tool_call_ids_are_unique_without_scope():
    caller = ToolCaller(MemoryToolRegistry([SEARCH_SPEC]))

    first = caller.invoke("web_search", {"query": "a"})
    second = caller.invoke("web_search", {"query": "a"})

    assert first.call_id != second.call_id


def test_tool_call_ids_are_stable_when_scope_is_given_for_replay():
    registry = MemoryToolRegistry([SEARCH_SPEC])

    first = ToolCaller(registry, scope="workflow-1").invoke("web_search", {"query": "a"})
    replayed = ToolCaller(registry, scope="workflow-1").invoke("web_search", {"query": "a"})

    assert first.call_id == replayed.call_id


def test_multi_agent_pipeline_records_tool_calls_per_stage():
    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料一"]}},
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    state = run_multi_agent_pipeline("分析主题", llm=model, tool_registry=registry)

    assert state.status is PipelineStatus.COMPLETED
    assert state.results[PipelineStage.COLLECT]["tool_calls"][0]["tool_name"] == "web_search"
    assert state.results[PipelineStage.REPORT]["content"] == "最终结论"


def test_pipeline_state_with_tool_calls_roundtrips_through_json():
    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料一"]}},
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    state = run_multi_agent_pipeline("分析主题", llm=model, tool_registry=registry)
    restored = deserialize_pipeline_state(json.dumps(serialize_pipeline_state(state)))

    assert restored == state
    assert restored.results[PipelineStage.COLLECT]["tool_calls"][0]["status"] == "succeeded"


def test_workflow_stage_activity_consumes_default_registry_without_changes():
    """D7-8 验收：Dapr 阶段活动不改代码即可用默认注册表调用工具。"""

    from app.workflows.pipeline import advance_pipeline_stage

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料一"]}},
    )
    set_tool_registry_factory(lambda: registry)
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    outcome = advance_pipeline_stage(
        start(new_pipeline_state(task="演示任务")),
        PipelineStage.COLLECT,
        task="演示任务",
        llm=model,
    )
    restored = deserialize_pipeline_state(outcome["state"])
    [record] = restored.results[PipelineStage.COLLECT]["tool_calls"]

    assert record["tool_name"] == "web_search"
    assert record["status"] == ToolCallStatus.SUCCEEDED.value
    assert record["output"] == {"hits": ["资料一"]}

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
import logging
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
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
    """内存版工具注册表（测试替身）：按名字返回固定结果。

    - `error`：每次调用都失败（异常即该 `error`）；
    - `failures_before_success=N`：先失败 N 次（异常取 `error`，未给则用默认），之后成功。
      两种模式互斥——给了 `failures_before_success` 就按「重试后成功」的语义走。
    """

    def __init__(
        self,
        specs: list[ToolSpec],
        *,
        results: dict[str, Any] | None = None,
        error: Exception | None = None,
        failures_before_success: int = 0,
    ) -> None:
        self._specs = list(specs)
        self._results = dict(results or {})
        self._error = error
        self._failures_before_success = failures_before_success
        self._retry_mode = failures_before_success > 0
        self.calls: list[ToolCall] = []

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, request: ToolCall) -> Any:
        self.calls.append(request)
        if self._failures_before_success:
            self._failures_before_success -= 1
            raise self._error or RuntimeError("工具暂时不可用")
        if self._error is not None and not self._retry_mode:
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


class EmptyAfterObservationChatModel(BaseChatModel):
    """收到工具观察后先返回空文本（F-07 场景）。

    `recovered=True` 时补一次「请用文字给出结论」的提示后给出结论；
    `recovered=False` 时补提示后仍为空，用于验证「告警并继续」的分支。
    """

    recovered: bool = True
    tool_name: str = "web_search"
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    bound_tools: list[Any] = Field(default_factory=list, exclude=True)
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "empty-after-observation-chat-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain 钩子签名
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        observed = any(isinstance(message, ToolMessage) for message in messages)
        nudged = any(
            isinstance(message, HumanMessage) and "不要再调用工具" in str(message.content)
            for message in messages
        )
        if not observed:
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
        elif not nudged or not self.recovered:
            message = AIMessage(content="")
        else:
            message = AIMessage(content="补上的结论")
        return ChatResult(generations=[ChatGeneration(message=message)])


class RelentlessToolChatModel(BaseChatModel):
    """一直请求工具、从不产出文字（真实模型撞工具轮次上限的场景，F-07）。

    `answer_after_nudge=True` 表示收到「不要再调用工具」的补提示后给出结论。
    """

    answer_after_nudge: bool = True
    tool_name: str = "web_search"
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    bound_tools: list[Any] = Field(default_factory=list, exclude=True)
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "relentless-tool-chat-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain 钩子签名
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        nudged = any(
            isinstance(message, HumanMessage) and "不要再调用工具" in str(message.content)
            for message in messages
        )
        if nudged and self.answer_after_nudge:
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content="补上的结论"))
                ]
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": self.tool_name,
                                "args": dict(self.tool_arguments),
                                "id": f"call-{len(self.calls)}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


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
    # app/mcp 落地后 default_tool_registry() 会回退到成员 C 的注册表，
    # 这里显式构造「没有注册表」的前置条件，而不是依赖模块不存在（ADR-012）。
    set_tool_registry_factory(lambda: None)
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


def test_role_stage_scopes_tool_call_ids_by_stage():
    """同一 Workflow 的两个阶段调同一工具时必须产出两个调用 ID（F-01）。

    `ToolCaller` 每个阶段重建、`index` 都从 0 起算，所以调用 ID 必须把阶段纳入派生键；
    否则 `app/core/tool_audit.execute_tool_call` 会把后一个阶段当成重放，直接返回前一个
    阶段的缓存结果（实测：analyze 请求 `12*(3+5)` 却拿到 collect 的 `12*(3+4)` 结果）。
    """

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    collect = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
        tool_scope="workflow-1",
    )
    analyze = run_role_stage(
        PipelineStage.ANALYZE,
        task="任务",
        previous={"content": "收集阶段的结论"},
        llm=model,
        tool_registry=registry,
        tool_scope="workflow-1",
    )

    assert collect["tool_calls"][0]["call_id"] != analyze["tool_calls"][0]["call_id"]


def test_role_stage_keeps_tool_call_id_stable_when_the_same_stage_replays():
    """同一阶段重放（Dapr 活动重放）仍要得到同一个调用 ID，幂等语义不能被改坏。"""

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    first = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
        tool_scope="workflow-1",
    )
    replayed = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
        tool_scope="workflow-1",
    )

    assert first["tool_calls"][0]["call_id"] == replayed["tool_calls"][0]["call_id"]


def test_tool_caller_stage_is_part_of_the_derived_call_id():
    """`ToolCaller` 把阶段纳入派生键：同 scope、同序号、同工具，不同阶段即不同 ID。"""

    registry = MemoryToolRegistry([SEARCH_SPEC])

    collect = ToolCaller(registry, scope="workflow-1", stage="collect").invoke(
        "web_search", {"query": "收集"}
    )
    analyze = ToolCaller(registry, scope="workflow-1", stage="analyze").invoke(
        "web_search", {"query": "分析"}
    )
    replayed = ToolCaller(registry, scope="workflow-1", stage="collect").invoke(
        "web_search", {"query": "收集"}
    )

    assert collect.call_id != analyze.call_id
    assert collect.call_id == replayed.call_id


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


def test_empty_output_is_retried_once_with_a_text_nudge():
    """工具回合后模型只回空文本时，补一次「给出文字结论」的提示（F-07）。"""

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = EmptyAfterObservationChatModel(tool_arguments={"query": "主题"})
    model.recovered = True

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    assert result["content"] == "补上的结论"
    assert len(model.calls) == 3  # 工具请求 → 空输出 → 补提示后的结论
    assert "不要再调用工具" in str(model.calls[2][-1].content)


def test_persistently_empty_output_warns_and_pipeline_continues(caplog):
    """补提示后仍为空：落 WARNING、只重试一次，流水线继续（不中断协作）。"""

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = EmptyAfterObservationChatModel(tool_arguments={"query": "主题"})
    model.recovered = False

    with caplog.at_level(logging.WARNING, logger="macp.orchestration.pipeline"):
        result = run_role_stage(
            PipelineStage.COLLECT,
            task="任务",
            llm=model,
            tool_registry=registry,
        )

    assert result["content"] == ""
    assert result["status"] == "completed"
    assert len(model.calls) == 3  # 不无限重试：工具请求 + 空输出 + 一次补提示
    assert "stage.empty_content" in caplog.text


def test_tool_iteration_limit_is_warned_and_then_nudged_for_text(caplog):
    """模型撞到工具轮次上限且没有文字时：先记上限告警，再补一次文字提示（F-07）。"""

    from app.orchestration.pipeline_graph import TOOL_CALL_MAX_ITERATIONS

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = RelentlessToolChatModel(tool_arguments={"query": "主题"})

    with caplog.at_level(logging.WARNING, logger="macp.orchestration.pipeline"):
        result = run_role_stage(
            PipelineStage.COLLECT,
            task="任务",
            llm=model,
            tool_registry=registry,
        )

    assert result["content"] == "补上的结论"
    # 1 次首调用 + 轮次上限次重入 + 1 次补提示
    assert len(model.calls) == TOOL_CALL_MAX_ITERATIONS + 2
    assert "stage.tool_iteration_limit" in caplog.text
    assert "不要再调用工具" in str(model.calls[-1][-1].content)


def test_tool_iteration_limit_with_persistent_empty_output_continues(caplog):
    """补提示后模型仍只给工具调用、不给文字：只重试一次、告警后继续（不阻断流水线）。"""

    from app.orchestration.pipeline_graph import TOOL_CALL_MAX_ITERATIONS

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
    )
    model = RelentlessToolChatModel(tool_arguments={"query": "主题"})
    model.answer_after_nudge = False

    with caplog.at_level(logging.WARNING, logger="macp.orchestration.pipeline"):
        result = run_role_stage(
            PipelineStage.COLLECT,
            task="任务",
            llm=model,
            tool_registry=registry,
        )

    assert result["content"] == ""
    assert result["status"] == "completed"
    assert len(model.calls) == TOOL_CALL_MAX_ITERATIONS + 2  # 不无限重试
    assert "stage.tool_iteration_limit" in caplog.text
    assert "stage.empty_content" in caplog.text


def test_failed_tool_call_is_retried_until_it_succeeds():
    """工具失败后按同一调用 ID 重试，成功即采用真实结果（不再记失败）。"""

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        results={"web_search": {"hits": ["资料"]}},
        error=RuntimeError("mcp server down"),
        failures_before_success=2,
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.SUCCEEDED.value
    assert record["output"] == {"hits": ["资料"]}
    assert record["error"] is None
    assert len(registry.calls) == 3  # 第 3 次尝试成功
    assert len({call.call_id for call in registry.calls}) == 1  # 同一个逻辑调用共用一个 ID
    assert result["content"] == "最终结论"


def test_tool_call_stops_after_three_retries_and_stage_still_answers():
    """重试 3 次后仍失败：记 failed、不再重试，阶段照常按实际结果输出。"""

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        error=RuntimeError("mcp server down"),
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.FAILED.value
    assert "mcp server down" in record["error"]
    assert len(registry.calls) == 4  # 首次 + 3 次重试
    assert result["status"] == "completed"
    assert result["content"] == "最终结论"  # 失败不中断：模型仍给出结论


def test_non_retryable_tool_failure_is_not_retried(caplog):
    """入参/策略造成的确定性失败只尝试一次（ADR-009 修订 3）。"""

    from app.tools.base import ToolExecutionError

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        error=ToolExecutionError("参数不合法: query 缺失", retryable=False),
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    with caplog.at_level(logging.INFO, logger="macp.orchestration.tools"):
        result = run_role_stage(
            PipelineStage.COLLECT,
            task="任务",
            llm=model,
            tool_registry=registry,
        )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.FAILED.value
    assert len(registry.calls) == 1  # 不重试
    assert "tool.retry_skipped" in caplog.text
    assert result["content"] == "最终结论"  # 仍按实际结果输出


def test_retryable_tool_failure_is_still_retried_three_times():
    """瞬时故障（服务不可达/超时）仍按上限重试 3 次（ADR-009 修订 3）。"""

    from app.tools.base import ToolExecutionError

    registry = MemoryToolRegistry(
        [SEARCH_SPEC],
        error=ToolExecutionError("搜索服务不可达: timed out"),  # retryable 默认 True
    )
    model = ToolCallingChatModel(tool_name="web_search", tool_arguments={"query": "主题"})

    result = run_role_stage(
        PipelineStage.COLLECT,
        task="任务",
        llm=model,
        tool_registry=registry,
    )

    [record] = result["tool_calls"]
    assert record["status"] == ToolCallStatus.FAILED.value
    assert len(registry.calls) == 4  # 首次 + 3 次重试

"""成员 A D5-6：多 Agent 流水线的 LangGraph 图与角色分配。

职责边界（分工.md §2/§3、ADR-006、doc/dapr-integration.md §2）：
- 本模块只提供编排层内容：图拓扑、节点执行顺序、状态 Schema 与角色分配；
- 角色静态内容（system_prompt 等）来自 ``app.agents.roles``，由成员 C 维护，
  接线时统一经 ``get_role(role_id)`` 获取；
- Workflow 侧的接线已完成：``app.workflows.pipeline`` 的阶段活动按序调用本模块的
  ``run_role_stage``（ADR-007）。

固定三步流水线将 ``PipelineStage`` 依次分配给协作角色：

``collect → collector``、``analyze → analyst``、``report → reporter``。

成员 A D7-8 增量：角色节点接入 MCP 工具。传入注册表（或默认解析到成员 C 的
``app/mcp`` 接入点）后，节点按模型返回的 tool_calls 调用工具并把观察结果回填给模型，
每次调用记录进阶段载荷的 ``tool_calls`` 字段，供审计落库与前端展示（ADR-009）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from app.agents.roles import RoleId, get_role
from app.config import AgentSettings, get_settings
from app.orchestration.llm import build_chat_model
from app.orchestration.pipeline import (
    PIPELINE_STEPS,
    PipelineStage,
    PipelineState,
    PipelineStatus,
    complete_step,
    new_pipeline_state,
    start,
)
from app.orchestration.tools import (
    ToolCaller,
    ToolCallRecord,
    ToolCallStatus,
    ToolRegistry,
    default_tool_registry,
)

# 流水线阶段 → 协作角色 的固定分配，是 M3 固定团队的事实来源之一。
PIPELINE_ROLE_ASSIGNMENT: dict[PipelineStage, RoleId] = {
    PipelineStage.COLLECT: RoleId.COLLECTOR,
    PipelineStage.ANALYZE: RoleId.ANALYST,
    PipelineStage.REPORT: RoleId.REPORTER,
}

# 单个角色节点内允许的「请求工具 → 回填观察」轮次上限，避免模型陷入无限循环。
TOOL_CALL_MAX_ITERATIONS = 4

_UPSTREAM_LABELS: dict[RoleId, str] = {
    RoleId.ANALYST: "信息收集结果",
    RoleId.REPORTER: "数据分析结果",
}


def role_for_stage(stage: PipelineStage | str) -> RoleId:
    """返回某流水线阶段对应的协作角色。"""
    if isinstance(stage, str):
        try:
            resolved = PipelineStage(stage)
        except ValueError as exc:
            raise ValueError(f"未知流水线阶段: {stage}") from exc
    else:
        resolved = stage
    try:
        return PIPELINE_ROLE_ASSIGNMENT[resolved]
    except KeyError as exc:
        raise ValueError(f"未知流水线阶段: {resolved}") from exc


def pipeline_roles() -> tuple[RoleId, ...]:
    """按流水线顺序返回固定团队的三个角色 id。"""
    return tuple(role_for_stage(stage) for stage in PIPELINE_STEPS)


def graph_node_order() -> tuple[str, ...]:
    """返回 LangGraph 节点的执行顺序（角色 id）。"""
    return tuple(role.value for role in pipeline_roles())


def _content_text(content: str | list[Any]) -> str:
    """把 AIMessage.content 归一化为纯文本，兼容字符串与 content block 列表。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _role_input(role: RoleId, task: str, previous: dict[str, Any] | None) -> str:
    """构造角色节点的用户输入：首节点给原始任务，后续节点带上游结果。"""
    if role is RoleId.COLLECTOR:
        return f"用户任务：\n{task}"
    if previous is None or not isinstance(previous.get("content"), str):
        raise ValueError(
            f"角色 {role.value} 缺少上游结果，不能独立执行"
        )
    return f"{_UPSTREAM_LABELS[role]}：\n{previous['content']}"


def _stage_result(
    stage: PipelineStage,
    content: str,
    previous: dict[str, Any] | None,
    tool_calls: Sequence[ToolCallRecord] = (),
) -> dict[str, Any]:
    """构造 Workflow 阶段活动使用的载荷。

    字段：``step`` / ``status`` / ``content`` / ``previous`` / ``tool_calls``。
    ``tool_calls`` 为本次阶段执行内产生的工具调用记录（可能为空），
    记录结构对齐 `doc/data-model.md` §3 tool_calls 表，供后续审计落库与展示。
    """

    return {
        "step": stage.value,
        "status": "completed",
        "content": content,
        "previous": previous,
        "tool_calls": [record.model_dump(mode="json") for record in tool_calls],
    }


def _observation(record: ToolCallRecord) -> str:
    """把工具调用记录转成回填给模型的观察文本（推理 → 行动 → 观察）。"""

    if record.status is ToolCallStatus.FAILED:
        return json.dumps(
            {"status": record.status.value, "error": record.error},
            ensure_ascii=False,
        )
    return json.dumps(
        {"status": record.status.value, "output": record.output},
        ensure_ascii=False,
        default=str,
    )


def _invoke_role(
    messages: list[Any],
    llm: BaseChatModel,
    caller: ToolCaller | None,
) -> Any:
    """执行角色节点：接入注册表时按模型请求调用工具，否则单次调用模型。"""

    if caller is None or not caller.has_tools():
        return llm.invoke(messages)

    try:
        model = llm.bind_tools(caller.openai_tools())
    except (AttributeError, NotImplementedError):
        # 模型不支持工具调用时退回普通对话，不阻断流水线。
        return llm.invoke(messages)

    response = model.invoke(messages)
    for _ in range(TOOL_CALL_MAX_ITERATIONS):
        requested = list(getattr(response, "tool_calls", None) or [])
        if not requested:
            break
        messages.append(response)
        for call in requested:
            arguments = call.get("args")
            record = caller.invoke(
                str(call.get("name") or ""),
                arguments if isinstance(arguments, dict) else {"value": arguments},
            )
            messages.append(
                ToolMessage(
                    content=_observation(record),
                    tool_call_id=str(call.get("id") or record.call_id),
                )
            )
        response = model.invoke(messages)
    return response


def _run_role_stage(
    stage: PipelineStage,
    task: str,
    previous: dict[str, Any] | None,
    llm: BaseChatModel,
    caller: ToolCaller | None = None,
) -> dict[str, Any]:
    role = role_for_stage(stage)
    definition = get_role(role)
    messages = [
        SystemMessage(content=definition.system_prompt),
        HumanMessage(content=_role_input(role, task, previous)),
    ]
    response = _invoke_role(messages, llm, caller)
    return _stage_result(
        stage,
        _content_text(response.content),
        previous,
        caller.records if caller is not None else (),
    )


def run_role_stage(
    stage: PipelineStage | str,
    task: str,
    previous: dict[str, Any] | None = None,
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_scope: str | None = None,
) -> dict[str, Any]:
    """调用指定阶段对应角色的模型，返回 Workflow 阶段活动使用的载荷。

    ``app.workflows.pipeline`` 的阶段活动据此生成阶段内容；纯函数不触碰
    PipelineState，状态推进仍由 Workflow 侧按 ``complete_step`` 完成。

    传入 ``tool_registry``（或解析到默认注册表）时，角色节点可发现并调用 MCP 工具；
    可持久化执行（Dapr Workflow）应给出稳定的 ``tool_scope``（如 Workflow 实例 ID），
    使同一次执行重放时得到相同的调用 ID（见 `doc/dapr-integration.md` §6）。
    """

    resolved = PipelineStage(stage) if isinstance(stage, str) else stage
    model = llm or build_chat_model(settings or get_settings())
    registry = tool_registry if tool_registry is not None else default_tool_registry()
    caller = ToolCaller(registry, scope=tool_scope) if registry is not None else None
    return _run_role_stage(resolved, task, previous, model, caller)


def _state_update(state: PipelineState) -> dict[str, Any]:
    """把 PipelineState 的推进结果转成 LangGraph 节点返回的状态更新。"""
    return {
        "status": state.status,
        "current_step": state.current_step,
        "completed_steps": list(state.completed_steps),
        "results": state.results,
        "updated_at": state.updated_at,
    }


def build_multi_agent_pipeline(
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
):
    """构建固定三步的多 Agent LangGraph 图：collector → analyst → reporter。

    ``tool_registry`` 为 None 时使用 ``default_tool_registry()``：解析不到注册表
    （例如 ``app/mcp`` 尚未提供）则各节点不调用工具。
    """

    model = llm or build_chat_model(settings or get_settings())
    registry = tool_registry if tool_registry is not None else default_tool_registry()

    def make_node(stage: PipelineStage):
        role = role_for_stage(stage)

        def node(state: PipelineState) -> dict[str, Any]:
            if state.status is not PipelineStatus.RUNNING:
                raise ValueError(
                    f"只能在 running 状态下执行角色 {role.value}"
                )
            if state.current_step is not stage:
                current = (
                    state.current_step.value if state.current_step else "无"
                )
                raise ValueError(
                    f"当前步骤是 {current}，不能执行角色 {role.value}"
                )

            previous_stage = _previous_stage(stage)
            previous = (
                state.results.get(previous_stage.value)
                if previous_stage is not None
                else None
            )
            caller = ToolCaller(registry) if registry is not None else None
            result = _run_role_stage(stage, state.task, previous, model, caller)
            return _state_update(complete_step(state, stage, result))

        return node

    builder = StateGraph(PipelineState)
    order = graph_node_order()
    for stage in PIPELINE_STEPS:
        role = role_for_stage(stage)
        builder.add_node(role.value, make_node(stage))

    builder.add_edge(START, order[0])
    for current, following in zip(order, order[1:]):
        builder.add_edge(current, following)
    builder.add_edge(order[-1], END)
    return builder.compile()


def _previous_stage(stage: PipelineStage) -> PipelineStage | None:
    """返回固定流水线中当前阶段的上游阶段，首阶段返回 None。"""
    index = PIPELINE_STEPS.index(stage)
    return PIPELINE_STEPS[index - 1] if index > 0 else None


def run_multi_agent_pipeline(
    task: str,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
) -> PipelineState:
    """用完整 LangGraph 图执行一次三步协作，返回终态 PipelineState。"""

    graph = build_multi_agent_pipeline(
        llm=llm,
        settings=settings,
        tool_registry=tool_registry,
    )
    output = graph.invoke(start(new_pipeline_state(task)))
    return PipelineState.model_validate(output)

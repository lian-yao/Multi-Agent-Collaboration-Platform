"""成员 A D5-6：多 Agent 流水线的 LangGraph 图与角色分配。

职责边界（分工.md §2/§3、ADR-006、doc/dapr-integration.md §2）：
- 本模块只提供编排层内容：图拓扑、节点执行顺序、状态 Schema 与角色分配；
- 角色静态内容（system_prompt 等）来自 ``app.agents.roles``，由成员 C 维护，
  接线时统一经 ``get_role(role_id)`` 获取；
- Workflow 侧的接线（替换 ``fake_stage_result``）属于成员 B 的 D5-6 交付，
  本模块提供与 fake 结果同构的 ``run_role_stage``，供其按序调用。

固定三步流水线将 ``PipelineStage`` 依次分配给协作角色：

``collect → collector``、``analyze → analyst``、``report → reporter``。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
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

# 流水线阶段 → 协作角色 的固定分配，是 M3 固定团队的事实来源之一。
PIPELINE_ROLE_ASSIGNMENT: dict[PipelineStage, RoleId] = {
    PipelineStage.COLLECT: RoleId.COLLECTOR,
    PipelineStage.ANALYZE: RoleId.ANALYST,
    PipelineStage.REPORT: RoleId.REPORTER,
}

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
) -> dict[str, Any]:
    """构造与 Workflow fake 结果同构的阶段载荷，便于成员 B 直接替换。"""
    return {
        "step": stage.value,
        "status": "completed",
        "content": content,
        "previous": previous,
    }


def _run_role_stage(
    stage: PipelineStage,
    task: str,
    previous: dict[str, Any] | None,
    llm: BaseChatModel,
) -> dict[str, Any]:
    role = role_for_stage(stage)
    definition = get_role(role)
    messages = [
        SystemMessage(content=definition.system_prompt),
        HumanMessage(content=_role_input(role, task, previous)),
    ]
    response = llm.invoke(messages)
    return _stage_result(
        stage,
        _content_text(response.content),
        previous,
    )


def run_role_stage(
    stage: PipelineStage | str,
    task: str,
    previous: dict[str, Any] | None = None,
    *,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
) -> dict[str, Any]:
    """调用指定阶段对应角色的模型，返回阶段结果（fake 结果同构）。

    Workflow 步骤活动可据此替换 ``fake_stage_result``；纯函数不触碰
    PipelineState，状态推进仍由成员 B 侧按 ``complete_step`` 完成。
    """

    resolved = PipelineStage(stage) if isinstance(stage, str) else stage
    model = llm or build_chat_model(settings or get_settings())
    return _run_role_stage(resolved, task, previous, model)


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
):
    """构建固定三步的多 Agent LangGraph 图：collector → analyst → reporter。"""

    model = llm or build_chat_model(settings or get_settings())

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
            result = _run_role_stage(stage, state.task, previous, model)
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
) -> PipelineState:
    """用完整 LangGraph 图执行一次三步协作，返回终态 PipelineState。"""

    graph = build_multi_agent_pipeline(llm=llm, settings=settings)
    output = graph.invoke(start(new_pipeline_state(task)))
    return PipelineState.model_validate(output)

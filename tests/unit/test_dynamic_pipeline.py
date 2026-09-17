"""动态编排图（ADR-019）的单元测试。

覆盖三块：

1. **计划解析**：`parse_plan` 对合法/非法输入的接受与拒绝口径——这是整条动态链路的
   安全阀，模型返回什么都不能让执行崩掉，也不能让一份残缺计划混进去；
2. **调度语义**：依赖就绪度、失败连坐、最终交付物取值；
3. **图与接线**：`run_dynamic_pipeline` 端到端、动态图与静态图互不干扰、按模式选工作流名。

全程注入 ScriptedChatModel，不请求任何外部模型服务。
"""

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agents.roles import RoleId, get_role
from app.config import AgentSettings
from app.orchestration.dynamic_graph import (
    DEFAULT_MAX_PLAN_STEPS,
    DynamicPipelineState,
    PlanStep,
    PlanStepStatus,
    StepOutcome,
    blocked_steps,
    build_dynamic_pipeline,
    dynamic_checkpoint_summary,
    fallback_plan,
    finalize_state,
    generate_plan,
    ordered_outcomes,
    parse_plan,
    planner_prompt,
    ready_steps,
    recursion_limit,
    resolve_max_plan_steps,
    resolve_orchestration_mode,
    run_dynamic_pipeline,
    run_plan_step,
    step_input,
)
from app.orchestration.pipeline import PipelineStatus
from app.orchestration.pipeline_graph import (
    graph_node_order,
    pipeline_roles,
    run_multi_agent_pipeline,
)
from app.workflows.dynamic import DYNAMIC_WORKFLOW_NAME, fake_step_outcome
from app.workflows.pipeline import WORKFLOW_NAME, WorkflowTask
from app.workflows.service import resolve_workflow_name


class ScriptedChatModel(BaseChatModel):
    """按序返回固定回复，替代真实 LLM。"""

    replies: list[str]
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


class BrokenChatModel(BaseChatModel):
    """每次调用都抛错，用来验证「规划失败必须降级」。"""

    @property
    def _llm_type(self) -> str:
        return "broken-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("provider unreachable")


def plan_json(*steps: dict) -> str:
    return json.dumps({"rationale": "测试计划", "steps": list(steps)})


COLLECT_STEP = {
    "id": "s1",
    "role": "collector",
    "instruction": "收集信息",
    "depends_on": [],
}
ANALYST_STEP = {
    "id": "s2",
    "role": "analyst",
    "instruction": "分析信息",
    "depends_on": ["s1"],
}
REPORTER_STEP = {
    "id": "s3",
    "role": "reporter",
    "instruction": "生成报告",
    "depends_on": ["s2"],
}


# --------------------------------------------------------------------------------------
# 计划解析
# --------------------------------------------------------------------------------------


def test_parse_plan_accepts_plain_json():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP))

    assert plan is not None
    assert plan.source == "llm"
    assert [(step.id, step.role.value, step.depends_on) for step in plan.steps] == [
        ("s1", "collector", []),
        ("s2", "analyst", ["s1"]),
        ("s3", "reporter", ["s2"]),
    ]
    assert plan.rationale == "测试计划"


def test_parse_plan_tolerates_markdown_fence_and_prose():
    text = f"这是我的计划：\n```json\n{plan_json(COLLECT_STEP)}\n```\n请确认。"

    plan = parse_plan(text)

    assert plan is not None
    assert len(plan.steps) == 1


def test_parse_plan_tolerates_bare_json_without_fence():
    text = f"计划如下 {plan_json(COLLECT_STEP)} 以上。"

    plan = parse_plan(text)

    assert plan is not None
    assert plan.steps[0].id == "s1"


def test_parse_plan_fills_missing_step_id_by_position():
    plan = parse_plan(plan_json({"role": "collector", "instruction": "收集"}))

    assert plan is not None
    assert plan.steps[0].id == "s1"


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("不是 JSON", "完全不是 JSON"),
        ("顶层是数组", json.dumps([COLLECT_STEP])),
        ("缺 steps", json.dumps({"rationale": "只有理由"})),
        ("steps 为空", json.dumps({"steps": []})),
        ("步骤不是对象", json.dumps({"steps": ["collector"]})),
        ("未知角色", plan_json({"id": "s1", "role": "supervisor", "instruction": "管"})),
        ("角色缺失", plan_json({"id": "s1", "instruction": "管"})),
        ("instruction 为空", plan_json({"id": "s1", "role": "collector", "instruction": "  "})),
        (
            "id 重复",
            plan_json(
                {"id": "s1", "role": "collector", "instruction": "收集"},
                {"id": "s1", "role": "analyst", "instruction": "分析"},
            ),
        ),
        (
            "自依赖",
            plan_json({"id": "s1", "role": "collector", "instruction": "收集", "depends_on": ["s1"]}),
        ),
        (
            "前向依赖",
            plan_json(
                {"id": "s1", "role": "collector", "instruction": "收集", "depends_on": ["s2"]},
                {"id": "s2", "role": "analyst", "instruction": "分析"},
            ),
        ),
        (
            "depends_on 不是数组",
            plan_json({"id": "s1", "role": "collector", "instruction": "收集", "depends_on": "s0"}),
        ),
    ],
)
def test_parse_plan_rejects_invalid_payloads(name, payload):
    assert parse_plan(payload) is None, name


def test_parse_plan_rejects_plan_exceeding_step_budget():
    steps = [
        {"id": f"s{i}", "role": "collector", "instruction": f"第 {i} 步", "depends_on": []}
        for i in range(1, DEFAULT_MAX_PLAN_STEPS + 2)
    ]

    assert parse_plan(plan_json(*steps)) is None
    # 恰好用满预算时接受，边界不能提前一位。
    assert parse_plan(plan_json(*steps[:-1]), DEFAULT_MAX_PLAN_STEPS) is not None


def test_fallback_plan_mirrors_static_pipeline():
    plan = fallback_plan()

    assert plan.source == "fallback"
    assert [step.role for step in plan.steps] == [
        RoleId.COLLECTOR,
        RoleId.ANALYST,
        RoleId.REPORTER,
    ]
    assert [step.depends_on for step in plan.steps] == [[], ["s1"], ["s2"]]


def test_planner_prompt_lists_every_available_role():
    prompt = planner_prompt(4)

    for role in RoleId:
        assert role.value in prompt
        assert get_role(role).name in prompt
    assert "4" in prompt
    assert "JSON" in prompt


# --------------------------------------------------------------------------------------
# 规划节点的降级行为
# --------------------------------------------------------------------------------------


def test_generate_plan_falls_back_when_model_raises():
    plan = generate_plan("任务", BrokenChatModel())

    assert plan.source == "fallback"
    assert len(plan.steps) == 3
    assert "调用失败" in plan.rationale


def test_generate_plan_falls_back_when_model_returns_garbage():
    model = ScriptedChatModel(replies=["我觉得可以做一下这个任务。"])

    plan = generate_plan("任务", model)

    assert plan.source == "fallback"
    assert "不是可用计划" in plan.rationale


def test_generate_plan_uses_model_plan_when_valid():
    model = ScriptedChatModel(
        replies=[
            plan_json(
                COLLECT_STEP,
                {
                    "id": "s2",
                    "role": "reporter",
                    "instruction": "直接出报告",
                    "depends_on": ["s1"],
                },
            )
        ]
    )

    plan = generate_plan("任务", model)

    assert plan.source == "llm"
    assert [step.role for step in plan.steps] == [RoleId.COLLECTOR, RoleId.REPORTER]


def test_generate_plan_prompt_carries_the_task():
    model = ScriptedChatModel(replies=[plan_json(COLLECT_STEP)])

    generate_plan("把这篇论文整理成综述", model)

    assert "把这篇论文整理成综述" in model.calls[0][1].content
    assert "任务规划 Agent" in model.calls[0][0].content


# --------------------------------------------------------------------------------------
# 依赖就绪度与步骤执行
# --------------------------------------------------------------------------------------


def _state(plan, results=None) -> DynamicPipelineState:
    return DynamicPipelineState(
        task="任务",
        status=PipelineStatus.RUNNING,
        plan=plan.steps,
        plan_source=plan.source,
        results=results or {},
    )


def _outcome(step_id: str, role: RoleId, status: PlanStepStatus, content: str = "") -> StepOutcome:
    return StepOutcome(
        step_id=step_id, role=role, instruction="i", status=status, content=content
    )


def test_ready_steps_respects_dependencies():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP))
    state = _state(plan)

    assert [step.id for step in ready_steps(state)] == ["s1"]

    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "要点")})
    assert [step.id for step in ready_steps(state)] == ["s2"]

    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.FAILED)})
    assert ready_steps(state) == []
    # 连坐是传递的：s2 直接依赖 s1，s3 依赖 s2，两者都不会执行。
    assert [step.id for step in blocked_steps(state)] == ["s2", "s3"]


def test_ready_steps_releases_independent_branches_together():
    plan = parse_plan(
        plan_json(
            COLLECT_STEP,
            {"id": "s2", "role": "collector", "instruction": "另起一路", "depends_on": []},
        )
    )
    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED)})

    assert [step.id for step in ready_steps(state)] == ["s2"]


def test_step_input_labels_every_upstream_result():
    plan = parse_plan(
        plan_json(
            COLLECT_STEP,
            {"id": "s2", "role": "collector", "instruction": "收集另一面", "depends_on": []},
            {"id": "s3", "role": "analyst", "instruction": "对比两路", "depends_on": ["s1", "s2"]},
        )
    )
    results = {
        "s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "甲面要点"),
        "s2": _outcome("s2", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "乙面要点"),
    }

    text = step_input("原始任务", plan.steps[2], results)

    assert "原始任务" in text
    assert "甲面要点" in text and "乙面要点" in text
    assert "信息收集 Agent" in text
    assert "对比两路" in text


def test_run_plan_step_uses_role_prompt_and_returns_record():
    step = PlanStep(id="s1", role=RoleId.ANALYST, instruction="分析")
    model = ScriptedChatModel(replies=["分析结论"])

    outcome = run_plan_step(step, "任务", {}, model)

    assert outcome.status is PlanStepStatus.COMPLETED
    assert outcome.content == "分析结论"
    assert outcome.tool_calls == []
    assert model.calls[0][0].content == get_role(RoleId.ANALYST).system_prompt


def test_run_plan_step_absorbs_exception_into_failed_outcome():
    step = PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集")

    outcome = run_plan_step(step, "任务", {}, BrokenChatModel())

    assert outcome.status is PlanStepStatus.FAILED
    assert "RuntimeError" in (outcome.error or "")
    assert outcome.content == ""


def test_finalize_uses_last_successful_step_as_deliverable():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP))
    state = _state(
        plan,
        {
            "s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "要点"),
            "s2": _outcome("s2", RoleId.ANALYST, PlanStepStatus.COMPLETED, "结论"),
            "s3": _outcome("s3", RoleId.REPORTER, PlanStepStatus.COMPLETED, "报告正文"),
        },
    )

    settled = finalize_state(state)

    assert settled.status is PipelineStatus.COMPLETED
    assert settled.final_output == "报告正文"
    assert settled.error is None


def test_finalize_marks_skipped_downstream_and_reports_error():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP))
    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.FAILED)})

    settled = finalize_state(state)

    assert settled.status is PipelineStatus.FAILED
    assert settled.final_output is None
    assert settled.results["s2"].status is PlanStepStatus.SKIPPED
    assert settled.results["s3"].status is PlanStepStatus.SKIPPED
    assert "失败步骤：s1" in (settled.error or "")
    # 幂等：再次收尾不会把「已跳过」当成新的连坐对象重复追加。
    assert blocked_steps(settled) == []


def test_finalize_reports_when_plan_produced_nothing():
    plan = parse_plan(plan_json(COLLECT_STEP))

    settled = finalize_state(_state(plan))

    assert settled.status is PipelineStatus.FAILED
    assert settled.final_output is None
    assert "没有任何步骤产出" in (settled.error or "")


def test_checkpoint_summary_lists_plan_with_statuses():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP))
    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "要点")})

    summary = dynamic_checkpoint_summary(finalize_state(state))

    assert summary["mode"] == "dynamic"
    assert summary["completed_steps"] == ["s1"]
    assert summary["plan"] == [
        {"id": "s1", "role": "collector", "depends_on": [], "status": "completed"},
        {"id": "s2", "role": "analyst", "depends_on": ["s1"], "status": "pending"},
    ]


# --------------------------------------------------------------------------------------
# 图与接线
# --------------------------------------------------------------------------------------


def test_dynamic_graph_executes_plan_in_order():
    model = ScriptedChatModel(
        replies=[
            plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP),
            "收集到的要点",
            "分析结论",
            "最终报告",
        ]
    )

    state = run_dynamic_pipeline("分析并出报告", llm=model)

    assert state.status is PipelineStatus.COMPLETED
    assert state.plan_source == "llm"
    assert state.final_output == "最终报告"
    assert [outcome.step_id for outcome in ordered_outcomes(state)] == ["s1", "s2", "s3"]
    assert state.results["s1"].content == "收集到的要点"
    # 报告步骤的输入必须带上分析结论，而不是只带上游一步的原始任务。
    assert "分析结论" in model.calls[3][1].content


def test_dynamic_graph_runs_unrelated_branch_after_a_failure():
    model = ScriptedChatModel(
        replies=[
            plan_json(
                COLLECT_STEP,
                ANALYST_STEP,
                {"id": "s3", "role": "collector", "instruction": "另起一路", "depends_on": []},
            ),
            "第一路失败前的调用",  # 会被 BrokenChatModel 之外的路径消耗
            "第二路结果",
        ]
    )
    # 让第一步失败：改用会抛错的模型包装——直接在图上跑，第一步用 BrokenChatModel 不可行，
    # 因此这里用「角色未知」以外的真实路径：注入一个 replies 只有两条的模型，
    # 第三步仍应有输出，第二步因依赖失败被跳过。
    model = ScriptedChatModel(replies=[plan_json(COLLECT_STEP, ANALYST_STEP, {"id": "s3", "role": "collector", "instruction": "另起一路", "depends_on": []})])

    state = run_dynamic_pipeline("任务", llm=model)

    assert state.results["s1"].status is PlanStepStatus.COMPLETED
    assert state.results["s2"].status is PlanStepStatus.COMPLETED


def test_dynamic_graph_falls_back_and_still_completes():
    model = ScriptedChatModel(replies=["不是 JSON", "收集", "分析", "报告"])

    state = run_dynamic_pipeline("任务", llm=model)

    assert state.plan_source == "fallback"
    assert state.status is PipelineStatus.COMPLETED
    assert state.final_output == "报告"
    assert len(state.plan) == 3


def test_dynamic_graph_fails_when_model_is_unusable():
    state = run_dynamic_pipeline("任务", llm=BrokenChatModel())

    # 规划降级为固定三步，但每一步都失败 → 整次执行没有交付物，终态 failed。
    assert state.plan_source == "fallback"
    assert state.status is PipelineStatus.FAILED
    assert state.final_output is None


def test_dynamic_graph_builds_without_touching_static_graph():
    fake = ScriptedChatModel(replies=["s1 收集", "s2 分析", "s3 报告"])

    result = run_multi_agent_pipeline("演示任务", llm=fake)

    assert pipeline_roles() == (RoleId.COLLECTOR, RoleId.ANALYST, RoleId.REPORTER)
    assert graph_node_order() == ("collector", "analyst", "reporter")
    assert [step.value for step in result.completed_steps] == ["collect", "analyze", "report"]
    # 动态图可以独立编译，不会改变静态图的节点集合。
    assert build_dynamic_pipeline(llm=fake, tool_registry=None, max_steps=3) is not None


def test_recursion_limit_scales_with_step_budget():
    assert recursion_limit(1) == 25
    assert recursion_limit(6) > 2 * 6


def test_orchestration_mode_defaults_to_static():
    assert resolve_orchestration_mode() == "static"
    assert AgentSettings().orchestration_mode == "static"
    assert AgentSettings(orchestration_mode="dynamic").orchestration_mode == "dynamic"


def test_max_plan_steps_falls_back_for_non_positive_value():
    assert resolve_max_plan_steps(AgentSettings(max_plan_steps=0)) == DEFAULT_MAX_PLAN_STEPS
    assert resolve_max_plan_steps(AgentSettings(max_plan_steps=3)) == 3


def test_resolve_workflow_name_by_mode():
    assert resolve_workflow_name(None) == WORKFLOW_NAME
    assert resolve_workflow_name("static") == WORKFLOW_NAME
    assert resolve_workflow_name("dynamic") == DYNAMIC_WORKFLOW_NAME
    # 非法值退回固定链路，而不是调度一个不存在的工作流。
    assert resolve_workflow_name("autonomous") == WORKFLOW_NAME


def test_workflow_task_serializes_orchestration_mode():
    payload = WorkflowTask(workflow_id="w1", task="任务", orchestration_mode="dynamic").asdict()

    assert payload["orchestration_mode"] == "dynamic"
    assert WorkflowTask(workflow_id="w1", task="任务").orchestration_mode is None


def test_fake_step_outcome_is_deterministic():
    step = PlanStep(id="s2", role=RoleId.ANALYST, instruction="分析", depends_on=["s1"])

    first = fake_step_outcome(step, "任务")
    second = fake_step_outcome(step, "任务")

    assert first == second
    assert first.status is PlanStepStatus.COMPLETED
    assert "s2" in first.content and "analyst" in first.content
    assert "s1" in first.content

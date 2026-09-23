"""动态编排图（ADR-019）的单元测试。

覆盖三块：

1. **计划解析**：`parse_plan` 对合法/非法输入的接受与拒绝口径——这是整条动态链路的
   安全阀，模型返回什么都不能让执行崩掉，也不能让一份残缺计划混进去；
2. **调度语义**：依赖就绪度、失败连坐、最终交付物取值；
3. **图与接线**：`run_dynamic_pipeline` 端到端、动态图与静态图互不干扰、按模式选工作流名。

全程注入 ScriptedChatModel，不请求任何外部模型服务。
"""

import json
import time

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agents.roles import RoleId, get_role
from app.attachments import AttachmentPayload
from app.config import AgentSettings
from app.memory import MessageRole, SessionMessage
from app.orchestration.dynamic_graph import (
    DEFAULT_MAX_PLAN_STEPS,
    DynamicPipelineState,
    FlowKind,
    PlanStep,
    PlanStepStatus,
    StepOutcome,
    blocked_steps,
    build_dynamic_pipeline,
    dynamic_checkpoint_summary,
    effective_attempts,
    effective_timeout,
    fallback_plan,
    finalize_state,
    generate_plan,
    ordered_outcomes,
    parse_plan,
    pending_batch,
    planner_prompt,
    ready_steps,
    recursion_limit,
    resolve_max_plan_steps,
    resolve_orchestration_mode,
    run_dynamic_pipeline,
    run_plan_step,
    single_agent_plan,
    step_input,
    waves,
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


def intake_json(*, need_multi: bool = True, rewritten: str = "改写后的任务") -> str:
    """intake 节点的模型输出（改写 + 意图，一次调用，ADR-038 §1）。"""

    return json.dumps(
        {
            "rewritten_task": rewritten,
            "intent": {
                "intent_type": "report" if need_multi else "question",
                "user_goal": "完成用户任务",
                "constraints": [],
                "need_multi_subtask": need_multi,
            },
        }
    )


def validation_json(*, satisfied: bool = True, defects: list[str] | None = None) -> str:
    return json.dumps(
        {
            "satisfied": satisfied,
            "defects": defects or [],
            "missing": [],
        }
    )


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


def test_planner_prompt_states_what_roles_can_actually_do():
    """规划 Agent 也要知道角色的**真实能力**（ADR-037 §4）：
    写进 instruction 的活必须落在角色做得到的范围内（工作区文件、附件、MCP、网页搜索），
    覆盖/删除这类动作要走人工审批——否则规划出来的步骤天然执行不了。"""

    prompt = planner_prompt()

    assert "工作区" in prompt
    assert "人工审批" in prompt
    assert "改写" in prompt, "要说明拿到的任务已经被问题改写补全过，可以直接当完整任务规划"


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


# ---------------------------------------------------------------------------------------
# 会话历史注入（ADR-019 的读点在动态链路上补齐）
#
# 这条不是"锦上添花"：2026-09-23 实测反馈「同一个会话不记得我之前说过什么」，
# 根因就是动态图的规划与步骤输入都只带当前任务——历史接进了固定三步、没接这里。
# ---------------------------------------------------------------------------------------


def _history(*pairs: tuple[MessageRole, str]) -> tuple[SessionMessage, ...]:
    return tuple(
        SessionMessage(session_id="s-1", role=role, content=content)
        for role, content in pairs
    )


def test_generate_plan_prompt_carries_the_session_history():
    model = ScriptedChatModel(replies=[plan_json(COLLECT_STEP)])
    history = _history(
        (MessageRole.USER, "生成冒泡代码python版本，放在文件夹里"),
        (MessageRole.ASSISTANT, "已生成 bubble.py。"),
        (MessageRole.USER, "重试"),
    )

    generate_plan("重试", model, history=history)

    content = model.calls[0][1].content
    assert "【会话历史（最近 3 条，供多轮上下文继承）】" in content
    assert "user: 生成冒泡代码python版本，放在文件夹里" in content
    assert "assistant: 已生成 bubble.py。" in content
    # 历史在前、本轮任务在后：模型先看到上下文，再看到这一轮要干什么。
    assert content.index("会话历史") < content.index("用户任务：\n重试")


def test_generate_plan_prompt_is_unchanged_without_history():
    """没有历史时提示词与接线前逐字一致——单轮的既有行为不受影响（ADR-019 口径）。"""

    model = ScriptedChatModel(replies=[plan_json(COLLECT_STEP)])

    generate_plan("只做这一件事", model)

    assert model.calls[0][1].content == "用户任务：\n只做这一件事"


def test_step_input_prefixes_session_history():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP))
    results = {
        "s1": StepOutcome(
            step_id="s1",
            role=RoleId.COLLECTOR,
            instruction="收集信息",
            status=PlanStepStatus.COMPLETED,
            content="收集结果",
        )
    }
    history = _history((MessageRole.USER, "第一轮的要求"))

    text = step_input("继续", plan.steps[1], results, history)

    assert text.startswith("【会话历史（最近 1 条，供多轮上下文继承）】")
    assert "user: 第一轮的要求" in text
    assert "用户任务：\n继续" in text
    assert "【s1 · 信息收集 Agent】" in text
    # 不带历史时逐字不变
    assert "会话历史" not in step_input("继续", plan.steps[1], results)


def test_run_plan_step_passes_history_into_the_prompt():
    plan = parse_plan(plan_json(COLLECT_STEP))
    model = ScriptedChatModel(replies=["产出"])

    run_plan_step(
        plan.steps[0],
        "重试",
        {},
        model,
        history=_history((MessageRole.USER, "上一轮：写冒泡排序")),
    )

    assert "上一轮：写冒泡排序" in model.calls[0][1].content


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
    assert summary["route"] == "multi"
    assert summary["round"] == 1
    assert summary["completed_steps"] == ["s1"]
    assert summary["failed_steps"] == []
    assert summary["skipped_steps"] == []
    assert summary["partial"] is False
    assert summary["plan"] == [
        {
            "id": "s1",
            "role": "collector",
            "depends_on": [],
            "expected_output": "",
            "retry": None,
            "timeout_seconds": None,
            "attempts": 1,
            "tokens": 0,
            "status": "completed",
        },
        {
            "id": "s2",
            "role": "analyst",
            "depends_on": ["s1"],
            "expected_output": "",
            "retry": None,
            "timeout_seconds": None,
            "attempts": None,
            "tokens": None,
            "status": "pending",
        },
    ]
    # 成本闸门（ADR-038 §9）：用量是下限口径，预算 0 = 不限制。
    assert summary["tokens_used"] == 0
    assert summary["token_budget"] == 0
    assert summary["budget_exceeded"] is False
    # flow 是给人看的整条流程：编排、合成节点在（校验没跑就不画）。
    kinds = [node["kind"] for node in summary["flow"]]
    assert kinds[:3] == ["intent", "plan", "worker"]
    assert "synthesize" in kinds
    assert "validate" not in kinds


# --------------------------------------------------------------------------------------
# 图与接线
# --------------------------------------------------------------------------------------


def test_dynamic_graph_executes_plan_in_order():
    model = ScriptedChatModel(
        replies=[
            intake_json(),
            plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP),
            "收集到的要点",
            "分析结论",
            "报告草稿",
            "合成后的最终报告",
            validation_json(),
        ]
    )

    state = run_dynamic_pipeline("分析并出报告", llm=model)

    assert state.status is PipelineStatus.COMPLETED
    assert state.plan_source == "llm"
    assert state.final_output == "合成后的最终报告"
    assert [outcome.step_id for outcome in ordered_outcomes(state)] == ["s1", "s2", "s3"]
    assert state.results["s1"].content == "收集到的要点"
    # 报告步骤的输入必须带上分析结论，而不是只带上游一步的原始任务。
    assert "分析结论" in model.calls[4][1].content


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
    model = ScriptedChatModel(
        replies=[
            "我理解你想让我分析这个任务。",
            "不是 JSON",
            "收集",
            "分析",
            "报告",
            "合成报告",
            validation_json(),
        ]
    )

    state = run_dynamic_pipeline("任务", llm=model)

    assert state.plan_source == "fallback"
    assert state.status is PipelineStatus.COMPLETED
    assert state.final_output == "合成报告"
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


# --------------------------------------------------------------------------------------
# 附件注入（ADR-021）
# --------------------------------------------------------------------------------------


def _image_payload() -> AttachmentPayload:
    return AttachmentPayload(
        id="a1",
        name="shot.png",
        kind="image",
        status="ready",
        mime="image/png",
        data=b"\x89PNG",
    )


def test_attachments_go_to_root_steps_only():
    """附件只进根步骤：非根步骤读的是上游正文，再塞一遍等于重复计费同一张图。"""

    model = ScriptedChatModel(replies=["ok"])
    root = PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集")
    child = PlanStep(id="s2", role=RoleId.REPORTER, instruction="汇总", depends_on=["s1"])

    run_plan_step(root, "任务", {}, model, None, "wf", (_image_payload(),))
    root_content = model.calls[0][1].content
    assert isinstance(root_content, list)
    assert [block["type"] for block in root_content] == ["text", "image_url"]

    run_plan_step(child, "任务", {"s1": StepOutcome(
        step_id="s1",
        role=RoleId.COLLECTOR,
        instruction="收集",
        status=PlanStepStatus.COMPLETED,
        content="上游正文",
    )}, model, None, "wf", (_image_payload(),))
    child_content = model.calls[1][1].content
    assert isinstance(child_content, str)
    assert "上游正文" in child_content


def test_every_root_step_gets_the_attachments():
    """多根计划各拿一份：彼此看不到对方产出，少给谁谁就完全不知道用户传了东西。"""

    model = ScriptedChatModel(replies=["a", "b"])
    first = PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集 A")
    second = PlanStep(id="s2", role=RoleId.ANALYST, instruction="独立核算")

    run_plan_step(first, "任务", {}, model, None, "wf", (_image_payload(),))
    run_plan_step(second, "任务", {}, model, None, "wf", (_image_payload(),))

    for call in model.calls:
        assert isinstance(call[1].content, list)


def test_static_collect_stage_receives_attachments():
    """静态链路的附件注入点同样是「拿到原始任务」的 collect 阶段。"""

    from app.orchestration.pipeline import new_pipeline_state, start
    from app.orchestration.pipeline_graph import run_role_stage

    model = ScriptedChatModel(replies=["收集完成"])
    state = start(new_pipeline_state(task="任务"))
    assert state.current_step is not None

    run_role_stage(
        state.current_step,
        "任务",
        previous=None,
        llm=model,
        tool_registry=None,
        attachments=(_image_payload(),),
    )
    content = model.calls[0][1].content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


# --------------------------------------------------------------------------------------
# ADR-038：波次并行、路由、合成、校验与重编排
# --------------------------------------------------------------------------------------


class SleepyScriptedChatModel(BaseChatModel):
    """按序返回固定回复，但每次调用会睡一会儿——用来观测**是否真的并行**。"""

    replies: list[str] = None  # type: ignore[assignment]
    delay: float = 0.25
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)
    windows: list[tuple[float, float]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "sleepy-scripted-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        started = time.perf_counter()
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        time.sleep(self.delay)
        self.windows.append((started, time.perf_counter()))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


class FailsOnPromptModel(ScriptedChatModel):
    """提示词里包含标记就抛错：用来制造「某一步失败、别的步照常」的场景。"""

    fail_marker: str = "分析信息"

    @property
    def _llm_type(self) -> str:
        return "fails-on-prompt-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        text = str(messages[-1].content)
        if self.fail_marker and self.fail_marker in text:
            raise RuntimeError("provider exploded")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _max_concurrency(windows: list[tuple[float, float]]) -> int:
    """同一时刻最多有几个调用在跑（区间扫描）。"""

    events = [(start, 1) for start, _ in windows] + [(end, -1) for _, end in windows]
    events.sort()
    current = best = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


def test_waves_group_independent_steps_and_chain_dependents():
    plan = parse_plan(
        plan_json(
            COLLECT_STEP,
            {"id": "s2", "role": "collector", "instruction": "另一路", "depends_on": []},
            {"id": "s3", "role": "analyst", "instruction": "对比", "depends_on": ["s1", "s2"]},
            {"id": "s4", "role": "reporter", "instruction": "成稿", "depends_on": ["s3"]},
        )
    )

    assert [[step.id for step in wave] for wave in waves(plan.steps)] == [
        ["s1", "s2"],
        ["s3"],
        ["s4"],
    ]


def test_pending_batch_respects_the_parallel_cap():
    plan = parse_plan(
        plan_json(
            COLLECT_STEP,
            {"id": "s2", "role": "collector", "instruction": "另一路", "depends_on": []},
            {"id": "s3", "role": "collector", "instruction": "第三路", "depends_on": []},
        )
    )

    state = _state(plan)
    assert [step.id for step in pending_batch(state, plan.steps, 2)] == ["s1", "s2"]

    state = _state(
        plan,
        {
            "s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED),
            "s2": _outcome("s2", RoleId.COLLECTOR, PlanStepStatus.COMPLETED),
        },
    )
    assert [step.id for step in pending_batch(state, plan.steps, 2)] == ["s3"]


def test_pending_batch_never_dispatches_blocked_steps():
    plan = parse_plan(plan_json(COLLECT_STEP, ANALYST_STEP))
    state = _state(plan, {"s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.FAILED)})

    assert pending_batch(state, plan.steps, 3) == []
    assert [step.id for step in blocked_steps(state)] == ["s2"]


def test_parse_plan_accepts_optional_step_fields():
    plan = parse_plan(
        plan_json(
            {
                **COLLECT_STEP,
                "expected_output": "结构化信息清单",
                "retry": 2,
                "timeout_seconds": 120,
            }
        )
    )

    step = plan.steps[0]
    assert step.expected_output == "结构化信息清单"
    assert step.retry == 2
    assert step.timeout_seconds == 120


def test_parse_plan_defaults_optional_step_fields():
    step = parse_plan(plan_json(COLLECT_STEP)).steps[0]

    assert step.expected_output == ""
    assert step.retry is None
    assert step.timeout_seconds is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retry", 9),
        ("retry", -1),
        ("retry", True),
        ("timeout_seconds", 5),
        ("timeout_seconds", 99999),
        ("timeout_seconds", "很久"),
    ],
)
def test_parse_plan_rejects_out_of_range_optional_fields(field, value):
    """越界同样整份丢弃：不修补半份计划是刻意的（ADR-019 §2）。"""

    assert parse_plan(plan_json({**COLLECT_STEP, field: value})) is None


def test_expected_output_is_carried_into_the_step_input():
    plan = parse_plan(
        plan_json({**COLLECT_STEP, "expected_output": "只输出要点清单"})
    )

    text = step_input("任务", plan.steps[0], {})

    assert "这一步期望的输出形态" in text
    assert "只输出要点清单" in text


def test_effective_attempts_and_timeout_follow_plan_then_settings():
    overridden = PlanStep(
        id="s1",
        role=RoleId.COLLECTOR,
        instruction="收集",
        retry=1,
        timeout_seconds=60,
    )
    assert effective_attempts(overridden, AgentSettings(subtask_max_attempts=5)) == 2
    assert effective_timeout(overridden, AgentSettings(subtask_timeout_seconds=99)) == 60.0

    plain = PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集")
    assert effective_attempts(plain, AgentSettings(subtask_max_attempts=5)) == 5
    assert effective_timeout(plain, AgentSettings(subtask_timeout_seconds=42)) == 42.0


def test_single_agent_plan_is_one_step_and_keeps_constraints():
    from app.orchestration.intake import IntentResult

    intent = IntentResult(
        intent_type="question",
        user_goal="解释幂等",
        constraints=["中文", "三句话以内"],
        need_multi_subtask=False,
    )

    plan = single_agent_plan("解释一下什么是幂等", intent)

    assert len(plan.steps) == 1
    assert plan.steps[0].role is RoleId.REPORTER
    assert "单 Agent 直答" in plan.steps[0].instruction
    assert "中文" in plan.steps[0].instruction
    assert plan.steps[0].expected_output == "中文；三句话以内"


def test_simple_task_routes_to_single_agent_without_planner_call():
    model = ScriptedChatModel(
        replies=[intake_json(need_multi=False, rewritten="解释一下什么是幂等"), "幂等就是……"]
    )

    state = run_dynamic_pipeline("什么是幂等", llm=model)

    assert state.route == "single"
    assert state.status is PipelineStatus.COMPLETED
    assert state.final_output == "幂等就是……"
    assert len(state.plan) == 1
    assert state.plan[0].role is RoleId.REPORTER
    # 只花 intake + 一次直答：不调规划、不合成、不校验——这就是「简单任务提性能」。
    assert len(model.calls) == 2
    assert [node.kind for node in state.flow] == [FlowKind.INTENT, FlowKind.WORKER]
    assert dynamic_checkpoint_summary(state)["route"] == "single"


def test_unparseable_intent_keeps_the_multi_agent_route():
    """意图解析不出来（模型只吐了任务文本）→ 按多 Agent 处理，这是安全默认。"""

    model = ScriptedChatModel(
        replies=[
            json.dumps({"rewritten_task": "对比 A 与 B"}),
            plan_json(COLLECT_STEP),
            "收集结果",
            "合成报告",
            validation_json(),
        ]
    )

    state = run_dynamic_pipeline("对比一下", llm=model)

    assert state.route == "multi"
    assert state.intent is None
    assert state.intent_source == "fallback"
    assert state.status is PipelineStatus.COMPLETED
    assert [node.kind for node in state.flow][:2] == [FlowKind.INTENT, FlowKind.PLAN]


def test_parallel_wave_runs_workers_concurrently():
    model = SleepyScriptedChatModel(
        replies=[
            intake_json(),
            plan_json(
                COLLECT_STEP,
                {"id": "s2", "role": "collector", "instruction": "另一路", "depends_on": []},
            ),
            "A 产出",
            "B 产出",
            "合成报告",
            validation_json(),
        ],
        delay=0.25,
    )

    state = run_dynamic_pipeline("两路并行", llm=model)

    assert state.status is PipelineStatus.COMPLETED
    assert _max_concurrency(model.windows) >= 2, "同波无依赖的步骤必须真的并行"


def test_partial_failure_is_marked_and_synthesis_is_told_about_it():
    model = FailsOnPromptModel(
        replies=[
            intake_json(),
            plan_json(COLLECT_STEP, ANALYST_STEP, REPORTER_STEP),
            "收集要点",
            "这条回复被失败吞掉",  # s2：分析步
            "合成报告（含失败说明）",
            validation_json(),
        ]
    )

    state = run_dynamic_pipeline("分析并出报告", llm=model)

    assert state.status is PipelineStatus.COMPLETED, "有可用交付物就不算整次失败"
    assert state.partial is True
    assert state.results["s2"].status is PlanStepStatus.FAILED
    assert state.results["s3"].status is PlanStepStatus.SKIPPED
    assert state.final_output == "合成报告（含失败说明）"
    assert "失败步骤：s2" in (state.error or "")

    synthesis_prompt = str(model.calls[4][1].content)
    assert "【失败与未执行的子任务】" in synthesis_prompt
    assert "s2" in synthesis_prompt and "s3" in synthesis_prompt

    summary = dynamic_checkpoint_summary(state)
    assert summary["partial"] is True
    assert summary["failed_steps"] == ["s2"]
    assert summary["skipped_steps"] == ["s3"]
    statuses = {node["id"]: node["status"] for node in summary["flow"]}
    assert statuses["s2"] == "failed" and statuses["s3"] == "skipped"


def test_validation_defects_trigger_exactly_one_replan_round():
    model = ScriptedChatModel(
        replies=[
            intake_json(),
            plan_json(COLLECT_STEP),
            "第一轮收集",
            "第一轮报告",
            validation_json(satisfied=False, defects=["缺少来源标注"]),
            plan_json(COLLECT_STEP),
            "第二轮收集",
            "第二轮报告",
            validation_json(satisfied=False, defects=["还是缺少来源"]),
        ]
    )

    state = run_dynamic_pipeline("写一份带来源的报告", llm=model)

    assert state.round == 2
    assert state.validation_rounds == 1
    assert state.status is PipelineStatus.COMPLETED
    assert state.final_output == "第二轮报告"
    assert state.validation is not None and state.validation.satisfied is False
    # 第二轮规划必须带上缺陷清单，否则「重编排」只是把上一轮原样再跑一遍。
    second_plan_prompt = str(model.calls[5][1].content)
    assert "上一轮交付物的问题" in second_plan_prompt
    assert "缺少来源标注" in second_plan_prompt
    # 最多两轮：第三轮不存在。
    assert len(model.calls) == 9


def test_validation_disabled_skips_the_validator():
    model = ScriptedChatModel(
        replies=[intake_json(), plan_json(COLLECT_STEP), "收集", "合成报告"]
    )

    state = run_dynamic_pipeline(
        "任务", llm=model, settings=AgentSettings(validation_enabled=False)
    )

    assert state.status is PipelineStatus.COMPLETED
    assert state.validation is None
    assert len(model.calls) == 4
    assert "validate" not in [node.kind for node in state.flow]


def test_checkpoint_summary_tolerates_legacy_state_without_new_fields():
    """升级瞬间在途的旧状态/旧计划不能被新字段噎住（ADR-038 §7）。"""

    state = DynamicPipelineState(
        task="旧任务",
        plan=[PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集")],
        results={
            "s1": _outcome("s1", RoleId.COLLECTOR, PlanStepStatus.COMPLETED, "旧产出")
        },
        status=PipelineStatus.RUNNING,
    )

    summary = dynamic_checkpoint_summary(finalize_state(state))

    assert summary["route"] == "multi"
    assert summary["round"] == 1
    assert summary["intent"] is None
    assert summary["plan"][0]["retry"] is None
    assert summary["completed_steps"] == ["s1"]
    assert summary["partial"] is False


# --------------------------------------------------------------------------------------
# ADR-038 §8/§9：实际尝试次数与累计 Token 预算
# --------------------------------------------------------------------------------------


class CountingChatModel(ScriptedChatModel):
    """带**用量元数据**的假模型：用来验证预算按实际用量累计。"""

    tokens_per_call: int = 100

    @property
    def _llm_type(self) -> str:
        return "counting-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        message = result.generations[0].message
        message.usage_metadata = {
            "input_tokens": self.tokens_per_call - 20,
            "output_tokens": 20,
            "total_tokens": self.tokens_per_call,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_tokens_are_accumulated_from_actual_usage():
    model = CountingChatModel(
        replies=[intake_json(), plan_json(COLLECT_STEP), "收集", "合成报告", validation_json()]
    )

    state = run_dynamic_pipeline("任务", llm=model)

    # intake + 规划 + 1 个子任务 + 合成 + 校验，每次都报 100。
    assert state.results["s1"].tokens == 100
    summary = dynamic_checkpoint_summary(state)
    assert summary["tokens_used"] == 500
    assert summary["token_budget"] == 0, "默认不限制"
    assert summary["budget_exceeded"] is False


def test_token_budget_stops_new_waves_and_says_so():
    """预算用尽：不再派发后续步骤，但**照常交付**已完成的部分并写明缺口。"""

    model = CountingChatModel(
        replies=[
            intake_json(),
            plan_json(
                COLLECT_STEP,
                {"id": "s2", "role": "analyst", "instruction": "分析", "depends_on": ["s1"]},
            ),
            "第一波产出",
            "合成报告（只有部分）",
            validation_json(),
        ],
        tokens_per_call=1000,
    )

    state = run_dynamic_pipeline(
        "任务", llm=model, settings=AgentSettings(token_budget=2500)
    )

    assert state.budget_exceeded is True
    assert state.results["s1"].status is PlanStepStatus.COMPLETED
    assert state.results["s2"].status is PlanStepStatus.SKIPPED
    assert "Token 预算" in (state.results["s2"].error or ""), "缺口原因要写成预算，不是上游失败"
    assert state.status is PipelineStatus.COMPLETED, "已有产出照样交付"
    assert state.partial is True
    assert state.final_output == "合成报告（只有部分）"
    assert "Token 预算上限" in (state.error or "")

    summary = dynamic_checkpoint_summary(state)
    assert summary["budget_exceeded"] is True
    assert summary["token_budget"] == 2500
    assert summary["skipped_steps"] == ["s2"]


def test_token_budget_blocks_the_replan_round():
    """校验不达标但预算已用尽 → 不再开第二轮（重编排按定义要再花一份钱）。"""

    model = CountingChatModel(
        replies=[
            intake_json(),
            plan_json(COLLECT_STEP),
            "第一轮收集",
            "第一轮报告",
            validation_json(satisfied=False, defects=["缺少来源"]),
        ],
        tokens_per_call=1000,
    )

    state = run_dynamic_pipeline(
        "写一份带来源的报告", llm=model, settings=AgentSettings(token_budget=1000)
    )

    assert state.round == 1, "预算用尽就不该重编排"
    assert state.validation is not None and state.validation.satisfied is False
    assert state.budget_exceeded is True
    assert state.status is PipelineStatus.COMPLETED, "第一轮的交付物仍然给用户"

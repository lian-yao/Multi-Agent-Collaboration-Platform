"""动态编排 Dapr 链路的单元测试（ADR-019）。

覆盖三块：

1. **活动行为**：规划活动在演练模式下不碰模型；步骤活动产出确定性结果；
2. **父工作流编排**：先规划后执行、子工作流实例 ID 稳定、依赖失败时跳过下游、
   业务终态与 Dapr 终态一致（ADR-016 F-05）；
3. **接线**：动态工作流按模式注册与调度，静态链路不受影响。

沿用 `tests/unit/test_workflow_pipeline.py` 的假上下文写法，不启动真实 Dapr。
"""

from typing import Any

import pytest

from app.orchestration.dynamic_graph import (
    PlanStepStatus,
    StepOutcome,
    fallback_plan,
)
from app.orchestration.pipeline import PipelineStatus
from app.workflows.dynamic import (
    DYNAMIC_SUBTASK_WORKFLOW_NAME,
    DYNAMIC_WORKFLOW_NAME,
    agent_dynamic_workflow,
    dynamic_plan_activity,
    dynamic_step_activity,
    dynamic_subtask_workflow,
)
from app.workflows.pipeline import SUBTASK_WORKFLOW_NAME, WORKFLOW_NAME, WorkflowTask
from app.workflows.service import WorkflowService, resolve_workflow_name


class _ScriptedTask:
    pass


class _ScriptedWorkflowContext:
    """DaprWorkflowContext 的最小替身，用来驱动编排生成器。"""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.calls: list[tuple[str, Any, Any]] = []
        self.statuses: list[str] = []

    def call_activity(self, activity, *, input=None, **_kwargs) -> _ScriptedTask:
        self.calls.append(("activity", _name(activity), input))
        return _ScriptedTask()

    def call_child_workflow(self, workflow, *, input=None, instance_id=None, **_kwargs):
        self.calls.append(("child", _name(workflow), input))
        self.calls[-1] = (*self.calls[-1], instance_id)
        assert instance_id, "动态子工作流必须给出稳定实例 ID"
        return _ScriptedTask()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


def _name(value: Any) -> str:
    if isinstance(value, str):
        return value
    alternate = getattr(value, "__dict__", {}).get("_dapr_alternate_name")
    return alternate or getattr(value, "__name__", str(value))


class _ActivityContext:
    def __init__(self, workflow_id: str = "wf-1") -> None:
        self.workflow_id = workflow_id


def _task(**overrides) -> dict[str, Any]:
    base = {
        "workflow_id": "wf-1",
        "session_id": "session-1",
        "agent_run_id": "run-1",
        "message_id": "msg-1",
        "task": "演示任务",
        "use_fake_model": True,
    }
    base.update(overrides)
    return base


def _outcome(step_id: str, role: str, status: PlanStepStatus, content: str = "") -> dict:
    return StepOutcome(
        step_id=step_id,
        role=role,  # type: ignore[arg-type]
        instruction="i",
        status=status,
        content=content,
    ).model_dump(mode="json")


def _plan_payload(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"workflow_id": "wf-1", "plan": {"steps": steps, "source": "llm", "rationale": "测试"}}


THREE_STEPS = [
    {"id": "s1", "role": "collector", "instruction": "收集", "depends_on": []},
    {"id": "s2", "role": "analyst", "instruction": "分析", "depends_on": ["s1"]},
    {"id": "s3", "role": "reporter", "instruction": "报告", "depends_on": ["s2"]},
]


# --------------------------------------------------------------------------------------
# 活动
# --------------------------------------------------------------------------------------


def test_workflow_functions_are_generators():
    import inspect

    assert inspect.isgeneratorfunction(agent_dynamic_workflow)
    assert inspect.isgeneratorfunction(dynamic_subtask_workflow)


def test_drill_mode_plan_activity_returns_fallback_without_model():
    result = dynamic_plan_activity(_ActivityContext(), {"task": _task()})

    plan = result["plan"]
    assert result["workflow_id"] == "wf-1"
    assert plan["source"] == "fallback"
    assert [step["role"] for step in plan["steps"]] == ["collector", "analyst", "reporter"]
    assert "演练模式" in plan["rationale"]


def test_drill_mode_step_activity_is_deterministic():
    payload = {
        "task": _task(),
        "step": THREE_STEPS[1],
        "results": {"s1": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点")},
    }

    first = dynamic_step_activity(_ActivityContext(), payload)
    second = dynamic_step_activity(_ActivityContext(), payload)

    assert first == second
    assert first["outcome"]["status"] == "completed"
    assert "s2" in first["outcome"]["content"]
    assert "s1" in first["outcome"]["content"]


def test_step_activity_absent_results_are_tolerated():
    payload = {"task": _task(), "step": THREE_STEPS[0]}

    result = dynamic_step_activity(_ActivityContext(), payload)

    assert result["outcome"]["step_id"] == "s1"


def test_plan_activity_feeds_session_history_into_the_planner(monkeypatch):
    """规划活动必须带上会话历史（ADR-019 的读点在动态链路上补齐）。

    2026-09-23 实测反馈「同一个会话不记得我之前说过什么」：动态图规划时只给了当前
    任务，用户只回「重试」时连要重试什么都判断不了。这条钉住活动层的接线。
    """

    from app.memory import MessageRole, SessionMessage
    from app.memory.runtime import conversation_memory
    import app.workflows.dynamic as dynamic_module

    memory = conversation_memory()
    memory.append_message(
        "session-1",
        SessionMessage(
            session_id="session-1",
            role=MessageRole.USER,
            content="生成冒泡代码python版本",
            agent_run_id="run-0",
        ),
    )
    memory.append_message(
        "session-1",
        SessionMessage(
            session_id="session-1",
            role=MessageRole.ASSISTANT,
            content="已生成 bubble.py",
            agent_run_id="run-0",
        ),
    )
    captured: dict[str, Any] = {}

    def fake_generate_plan(task, llm, max_steps, **kwargs):
        captured["task"] = task
        captured["history"] = kwargs.get("history")
        return fallback_plan("测试替身")

    monkeypatch.setattr(dynamic_module, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(dynamic_module, "build_chat_model", lambda settings: object())

    dynamic_plan_activity(_ActivityContext(), {"task": _task(use_fake_model=False)})

    assert captured["task"] == "演示任务"
    assert [message.content for message in captured["history"]] == [
        "生成冒泡代码python版本",
        "已生成 bubble.py",
    ], "规划模型要能看到上一轮，不只是当前任务"


def test_step_activity_feeds_session_history_into_the_step(monkeypatch):
    """步骤活动同样要带历史——否则「重试」这一步拿不到上一轮的产物。"""

    from app.memory import MessageRole, SessionMessage
    from app.memory.runtime import conversation_memory
    import app.workflows.dynamic as dynamic_module

    conversation_memory().append_message(
        "session-1",
        SessionMessage(
            session_id="session-1",
            role=MessageRole.USER,
            content="上一轮：写冒泡排序",
            agent_run_id="run-0",
        ),
    )
    captured: dict[str, Any] = {}

    def fake_run_plan_step(
        step, task, results, llm, caller, workflow_id, attachments, history=(), preferences=""
    ):
        captured["task"] = task
        captured["history"] = history
        captured["preferences"] = preferences
        return StepOutcome(
            step_id=step.id,
            role=step.role,
            instruction=step.instruction,
            status=PlanStepStatus.COMPLETED,
            content="ok",
        )

    monkeypatch.setattr(dynamic_module, "run_plan_step", fake_run_plan_step)
    monkeypatch.setattr(dynamic_module, "build_chat_model", lambda settings: object())
    monkeypatch.setattr(dynamic_module, "default_tool_registry", lambda: None)

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
    )

    assert captured["task"] == "演示任务"
    assert [message.content for message in captured["history"]] == ["上一轮：写冒泡排序"]


# --------------------------------------------------------------------------------------
# 父工作流
# --------------------------------------------------------------------------------------


def _rewrite_output() -> dict[str, Any]:
    """改写活动的替身输出（ADR-037）：测试不调模型，直接给"已按上下文补全"的结果。"""

    return {
        "workflow_id": "wf-1",
        "task": "演示任务（已按上下文补全）",
        "source": "model",
        "original_chars": 4,
        "task_chars": 14,
    }


def test_dynamic_workflow_plans_then_runs_each_step():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())

    gen.send(None)
    # 第一个活动必须是改写（ADR-037）：规划拿到「重试」这类输入时，没有改写连要重试什么都判断不了。
    assert ctx.calls[0][0:2] == ("activity", "rewrite_activity")
    gen.send(_rewrite_output())
    assert ctx.calls[1][0:2] == ("activity", "dynamic_plan_activity")

    gen.send(_plan_payload(THREE_STEPS))
    for index, step in enumerate(THREE_STEPS):
        call = ctx.calls[index + 2]
        assert call[0:2] == ("child", "dynamic_subtask_workflow")
        assert call[2]["step"]["id"] == step["id"]
        assert call[3] == f"wf-1:dyn:{step['id']}"
        gen.send(
            {
                "workflow_id": "wf-1",
                "outcome": _outcome(step["id"], step["role"], PlanStepStatus.COMPLETED, f"{step['id']} 产出"),
            }
        )

    finalize = ctx.calls[-1]
    assert finalize[0:2] == ("activity", "finalize_activity")
    assert finalize[2]["status"] == "completed"
    assert finalize[2]["report"] == "s3 产出"
    assert finalize[2]["checkpoint"]["mode"] == "dynamic"
    assert finalize[2]["checkpoint"]["completed_steps"] == ["s1", "s2", "s3"]
    # 改写结果随 checkpoint 落库：界面/审计要能看见改成了什么
    assert finalize[2]["checkpoint"]["rewritten_task"] == "演示任务（已按上下文补全）"
    assert finalize[2]["checkpoint"]["rewrite_source"] == "model"

    with pytest.raises(StopIteration):
        gen.send(None)


def test_dynamic_workflow_passes_upstream_results_to_later_steps():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    gen.send(None)
    gen.send(_rewrite_output())
    gen.send(_plan_payload(THREE_STEPS))

    gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点")})
    second_child_input = ctx.calls[3][2]

    assert second_child_input["results"]["s1"]["content"] == "要点"


def test_failed_step_skips_downstream_and_fails_the_workflow():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    gen.send(None)
    gen.send(_rewrite_output())
    gen.send(_plan_payload(THREE_STEPS))

    # s1 失败：s2、s3 连坐跳过，不应再产生任何子工作流调用。
    gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.FAILED)})

    child_calls = [call for call in ctx.calls if call[0] == "child"]
    assert len(child_calls) == 1

    finalize = ctx.calls[-1]
    assert finalize[2]["status"] == "failed"
    assert "失败步骤：s1" in (finalize[2]["error"] or "")
    assert finalize[2]["checkpoint"]["plan"][1]["status"] == "skipped"
    assert finalize[2]["checkpoint"]["plan"][2]["status"] == "skipped"

    # 业务失败必须反映到 Dapr 实例终态，避免「工作流 completed 但消息 failed」的错位。
    with pytest.raises(RuntimeError, match="失败步骤"):
        gen.send(None)


def test_plan_activity_failure_marks_workflow_failed():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    gen.send(None)
    # 先过改写那一步（ADR-037）：否则抛进的是改写那个 yield，这条用例就不再是在验证
    # "规划失败"，而是在验证"改写失败"了。
    gen.send(_rewrite_output())

    # 抛进生成器会先走 except：调度失败终态活动，然后才把异常继续往上抛。
    gen.throw(RuntimeError("planner exploded"))

    assert ctx.calls[-1][0:2] == ("activity", "finalize_activity")
    assert ctx.calls[-1][2]["status"] == "failed"
    assert ctx.calls[-1][2]["error"] == "planner exploded"

    with pytest.raises(RuntimeError, match="planner exploded"):
        gen.send(None)


def test_subtask_workflow_wraps_step_in_one_activity():
    ctx = _ScriptedWorkflowContext("wf-1:dyn:s1")
    activity_input = {"task": _task(), "step": THREE_STEPS[0], "results": {}}
    gen = dynamic_subtask_workflow(ctx, activity_input)

    gen.send(None)
    assert ctx.calls == [("activity", "dynamic_step_activity", activity_input)]
    assert ctx.statuses == ["dyn:s1"]

    # 子工作流只有一次 yield，喂回结果即结束并把它作为返回值带出。
    with pytest.raises(StopIteration) as stopped:
        gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "x")})

    assert stopped.value.value["outcome"]["content"] == "x"


def test_fallback_plan_steps_match_workflow_expectations():
    plan = fallback_plan()

    assert [(step.id, step.role.value, step.depends_on) for step in plan.steps] == [
        ("s1", "collector", []),
        ("s2", "analyst", ["s1"]),
        ("s3", "reporter", ["s2"]),
    ]


# --------------------------------------------------------------------------------------
# 接线
# --------------------------------------------------------------------------------------


class _RecordingRuntime:
    def __init__(self) -> None:
        self.workflows: list[tuple[str, str]] = []
        self.activities: list[str] = []
        self.started = False

    def register_workflow(self, workflow, *, name=None) -> None:
        self.workflows.append((name or _name(workflow), _name(workflow)))

    def register_activity(self, activity) -> None:
        self.activities.append(_name(activity))

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.started = False


class _RecordingClient:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, dict[str, Any], str]] = []

    def schedule_new_workflow(self, workflow, *, input=None, instance_id=None) -> str:
        self.scheduled.append((workflow, input, instance_id))
        return instance_id or ""

    def close(self) -> None:
        pass


def test_service_registers_dynamic_workflows_alongside_static():
    runtime = _RecordingRuntime()
    service = WorkflowService(runtime=runtime, client=_RecordingClient())

    service.register()

    names = [name for name, _ in runtime.workflows]
    assert names == [WORKFLOW_NAME, SUBTASK_WORKFLOW_NAME, DYNAMIC_WORKFLOW_NAME, DYNAMIC_SUBTASK_WORKFLOW_NAME]
    assert "dynamic_plan_activity" in runtime.activities
    assert "dynamic_step_activity" in runtime.activities
    # 静态链路的活动一个都没少。
    assert {"run_stage_activity", "collect_activity", "finalize_activity"} <= set(runtime.activities)


def test_schedule_picks_workflow_name_by_mode(monkeypatch):
    # 调度会顺带把 workflow_runs 状态置 running；这里只关心选了哪个工作流。
    monkeypatch.setattr("app.workflows.service.update_workflow_run", lambda *a, **k: None)
    client = _RecordingClient()
    service = WorkflowService(runtime=_RecordingRuntime(), client=client)

    service.schedule(WorkflowTask(workflow_id="w1", task="任务"))
    service.schedule(WorkflowTask(workflow_id="w2", task="任务", orchestration_mode="dynamic"))

    assert [item[0] for item in client.scheduled] == [WORKFLOW_NAME, DYNAMIC_WORKFLOW_NAME]
    assert [item[2] for item in client.scheduled] == ["w1", "w2"]
    assert client.scheduled[1][1]["orchestration_mode"] == "dynamic"


def test_resolve_workflow_name_defaults_to_static():
    assert resolve_workflow_name() == WORKFLOW_NAME

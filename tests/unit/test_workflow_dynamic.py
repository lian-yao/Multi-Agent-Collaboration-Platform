"""动态编排 Dapr 链路的单元测试（ADR-019）。

覆盖三块：

1. **活动行为**：规划活动在演练模式下不碰模型；步骤活动产出确定性结果；
2. **父工作流编排**：先规划后执行、子工作流实例 ID 稳定、依赖失败时跳过下游、
   业务终态与 Dapr 终态一致（ADR-016 F-05）；
3. **接线**：动态工作流按模式注册与调度，静态链路不受影响。

沿用 `tests/unit/test_workflow_pipeline.py` 的假上下文写法，不启动真实 Dapr。
"""

import re
from pathlib import Path
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
    dynamic_progress_activity,
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


def test_drill_mode_plan_activity_returns_fallback_without_model(monkeypatch):
    monkeypatch.setattr("app.workflows.dynamic.update_workflow_run", lambda *a, **k: None)
    result = dynamic_plan_activity(_ActivityContext(), {"task": _task()})

    plan = result["plan"]
    assert result["workflow_id"] == "wf-1"
    assert plan["source"] == "fallback"
    assert [step["role"] for step in plan["steps"]] == ["collector", "analyst", "reporter"]
    assert "演练模式" in plan["rationale"]


def test_drill_mode_plan_activity_persists_plan_checkpoint(monkeypatch):
    """规划产出后立即把计划写回 checkpoint（§5.22「计划即落盘」）。"""

    written: dict[str, Any] = {}

    def fake_update(workflow_id: str, **kwargs: Any) -> None:
        written["workflow_id"] = workflow_id
        written.update(kwargs)

    monkeypatch.setattr("app.workflows.dynamic.update_workflow_run", fake_update)

    dynamic_plan_activity(_ActivityContext(), {"task": _task()})

    checkpoint = written["checkpoint"]
    assert written["workflow_id"] == "wf-1"
    assert checkpoint["mode"] == "dynamic"
    assert checkpoint["status"] == "running"
    assert checkpoint["plan_rationale"] != ""
    assert [step["role"] for step in checkpoint["plan"]] == ["collector", "analyst", "reporter"]
    assert all(step["status"] == "pending" for step in checkpoint["plan"])
    # 首步同时占住 `current_step`（§5.22）：**行与 checkpoint 两处都要写**——前端只认
    # 指针相等的那一步（默认摊开 + 承接实时工具调用），少写一处这条链路就断。
    assert written["current_step"] == checkpoint["current_step"] == "s1"


def test_drill_mode_step_activity_is_deterministic(monkeypatch):
    monkeypatch.setattr("app.workflows.dynamic.save_step_result", lambda *a, **k: None)
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


def test_step_activity_persists_step_outcome_to_state_store(monkeypatch):
    """每步完成后把 StepOutcome 落盘到状态存储 key `dyn:{step_id}`（§5.22）。"""

    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.workflows.dynamic.save_step_result",
        lambda workflow_id, step, result: saved.update(
            {"workflow_id": workflow_id, "step": step, "result": result}
        ),
    )

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(), "step": THREE_STEPS[0], "results": {}},
    )

    assert saved["workflow_id"] == "wf-1"
    assert saved["step"] == "dyn:s1"
    assert saved["result"]["step_id"] == "s1"
    assert saved["result"]["content"] != ""


def test_step_activity_absent_results_are_tolerated(monkeypatch):
    monkeypatch.setattr("app.workflows.dynamic.save_step_result", lambda *a, **k: None)
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
        # ADR-036：候选角色集由活动层传入，缺它就说明合并时把并集吃掉了
        assert "candidates" in kwargs
        return fallback_plan("测试替身")

    monkeypatch.setattr(dynamic_module, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(dynamic_module, "build_chat_model", lambda settings: object())
    # 规划活动返回前会把计划落盘（§5.22「计划即落盘」）；这条用例只看进参，写回不必真落库。
    monkeypatch.setattr(dynamic_module, "update_workflow_run", lambda *a, **k: None)

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
        step,
        task,
        results,
        llm,
        caller,
        workflow_id,
        attachments,
        history=(),
        preferences="",
        catalog=None,
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
    # 每步完成即落盘（§5.22）在途侧新增：这两条只看进参，落盘替掉。
    monkeypatch.setattr(dynamic_module, "save_step_result", lambda *a, **k: None)

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
    )

    assert captured["task"] == "演示任务"
    assert [message.content for message in captured["history"]] == ["上一轮：写冒泡排序"]


def test_step_activity_mounts_session_scoped_tools(monkeypatch):
    """**动态链路也必须挂会话级工具**（工作区文件工具 ADR-033 + 会话文件工具 ADR-025）。

    这条是 2026-09-23 实测事故的回归：静态链路的阶段活动一直调用
    `session_scoped_registry`，动态链路漏了——使用者在「自动编排」（前端默认）下绑定好工作区
    再问「这个文件夹下面有哪些文件」，Agent 的工具列表里**根本没有文件类工具**，连带
    `code_execution` 也拿到不带工作区挂载的实例（沙箱里 `/workspace` 不存在、cwd 退到 /tmp）。
    """

    import app.workflows.dynamic as dynamic_module

    seen: dict[str, Any] = {}

    def fake_session_scoped(registry, session_id):
        seen["session_id"] = session_id
        return registry

    monkeypatch.setattr(dynamic_module, "session_scoped_registry", fake_session_scoped)

    class _BaseRegistry:
        """最小注册表：`ToolCaller` 构造时会问目录，给一条空目录即可。"""

        def list_tools(self):
            return ()

    monkeypatch.setattr(dynamic_module, "default_tool_registry", _BaseRegistry)
    monkeypatch.setattr(dynamic_module, "build_chat_model", lambda settings: object())
    # 每步完成即落盘（§5.22）在途侧新增：这两条只看进参，落盘替掉。
    monkeypatch.setattr(dynamic_module, "save_step_result", lambda *a, **k: None)
    monkeypatch.setattr(
        dynamic_module,
        "run_plan_step",
        lambda step, task, results, llm, caller, workflow_id, attachments, history=(), preferences="", catalog=None: (
            StepOutcome(
                step_id=step.id,
                role=step.role,
                instruction=step.instruction,
                status=PlanStepStatus.COMPLETED,
                content="ok",
            )
        ),
    )

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
    )

    assert seen["session_id"] == "session-1", "会话级工具必须按本会话挂，否则工作区工具不会出现"


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
    # 每个步骤：先 child 子工作流执行，再 progress 活动回写进度。
    for index, step in enumerate(THREE_STEPS):
        child_calls = [call for call in ctx.calls if call[0] == "child"]
        assert child_calls[index][2]["step"]["id"] == step["id"]
        assert child_calls[index][3] == f"wf-1:dyn:{step['id']}"
        gen.send(
            {
                "workflow_id": "wf-1",
                "outcome": _outcome(step["id"], step["role"], PlanStepStatus.COMPLETED, f"{step['id']} 产出"),
            }
        )
        # 喂回 progress 活动的返回值，推进到下一步的 child 调用。
        gen.send({"workflow_id": "wf-1"})
    # 每步执行完都紧跟一次进度回写（三步 → 三次 progress 活动）。
    progress_calls = [call for call in ctx.calls if call[0:2] == ("activity", "dynamic_progress_activity")]
    assert len(progress_calls) == 3

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


def test_progress_activity_writes_running_checkpoint_with_plan(monkeypatch):
    """进度活动回写 running 状态的完整 checkpoint（含 plan + rationale + 已完成步骤）。"""

    written: dict[str, Any] = {}

    def fake_update(workflow_id: str, **kwargs: Any) -> None:
        written["workflow_id"] = workflow_id
        written.update(kwargs)

    monkeypatch.setattr("app.workflows.dynamic.update_workflow_run", fake_update)

    result = dynamic_progress_activity(
        _ActivityContext(),
        {
            "workflow_id": "wf-1",
            "plan": _plan_payload(THREE_STEPS)["plan"],
            "results": {"s1": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点")},
        },
    )

    assert result["workflow_id"] == "wf-1"
    checkpoint = written["checkpoint"]
    assert checkpoint["status"] == "running"
    assert checkpoint["completed_steps"] == ["s1"]
    assert checkpoint["plan_rationale"] == "测试"
    by_id = {step["id"]: step for step in checkpoint["plan"]}
    assert by_id["s1"]["status"] == "completed"
    assert by_id["s2"]["status"] == "pending"
    assert by_id["s2"]["role"] == "analyst"
    # 指针推到「下一个还没出结果的步骤」（§5.22）：口径与静态链路一致——在上一步落盘时
    # 就把指针写成下一步，而不是等下一步开跑。运行中的那一步靠它才会默认摊开。
    assert written["current_step"] == checkpoint["current_step"] == "s2"


def test_progress_activity_clears_pointer_when_every_step_has_a_result(monkeypatch):
    """全部步骤都有结果后指针归零（§5.22：终态 `checkpoint.current_step` 为 `null`）。"""

    written: dict[str, Any] = {}

    def fake_update(workflow_id: str, **kwargs: Any) -> None:
        written["workflow_id"] = workflow_id
        written.update(kwargs)

    monkeypatch.setattr("app.workflows.dynamic.update_workflow_run", fake_update)

    dynamic_progress_activity(
        _ActivityContext(),
        {
            "workflow_id": "wf-1",
            "plan": _plan_payload(THREE_STEPS)["plan"],
            "results": {
                "s1": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "a"),
                "s2": _outcome("s2", "analyst", PlanStepStatus.COMPLETED, "b"),
                "s3": _outcome("s3", "reporter", PlanStepStatus.COMPLETED, "c"),
            },
        },
    )

    assert written["current_step"] is None
    assert written["checkpoint"]["current_step"] is None
    assert written["checkpoint"]["completed_steps"] == ["s1", "s2", "s3"]


def test_dynamic_workflow_passes_upstream_results_to_later_steps():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    gen.send(None)
    gen.send(_rewrite_output())
    gen.send(_plan_payload(THREE_STEPS))

    gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点")})
    # 喂回 progress 活动的返回值，推进到 s2 的 child 调用。
    gen.send({"workflow_id": "wf-1"})
    second_child_input = [call for call in ctx.calls if call[0] == "child"][1][2]

    assert second_child_input["results"]["s1"]["content"] == "要点"


def test_failed_step_skips_downstream_and_fails_the_workflow():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    gen.send(None)
    gen.send(_rewrite_output())
    gen.send(_plan_payload(THREE_STEPS))

    # s1 失败：s2、s3 连坐跳过，不应再产生任何子工作流调用。
    gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.FAILED)})
    # 喂回 s1 的 progress 活动返回值，推进到 s2/s3 的跳过判定与终态。
    gen.send({"workflow_id": "wf-1"})

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

    assert [(step.id, step.role, step.depends_on) for step in plan.steps] == [
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


# 注册清单是**第二处**独立清单：工作流体里 `ctx.call_activity(...)` 调的名字，与
# `WorkflowService.register()` 里注册的名字之间没有任何交叉校验。漏一处**导入期与启动期都不
# 报错**，只在真 Dapr 运行时跑到那一步时炸
# `Activity task #N failed: Activity function named 'x' was not registered!`。
# 上面那条 `in` 断言看不见这种情况——它检查的是「我认识的这几个在不在」，不是「调用到的都注册
# 了没」。所以这里按**源码反推应注册集合**再比对：在没有真运行时的前提下，这是唯一能拦住漏注册
# 的办法。（2026-09-22 线上据此挂掉：`dynamic_progress_activity` 加进了工作流体却没注册，
# 所有 dynamic 任务都在第一步跑完后失败。）
_WORKFLOW_SOURCE_DIR = Path(__file__).resolve().parents[2] / "app" / "workflows"
_CALL_ACTIVITY = re.compile(r"call_activity\(\s*([A-Za-z_]\w*)")
_CALL_CHILD_WORKFLOW = re.compile(r"call_child_workflow\(\s*([A-Za-z_]\w*)")


def _called_names(pattern: "re.Pattern[str]") -> set[str]:
    """扫 `app/workflows/*.py` 里被调用的活动 / 子工作流名（忽略行尾注释）。"""

    names: set[str] = set()
    for path in sorted(_WORKFLOW_SOURCE_DIR.glob("*.py")):
        source = "\n".join(
            line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
        )
        names |= set(pattern.findall(source))
    return names


def test_every_activity_called_by_a_workflow_body_is_registered():
    runtime = _RecordingRuntime()
    WorkflowService(runtime=runtime, client=_RecordingClient()).register()

    missing = sorted(_called_names(_CALL_ACTIVITY) - set(runtime.activities))
    assert not missing, (
        f"工作流体调用了但没注册的活动：{missing} —— 真 Dapr 运行时会报 "
        "'Activity function named ... was not registered!'，"
        "补 `service.py` 的 import 与 `register_activity`。"
    )


def test_every_child_workflow_called_by_a_workflow_body_is_registered():
    runtime = _RecordingRuntime()
    WorkflowService(runtime=runtime, client=_RecordingClient()).register()

    # `_RecordingRuntime` 记的第二个元素是函数自己的名字：这几个工作流都没用装饰器改名，
    # 传给 Dapr 的名字是在 `register_workflow(..., name=...)` 里显式给的常量。
    registered = {ident for _, ident in runtime.workflows}
    missing = sorted(_called_names(_CALL_CHILD_WORKFLOW) - registered)
    assert not missing, f"工作流体调用了但没注册的子工作流：{missing}"
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

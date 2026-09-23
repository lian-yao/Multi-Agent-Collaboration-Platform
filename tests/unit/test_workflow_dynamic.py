"""动态编排 Dapr 链路的单元测试（ADR-019 / ADR-038）。

覆盖四块：

1. **活动行为**：intake / 规划 / 步骤 / 合成 / 校验 / checkpoint 各自的口径（含降级）；
2. **父工作流编排**：intake 先行、波内并行、子工作流实例 ID 带轮次、失败只连坐下游、
   校验不达标重编排一轮、业务终态与 Dapr 终态一致（ADR-016 F-05）；
3. **子工作流**：一个持久化边界内执行一个步骤，失败收敛成业务失败而不是抛给父工作流；
4. **接线**：动态工作流与活动按模式注册与调度，静态链路不受影响。

沿用 `tests/unit/test_workflow_pipeline.py` 的假上下文写法，不启动真实 Dapr；
`when_all` 由假上下文记成「批」，用例据此断言同波的子任务确实是**一起**派发的。
"""

from datetime import timedelta
from typing import Any

import pytest

from app.orchestration.dynamic_graph import (
    PlanStepStatus,
    StepOutcome,
    fallback_plan,
)
from app.config import AgentSettings
from app.workflows.dynamic import (
    DYNAMIC_SUBTASK_WORKFLOW_NAME,
    DYNAMIC_WORKFLOW_NAME,
    StepAttemptFailed,
    agent_dynamic_workflow,
    dynamic_checkpoint_activity,
    dynamic_plan_activity,
    dynamic_step_activity,
    dynamic_subtask_workflow,
    dynamic_synthesize_activity,
    dynamic_validate_activity,
    fake_step_outcome,
    intake_activity,
    retry_backoff,
)
from app.workflows.pipeline import SUBTASK_WORKFLOW_NAME, WORKFLOW_NAME, WorkflowTask
from app.workflows.service import WorkflowService, resolve_workflow_name


class _ScriptedTask:
    """被 yield 出去的任务：活动与子工作流都记下名字与输入，便于断言。"""

    def __init__(self, name: str, payload: Any = None, instance_id: str | None = None) -> None:
        self.name = name
        self.payload = payload
        self.instance_id = instance_id


class _WhenAll:
    """`when_all` 的替身：带上这一批子任务，用例据此造结果列表。"""

    def __init__(self, tasks: list[_ScriptedTask]) -> None:
        self.tasks = tasks


class _ScriptedWorkflowContext:
    """DaprWorkflowContext 的最小替身，用来驱动编排生成器。"""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.calls: list[tuple] = []
        self.batches: list[list[_ScriptedTask]] = []
        self.statuses: list[str] = []
        # `when_all` 是模块级函数，拿不到 self：这里记下"当前驱动的上下文"，
        # 由下面的 `when_all` 替身把批次挂到它身上（用例都是单上下文驱动的）。
        _CURRENT_CONTEXT.clear()
        _CURRENT_CONTEXT.append(self)

    def call_activity(
        self, activity, *, input=None, retry_policy=None, **_kwargs
    ) -> _ScriptedTask:
        task = _ScriptedTask(_name(activity), input)
        self.calls.append(("activity", task.name, input, retry_policy))
        return task

    def call_child_workflow(
        self, workflow, *, input=None, instance_id=None, retry_policy=None, **_kwargs
    ) -> _ScriptedTask:
        assert instance_id, "动态子工作流必须给出稳定实例 ID"
        task = _ScriptedTask(_name(workflow), input, instance_id)
        self.calls.append(("child", task.name, input, instance_id))
        return task

    def create_timer(self, delta: Any) -> _ScriptedTask:
        task = _ScriptedTask("timer", delta)
        self.calls.append(("timer", "create_timer", delta))
        return task

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


_CURRENT_CONTEXT: list[_ScriptedWorkflowContext] = []


@pytest.fixture(autouse=True)
def inline_when_all(monkeypatch):
    """把 `dapr.ext.workflow.when_all` 换成批记录器。

    **它是模块级函数，不是上下文方法**——真实运行时给工作流的上下文
    （`_RuntimeOrchestrationContext`）没有 `when_all` 属性。替身若把它假装成方法，
    单测会全绿而真机上直接崩：2026-09-24 在真实 sidecar 上就是这么暴露的
    （`AttributeError: '_RuntimeOrchestrationContext' object has no attribute 'when_all'`）。
    """

    import app.workflows.dynamic as dynamic_module

    def fake_when_all(tasks: list[_ScriptedTask]) -> _WhenAll:
        if _CURRENT_CONTEXT:
            _CURRENT_CONTEXT[-1].batches.append(list(tasks))
        return _WhenAll(list(tasks))

    monkeypatch.setattr(dynamic_module, "when_all", fake_when_all)
    return fake_when_all


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


def _outcome(
    step_id: str,
    role: str,
    status: PlanStepStatus,
    content: str = "",
    *,
    tokens: int = 0,
    attempts: int = 1,
) -> dict:
    return StepOutcome(
        step_id=step_id,
        role=role,  # type: ignore[arg-type]
        instruction="i",
        status=status,
        content=content,
        tokens=tokens,
        attempts=attempts,
    ).model_dump(mode="json")


def _plan_payload(
    steps: list[dict[str, Any]], *, source: str = "llm", tokens: int = 0
) -> dict[str, Any]:
    return {
        "workflow_id": "wf-1",
        "round": 1,
        "plan": {"steps": steps, "source": source, "rationale": "测试", "tokens": tokens},
    }


THREE_STEPS = [
    {"id": "s1", "role": "collector", "instruction": "收集", "depends_on": []},
    {"id": "s2", "role": "analyst", "instruction": "分析", "depends_on": ["s1"]},
    {"id": "s3", "role": "reporter", "instruction": "报告", "depends_on": ["s2"]},
]

PARALLEL_STEPS = [
    {"id": "s1", "role": "collector", "instruction": "收集 A", "depends_on": []},
    {"id": "s2", "role": "collector", "instruction": "收集 B", "depends_on": []},
    {"id": "s3", "role": "analyst", "instruction": "对比", "depends_on": ["s1", "s2"]},
]

INTAKE_MULTI = {
    "workflow_id": "wf-1",
    "task": "演示任务（已按上下文补全）",
    "source": "model",
    "original_chars": 4,
    "task_chars": 14,
    "intent": {
        "intent_type": "report",
        "user_goal": "出一份对比报告",
        "constraints": ["中文"],
        "need_multi_subtask": True,
    },
    "intent_source": "model",
}

INTAKE_SINGLE = {
    **INTAKE_MULTI,
    "task": "解释一下什么是幂等",
    "intent": {
        "intent_type": "question",
        "user_goal": "解释幂等",
        "constraints": [],
        "need_multi_subtask": False,
    },
}

VALIDATION_OK = {
    "validation": {"satisfied": True, "defects": [], "missing": [], "source": "model"}
}


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


def test_drill_mode_intake_activity_keeps_multi_agent_route_without_model():
    result = intake_activity(_ActivityContext(), {"task": _task()})

    assert result["task"] == "演示任务"
    assert result["source"] == "original"
    assert result["intent"] is None
    assert result["intent_source"] == "fallback"


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


def test_step_activity_raises_so_retry_policy_can_work(monkeypatch):
    """失败必须抛错：重试策略挂在活动调用上，把失败当返回值就等于没有重试。"""

    import app.workflows.dynamic as dynamic_module

    monkeypatch.setattr(
        dynamic_module,
        "run_plan_step",
        lambda *a, **k: StepOutcome(
            step_id="s1",
            role="collector",  # type: ignore[arg-type]
            instruction="收集",
            status=PlanStepStatus.FAILED,
            error="RuntimeError: boom",
        ),
    )
    monkeypatch.setattr(dynamic_module, "default_tool_registry", lambda: None)

    with pytest.raises(StepAttemptFailed, match="boom"):
        dynamic_step_activity(
            _ActivityContext(),
            {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
        )


def test_plan_activity_feeds_intent_and_defects_into_the_planner(monkeypatch):
    """重编排必须把上一轮的缺陷带给规划模型，否则只是把上一轮原样再跑一遍。"""

    import app.workflows.dynamic as dynamic_module

    captured: dict[str, Any] = {}

    def fake_generate_plan(task, llm, max_steps, **kwargs):
        captured["task"] = task
        captured["intent"] = kwargs.get("intent")
        captured["defects"] = kwargs.get("defects")
        return fallback_plan("测试替身")

    monkeypatch.setattr(dynamic_module, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(dynamic_module, "build_chat_model", lambda settings: object())

    dynamic_plan_activity(
        _ActivityContext(),
        {
            "task": _task(use_fake_model=False),
            "intent": INTAKE_MULTI["intent"],
            "defects": ["缺少来源标注"],
            "round": 2,
        },
    )

    assert captured["task"] == "演示任务"
    assert captured["intent"].user_goal == "出一份对比报告"
    assert captured["defects"] == ["缺少来源标注"]


def test_plan_activity_feeds_session_history_into_the_planner(monkeypatch):
    """规划活动必须带上会话历史（ADR-019 的读点在动态链路上补齐）。"""

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


def test_intake_activity_feeds_session_history(monkeypatch):
    """intake 也要看得到上一轮：只回「重试」时，没历史连要重试什么都判断不了。"""

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

    def fake_intake(task, **kwargs):
        captured["task"] = task
        captured["history"] = kwargs.get("history")
        return {
            "task": task,
            "source": "original",
            "intent": None,
            "intent_source": "fallback",
        }

    monkeypatch.setattr(dynamic_module, "intake_task", fake_intake)

    intake_activity(_ActivityContext(), {"task": _task(use_fake_model=False)})

    assert captured["task"] == "演示任务"
    assert [message.content for message in captured["history"]] == ["上一轮：写冒泡排序"]


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
        step, task, results, llm, caller, workflow_id, attachments, *args, **kwargs
    ):
        captured["task"] = task
        captured["history"] = args[0] if args else kwargs.get("history")
        captured["preferences"] = args[1] if len(args) > 1 else kwargs.get("preferences")
        captured["timeout"] = kwargs.get("timeout_seconds")
        return StepOutcome(
            step_id=step.id,
            role=step.role,
            instruction=step.instruction,
            status=PlanStepStatus.COMPLETED,
            content="ok",
        )

    monkeypatch.setattr(dynamic_module, "run_plan_step", fake_run_plan_step)
    monkeypatch.setattr(dynamic_module, "default_tool_registry", lambda: None)

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
    )

    assert captured["task"] == "演示任务"
    assert [message.content for message in captured["history"]] == ["上一轮：写冒泡排序"]
    assert captured["timeout"] is not None, "该步的超时要落到模型客户端"


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
    monkeypatch.setattr(
        dynamic_module,
        "run_plan_step",
        lambda *args, **kwargs: StepOutcome(
            step_id="s1",
            role="collector",  # type: ignore[arg-type]
            instruction="收集",
            status=PlanStepStatus.COMPLETED,
            content="ok",
        ),
    )

    dynamic_step_activity(
        _ActivityContext(),
        {"task": _task(use_fake_model=False), "step": THREE_STEPS[0], "results": {}},
    )

    assert seen["session_id"] == "session-1", "会话级工具必须按本会话挂，否则工作区工具不会出现"


def test_synthesize_activity_tells_the_synthesizer_which_subtasks_failed(monkeypatch):
    """合成器必须拿到失败清单：需求 2「让用户知道部分子任务失败」就落在这里。"""

    import app.workflows.dynamic as dynamic_module
    from app.orchestration.synthesis import SynthesisOutcome

    captured: dict[str, Any] = {}

    def fake_run_synthesis(task, results, **kwargs):
        captured["results"] = results
        captured["failed"] = kwargs.get("failed")
        captured["skipped"] = kwargs.get("skipped")
        return SynthesisOutcome(status="completed", content="合成报告")

    monkeypatch.setattr(dynamic_module, "run_synthesis", fake_run_synthesis)
    monkeypatch.setattr(dynamic_module, "default_tool_registry", lambda: None)

    payload = {
        "task": _task(use_fake_model=False),
        "plan": {"steps": THREE_STEPS, "source": "llm", "rationale": "r"},
        "results": {
            "s1": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点"),
            "s2": _outcome("s2", "analyst", PlanStepStatus.FAILED),
            "s3": _outcome("s3", "reporter", PlanStepStatus.SKIPPED),
        },
    }

    result = dynamic_synthesize_activity(_ActivityContext(), payload)

    assert result["status"] == "completed"
    assert captured["results"] == {"s1": "要点"}
    assert [item[0] for item in captured["failed"]] == ["s2"]
    assert [item[0] for item in captured["skipped"]] == ["s3"]


def test_validate_activity_reports_the_validation(monkeypatch):
    import app.workflows.dynamic as dynamic_module
    from app.orchestration.synthesis import ValidationResult

    monkeypatch.setattr(
        dynamic_module,
        "run_validation",
        lambda *a, **k: ValidationResult(satisfied=False, defects=["缺来源"]),
    )

    result = dynamic_validate_activity(
        _ActivityContext(),
        {
            "task": _task(use_fake_model=False),
            "plan": {"steps": THREE_STEPS, "source": "llm", "rationale": "r"},
            "results": {"s1": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "要点")},
            "deliverable": "报告",
        },
    )

    assert result["validation"]["satisfied"] is False
    assert result["validation"]["defects"] == ["缺来源"]


def test_checkpoint_activity_writes_the_summary(monkeypatch):
    import app.workflows.dynamic as dynamic_module

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dynamic_module,
        "update_workflow_run",
        lambda workflow_id, **kwargs: captured.update(workflow_id=workflow_id, **kwargs),
    )

    dynamic_checkpoint_activity(
        _ActivityContext(),
        {"workflow_id": "wf-1", "checkpoint": {"mode": "dynamic", "round": 1}},
    )

    assert captured["workflow_id"] == "wf-1"
    assert captured["status"] == "running"
    assert captured["checkpoint"]["round"] == 1


def test_retry_backoff_follows_the_documented_curve():
    """退避曲线与原先挂在 `RetryPolicy` 上的参数同口径（首次 1s、系数 2、上限 10s）。"""

    assert retry_backoff(1) == timedelta(seconds=1)
    assert retry_backoff(2) == timedelta(seconds=2)
    assert retry_backoff(3) == timedelta(seconds=4)
    assert retry_backoff(10) == timedelta(seconds=10), "上限封顶，不要指数爆炸"


# --------------------------------------------------------------------------------------
# 父工作流
# --------------------------------------------------------------------------------------


def _drive(gen, respond):
    """驱动工作流生成器：把它 yield 出来的任务交给 `respond` 造结果再喂回去。"""

    sent = None
    started = False
    while True:
        try:
            yielded = gen.send(sent) if started else next(gen)
        except StopIteration as stopped:
            return stopped.value
        started = True
        sent = respond(yielded)


def _completed_from(task: _ScriptedTask) -> dict[str, Any]:
    step = task.payload["step"]
    return {
        "workflow_id": "wf-1",
        "outcome": _outcome(
            step["id"], step["role"], PlanStepStatus.COMPLETED, f"{step['id']} 产出"
        ),
    }


def _activity_names(ctx: _ScriptedWorkflowContext) -> list[str]:
    return [call[1] for call in ctx.calls if call[0] == "activity"]


def test_dynamic_workflow_runs_a_parallel_wave_then_synthesizes():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())

    script: dict[str, Any] = {
        "intake_activity": INTAKE_MULTI,
        "dynamic_plan_activity": _plan_payload(PARALLEL_STEPS),
        "dynamic_synthesize_activity": {"status": "completed", "content": "合成报告"},
        "dynamic_validate_activity": VALIDATION_OK,
        "dynamic_checkpoint_activity": {"workflow_id": "wf-1"},
        "finalize_activity": {"workflow_id": "wf-1", "status": "completed"},
    }

    def respond(yielded: Any) -> Any:
        if isinstance(yielded, _WhenAll):
            return [_completed_from(task) for task in yielded.tasks]
        return script[yielded.name]

    result = _drive(gen, respond)

    # 第一个活动必须是 intake（改写 + 意图）：规划拿到「重试」这类输入时，
    # 没有它连要重试什么都判断不了。
    assert ctx.calls[0][0:2] == ("activity", "intake_activity")
    assert ctx.calls[1][0:2] == ("activity", "dynamic_plan_activity")

    # s1 / s2 同波：**一次 when_all 里两个子工作流**（这就是并行）。
    assert [task.instance_id for task in ctx.batches[0]] == [
        "wf-1:dyn:r1:s1",
        "wf-1:dyn:r1:s2",
    ]
    # s3 依赖两者，只能在第二波。
    assert [task.instance_id for task in ctx.batches[1]] == ["wf-1:dyn:r1:s3"]

    names = _activity_names(ctx)
    assert names.index("dynamic_synthesize_activity") < names.index("dynamic_validate_activity")
    assert names.index("dynamic_validate_activity") < names.index("finalize_activity")

    finalize = [call for call in ctx.calls if call[1] == "finalize_activity"][-1]
    assert finalize[2]["status"] == "completed"
    assert finalize[2]["report"] == "合成报告"
    checkpoint = finalize[2]["checkpoint"]
    assert checkpoint["mode"] == "dynamic"
    assert checkpoint["route"] == "multi"
    assert [node["kind"] for node in checkpoint["flow"]][:2] == ["intent", "plan"]
    assert result["output"] == "合成报告"


def test_dynamic_workflow_single_agent_route_skips_planner_and_synthesis():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())

    script: dict[str, Any] = {
        "intake_activity": INTAKE_SINGLE,
        "dynamic_checkpoint_activity": {"workflow_id": "wf-1"},
        "finalize_activity": {"workflow_id": "wf-1", "status": "completed"},
    }

    def respond(yielded: Any) -> Any:
        if isinstance(yielded, _WhenAll):
            return [_completed_from(task) for task in yielded.tasks]
        return script[yielded.name]

    result = _drive(gen, respond)

    names = _activity_names(ctx)
    assert "dynamic_plan_activity" not in names, "简单任务不该再调规划器"
    assert "dynamic_synthesize_activity" not in names, "单 Agent 直答的输出就是交付物"
    assert "dynamic_validate_activity" not in names
    assert len(ctx.batches) == 1 and len(ctx.batches[0]) == 1
    assert ctx.batches[0][0].instance_id == "wf-1:dyn:r1:s1"

    finalize = [call for call in ctx.calls if call[1] == "finalize_activity"][-1]
    assert finalize[2]["report"] == "s1 产出"
    assert finalize[2]["checkpoint"]["route"] == "single"
    assert result["output"] == "s1 产出"


def test_failed_step_marks_partial_and_skips_downstream():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())

    script: dict[str, Any] = {
        "intake_activity": INTAKE_MULTI,
        "dynamic_plan_activity": _plan_payload(THREE_STEPS),
        "dynamic_synthesize_activity": {"status": "completed", "content": "部分报告"},
        "dynamic_validate_activity": VALIDATION_OK,
        "dynamic_checkpoint_activity": {"workflow_id": "wf-1"},
        "finalize_activity": {"workflow_id": "wf-1", "status": "completed"},
    }

    def respond(yielded: Any) -> Any:
        if isinstance(yielded, _WhenAll):
            results = []
            for task in yielded.tasks:
                step = task.payload["step"]
                if step["id"] == "s2":
                    results.append(
                        {
                            "workflow_id": "wf-1",
                            "outcome": StepOutcome(
                                step_id="s2",
                                role="analyst",  # type: ignore[arg-type]
                                instruction="分析",
                                status=PlanStepStatus.FAILED,
                                error="RuntimeError: boom",
                            ).model_dump(mode="json"),
                        }
                    )
                else:
                    results.append(_completed_from(task))
            return results
        return script[yielded.name]

    _drive(gen, respond)

    # s2 失败之后不该再为 s3 派发子工作流。
    child_ids = [call[3] for call in ctx.calls if call[0] == "child"]
    assert child_ids == ["wf-1:dyn:r1:s1", "wf-1:dyn:r1:s2"]

    finalize = [call for call in ctx.calls if call[1] == "finalize_activity"][-1]
    checkpoint = finalize[2]["checkpoint"]
    assert finalize[2]["status"] == "completed", "有交付物就不是整次失败"
    assert checkpoint["partial"] is True
    assert checkpoint["failed_steps"] == ["s2"]
    assert checkpoint["skipped_steps"] == ["s3"]
    assert "失败步骤：s2" in (finalize[2]["error"] or "")
    statuses = {node["id"]: node["status"] for node in checkpoint["flow"]}
    assert statuses["s2"] == "failed" and statuses["s3"] == "skipped"


def test_validation_defects_trigger_one_replan_round():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    plan_calls: list[dict[str, Any]] = []

    def respond(yielded: Any) -> Any:
        if isinstance(yielded, _WhenAll):
            return [_completed_from(task) for task in yielded.tasks]
        if yielded.name == "intake_activity":
            return INTAKE_MULTI
        if yielded.name == "dynamic_plan_activity":
            plan_calls.append(yielded.payload)
            round_number = yielded.payload["round"]
            return {
                "workflow_id": "wf-1",
                "round": round_number,
                "plan": {
                    "steps": PARALLEL_STEPS,
                    "source": "llm",
                    "rationale": f"r{round_number}",
                },
            }
        if yielded.name == "dynamic_synthesize_activity":
            return {"status": "completed", "content": f"报告 r{yielded.payload['round']}"}
        if yielded.name == "dynamic_validate_activity":
            if yielded.payload["round"] == 1:
                return {
                    "validation": {
                        "satisfied": False,
                        "defects": ["缺少来源标注"],
                        "missing": ["风险一节"],
                        "source": "model",
                    }
                }
            return VALIDATION_OK
        return {"workflow_id": "wf-1"}

    result = _drive(gen, respond)

    assert [call["round"] for call in plan_calls] == [1, 2]
    assert plan_calls[1]["defects"] == ["缺少来源标注", "风险一节"]
    # 第二轮的子工作流实例 ID 必须换命名空间，否则会和第一轮撞实例。
    second_round_children = [
        call[3] for call in ctx.calls if call[0] == "child" and ":r2:" in call[3]
    ]
    assert second_round_children == ["wf-1:dyn:r2:s1", "wf-1:dyn:r2:s2", "wf-1:dyn:r2:s3"]
    assert result["output"] == "报告 r2"


def test_token_budget_stops_remaining_waves_and_reports_the_gap(monkeypatch):
    """累计预算用尽：不再派发后续波次，剩余步骤按「预算」原因跳过，仍然交付已知结果。"""

    import app.workflows.dynamic as dynamic_module

    monkeypatch.setattr(
        dynamic_module, "get_settings", lambda: AgentSettings(token_budget=1000)
    )
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())

    script: dict[str, Any] = {
        "intake_activity": {**INTAKE_MULTI, "tokens": 400},
        "dynamic_plan_activity": _plan_payload(THREE_STEPS, tokens=300),
        "dynamic_synthesize_activity": {
            "status": "completed",
            "content": "部分报告",
            "tokens": 300,
        },
        "dynamic_validate_activity": VALIDATION_OK,
        "dynamic_checkpoint_activity": {"workflow_id": "wf-1"},
        "finalize_activity": {"workflow_id": "wf-1", "status": "completed"},
    }

    def respond(yielded: Any) -> Any:
        if isinstance(yielded, _WhenAll):
            # 每一步都报 500：加上 intake+规划已经超过 1000 的预算。
            return [
                {
                    "workflow_id": "wf-1",
                    "outcome": _outcome(
                        task.payload["step"]["id"],
                        task.payload["step"]["role"],
                        PlanStepStatus.COMPLETED,
                        "产出",
                        tokens=500,
                    ),
                }
                for task in yielded.tasks
            ]
        return script[yielded.name]

    _drive(gen, respond)

    # s1 跑完之后预算已破：s2、s3 都不该再派发子工作流。
    child_ids = [call[3] for call in ctx.calls if call[0] == "child"]
    assert child_ids == ["wf-1:dyn:r1:s1"]

    finalize = [call for call in ctx.calls if call[1] == "finalize_activity"][-1]
    checkpoint = finalize[2]["checkpoint"]
    assert finalize[2]["status"] == "completed"
    assert finalize[2]["report"] == "部分报告"
    assert checkpoint["budget_exceeded"] is True
    assert checkpoint["token_budget"] == 1000
    assert checkpoint["tokens_used"] == 1500
    assert checkpoint["skipped_steps"] == ["s2", "s3"]
    assert "Token 预算" in (finalize[2]["error"] or "")
    statuses = {node["id"]: node["status"] for node in checkpoint["flow"]}
    assert statuses["s2"] == "skipped" and statuses["s3"] == "skipped"
    # 实际尝试次数进 checkpoint：这里配了默认 3 次、实际只用 1 次。
    plan_entry = {entry["id"]: entry for entry in checkpoint["plan"]}["s1"]
    assert plan_entry["attempts"] == 1
    assert plan_entry["tokens"] == 500


def test_plan_activity_failure_marks_workflow_failed():
    ctx = _ScriptedWorkflowContext("wf-1")
    gen = agent_dynamic_workflow(ctx, _task())
    next(gen)
    # 先过 intake：否则抛进的是 intake 那个 yield，这条用例就不再是在验证"规划失败"。
    gen.send(INTAKE_MULTI)

    # 抛进生成器会先走 except：调度失败终态活动，然后才把异常继续往上抛。
    gen.throw(RuntimeError("planner exploded"))

    assert ctx.calls[-1][0:2] == ("activity", "finalize_activity")
    assert ctx.calls[-1][2]["status"] == "failed"
    assert ctx.calls[-1][2]["error"] == "planner exploded"

    with pytest.raises(RuntimeError, match="planner exploded"):
        gen.send(None)


def test_subtask_workflow_records_the_attempt_that_succeeded():
    """一次成功也算「第 1 次尝试」：`attempts` 是实际次数，不是配置值。"""

    ctx = _ScriptedWorkflowContext("wf-1:dyn:r1:s1")
    activity_input = {
        "task": _task(),
        "step": THREE_STEPS[0],
        "results": {},
        "round": 1,
        "max_attempts": 3,
    }
    gen = dynamic_subtask_workflow(ctx, activity_input)

    gen.send(None)
    # 首次尝试：activity 调用带上 attempt=1；**不再**挂 Dapr 重试策略——
    # 重试是本子工作流里的显式循环（否则拿不到"第几次"）。
    call = ctx.calls[0]
    assert call[0:2] == ("activity", "dynamic_step_activity")
    assert call[2]["attempt"] == 1
    assert call[2]["max_attempts"] == 3
    assert call[3] is None
    assert ctx.statuses == ["dyn:r1:s1"]

    with pytest.raises(StopIteration) as stopped:
        gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "x")})

    assert stopped.value.value["outcome"]["content"] == "x"


def test_subtask_workflow_retries_then_succeeds_and_counts_attempts():
    """第一次失败 → 退避 → 第二次成功：结果里记 `attempts=2`。"""

    ctx = _ScriptedWorkflowContext("wf-1:dyn:r1:s1")
    gen = dynamic_subtask_workflow(
        ctx,
        {
            "task": _task(),
            "step": THREE_STEPS[0],
            "results": {},
            "round": 1,
            "max_attempts": 3,
        },
    )
    gen.send(None)
    assert ctx.calls[0][2]["attempt"] == 1

    # 第一次失败：子工作流不该把异常抛出去，而是发一个退避定时器再试。
    timer = gen.throw(StepAttemptFailed("boom"))
    assert timer.name == "timer"
    assert timer.payload == timedelta(seconds=1)

    gen.send(None)
    assert ctx.calls[-1][2]["attempt"] == 2

    with pytest.raises(StopIteration) as stopped:
        gen.send({"outcome": _outcome("s1", "collector", PlanStepStatus.COMPLETED, "x")})

    outcome = stopped.value.value["outcome"]
    assert outcome["status"] == "completed"
    assert outcome["attempts"] == 2


def test_subtask_workflow_converts_exhausted_retries_into_failed_outcome():
    """重试耗尽 → 业务失败结果（含**实际尝试次数**），不把异常抛给父工作流。"""

    ctx = _ScriptedWorkflowContext("wf-1:dyn:r1:s1")
    gen = dynamic_subtask_workflow(
        ctx,
        {
            "task": _task(),
            "step": THREE_STEPS[0],
            "results": {},
            "round": 1,
            "max_attempts": 2,
        },
    )
    gen.send(None)
    gen.throw(StepAttemptFailed("boom"))  # 第一次失败 → 退避
    gen.send(None)
    assert ctx.calls[-1][2]["attempt"] == 2

    with pytest.raises(StopIteration) as stopped:
        gen.throw(StepAttemptFailed("boom again"))

    outcome = stopped.value.value["outcome"]
    assert outcome["status"] == "failed"
    assert "boom again" in outcome["error"]
    assert outcome["attempts"] == 2, "耗尽时也要记下真实尝试次数"
    assert ctx.statuses[-1] == "dyn:r1:s1:failed"


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
    assert names == [
        WORKFLOW_NAME,
        SUBTASK_WORKFLOW_NAME,
        DYNAMIC_WORKFLOW_NAME,
        DYNAMIC_SUBTASK_WORKFLOW_NAME,
    ]
    assert {
        "intake_activity",
        "dynamic_plan_activity",
        "dynamic_step_activity",
        "dynamic_synthesize_activity",
        "dynamic_validate_activity",
        "dynamic_checkpoint_activity",
    } <= set(runtime.activities)
    # 静态链路的活动一个都没少。
    assert {"run_stage_activity", "collect_activity", "finalize_activity"} <= set(
        runtime.activities
    )


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


def test_fake_step_outcome_is_deterministic():
    from app.agents.roles import RoleId
    from app.orchestration.dynamic_graph import PlanStep

    step = PlanStep(id="s2", role=RoleId.ANALYST, instruction="分析", depends_on=["s1"])

    first = fake_step_outcome(step, "任务")
    second = fake_step_outcome(step, "任务")

    assert first == second
    assert first.status is PlanStepStatus.COMPLETED
    assert "s2" in first.content and "analyst" in first.content
    assert "s1" in first.content

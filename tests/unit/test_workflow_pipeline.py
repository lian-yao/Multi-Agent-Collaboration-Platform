from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.orchestration.pipeline import (
    PipelineStage,
    PipelineStatus,
    deserialize_pipeline_state,
    new_pipeline_state,
    start,
)
from app.workflows.pipeline import (
    PIPELINE_STEPS,
    SUBTASK_WORKFLOW_NAME,
    WorkflowTask,
    advance_pipeline_stage,
    agent_pipeline_workflow,
    agent_subtask_workflow,
    fake_stage_result,
    subtask_instance_id,
)


class _ScriptedRoleModel(BaseChatModel):
    replies: list[str]
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-role-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def test_pipeline_has_three_ordered_steps():
    assert PIPELINE_STEPS == ("collect", "analyze", "report")


def test_workflow_task_is_json_serializable():
    task = WorkflowTask(
        workflow_id="wf-1",
        session_id="session-1",
        agent_run_id="run-1",
        task="hello",
        hold_seconds=2,
        use_fake_model=True,
    )
    payload = task.asdict()
    assert payload["workflow_id"] == "wf-1"
    assert payload["session_id"] == "session-1"
    assert payload["hold_seconds"] == 2
    assert payload["use_fake_model"] is True


def test_fake_stage_chain_keeps_previous_result():
    collected = fake_stage_result("collect", "task-a")
    analyzed = fake_stage_result("analyze", "task-a", previous=collected)
    reported = fake_stage_result("report", "task-a", previous=analyzed)

    assert reported["step"] == "report"
    assert reported["previous"]["step"] == "analyze"
    assert reported["previous"]["previous"]["step"] == "collect"


def test_advance_pipeline_stage_advances_contract_state_and_summary():
    state = start(new_pipeline_state(task="演示任务"))

    outcome = advance_pipeline_stage(
        state,
        PipelineStage.COLLECT,
        task="演示任务",
        use_fake_model=True,
    )
    restored = deserialize_pipeline_state(outcome["state"])

    assert restored.status is PipelineStatus.RUNNING
    assert restored.current_step is PipelineStage.ANALYZE
    assert restored.completed_steps == [PipelineStage.COLLECT]
    assert set(outcome["checkpoint"]) == {
        "status",
        "current_step",
        "completed_steps",
        "updated_at",
    }


def test_advance_pipeline_stage_completes_whole_chain():
    state = start(new_pipeline_state(task="演示任务"))

    for stage in (
        PipelineStage.COLLECT,
        PipelineStage.ANALYZE,
        PipelineStage.REPORT,
    ):
        outcome = advance_pipeline_stage(
            state,
            stage,
            task="演示任务",
            use_fake_model=True,
        )
        state = deserialize_pipeline_state(outcome["state"])

    assert state.status is PipelineStatus.COMPLETED
    assert state.completed_steps == [
        PipelineStage.COLLECT,
        PipelineStage.ANALYZE,
        PipelineStage.REPORT,
    ]
    report = state.results[PipelineStage.REPORT]
    assert report["step"] == "report"
    assert report["previous"]["step"] == "analyze"
    assert report["previous"]["previous"]["step"] == "collect"


def test_advance_pipeline_stage_uses_role_model_when_enabled():
    model = _ScriptedRoleModel(replies=["收集结果", "分析结果", "报告结果"])
    state = start(new_pipeline_state(task="演示任务"))

    collected = advance_pipeline_stage(
        state,
        PipelineStage.COLLECT,
        task="演示任务",
        llm=model,
    )
    state = deserialize_pipeline_state(collected["state"])
    analyzed = advance_pipeline_stage(
        state,
        PipelineStage.ANALYZE,
        task="演示任务",
        llm=model,
    )
    state = deserialize_pipeline_state(analyzed["state"])
    reported = advance_pipeline_stage(
        state,
        PipelineStage.REPORT,
        task="演示任务",
        llm=model,
    )

    final_state = deserialize_pipeline_state(reported["state"])
    assert final_state.results[PipelineStage.REPORT]["content"] == "报告结果"
    assert len(model.calls) == 3
    assert final_state.results[PipelineStage.ANALYZE]["previous"]["content"] == "收集结果"


def test_workflow_function_is_generator():
    assert inspect_isgeneratorfunction(agent_pipeline_workflow)
    assert inspect_isgeneratorfunction(agent_subtask_workflow)


def inspect_isgeneratorfunction(fn):
    import inspect

    return inspect.isgeneratorfunction(fn)


class _ScriptedTask:
    pass


class _ScriptedWorkflowContext:
    """Minimal stand-in for DaprWorkflowContext used to drive the orchestrator."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.calls: list[tuple[str, Any, Any]] = []

    @staticmethod
    def _name(value: Any) -> str:
        if isinstance(value, str):
            return value
        alternate = getattr(value, "__dict__", {}).get("_dapr_alternate_name")
        return alternate or getattr(value, "__name__", str(value))

    def call_activity(self, activity, *, input=None, **_kwargs) -> _ScriptedTask:
        self.calls.append(("activity", self._name(activity), input))
        return _ScriptedTask()

    def call_child_workflow(self, workflow, *, input=None, instance_id=None, **_kwargs):
        self.calls.append(("child", self._name(workflow), input))
        assert instance_id
        return _ScriptedTask()

    def create_timer(self, _delta) -> _ScriptedTask:
        return _ScriptedTask()

    def set_custom_status(self, _status: str) -> None:
        pass


def _stage_outputs(task_text: str) -> list[dict[str, Any]]:
    state = start(new_pipeline_state(task=task_text))
    outputs: list[dict[str, Any]] = []
    for stage in (PipelineStage.COLLECT, PipelineStage.ANALYZE, PipelineStage.REPORT):
        outcome = advance_pipeline_stage(
            state,
            stage,
            task=task_text,
            use_fake_model=True,
        )
        state = deserialize_pipeline_state(outcome["state"])
        outputs.append(outcome)
    return outputs


def test_completed_workflow_schedules_subtasks_and_terminal_finalize():
    ctx = _ScriptedWorkflowContext(instance_id="wf-1")
    task = {
        "workflow_id": "wf-1",
        "session_id": "session-1",
        "agent_run_id": "run-1",
        "message_id": "msg-1",
        "task": "演示任务",
        "hold_seconds": 0,
        "use_fake_model": True,
    }
    gen = agent_pipeline_workflow(ctx, task)

    gen.send(None)
    for output in _stage_outputs("演示任务"):
        gen.send(output)

    child_calls = [call for call in ctx.calls if call[0] == "child"]
    assert [call[1] for call in child_calls] == [agent_subtask_workflow.__name__] * 3
    assert [call[2]["step"] for call in child_calls] == [
        "collect",
        "analyze",
        "report",
    ]
    assert [call[2]["workflow_id"] for call in child_calls] == ["wf-1"] * 3
    assert ctx.calls[-1][0:2] == ("activity", "finalize_activity")
    assert ctx.calls[-1][2]["status"] == "completed"
    assert ctx.calls[-1][2]["checkpoint"]["status"] == (
        PipelineStatus.COMPLETED.value
    )

    try:
        gen.send(None)
    except StopIteration:
        pass


def test_subtask_workflow_wraps_stage_in_one_activity():
    ctx = _ScriptedWorkflowContext(instance_id="wf-1:collect")
    activity_input = {
        "workflow_id": "wf-1",
        "step": "collect",
        "task": {"workflow_id": "wf-1", "task": "演示任务"},
        "payload": {
            "step": "collect",
            "state": {
                "task": "演示任务",
                "status": "running",
                "current_step": "collect",
                "completed_steps": [],
                "results": {},
                "error": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
            "attempt": 1,
        },
    }
    gen = agent_subtask_workflow(ctx, activity_input)
    gen.send(None)

    assert ctx.calls == [
        ("activity", "run_stage_activity", activity_input),
    ]

    try:
        gen.send({"state": {}, "checkpoint": {}, "result": {}})
    except StopIteration:
        pass


def test_subtask_instance_id_is_stable_per_stage():
    assert subtask_instance_id("wf-1", PipelineStage.COLLECT) == "wf-1:collect"
    assert subtask_instance_id("wf-1", PipelineStage.REPORT) == "wf-1:report"


def test_failed_stage_schedules_failure_finalize_before_reraise():
    ctx = _ScriptedWorkflowContext(instance_id="wf-1")
    task = {
        "workflow_id": "wf-1",
        "session_id": "session-1",
        "agent_run_id": "run-1",
        "message_id": "msg-1",
        "task": "演示任务",
        "hold_seconds": 0,
        "use_fake_model": True,
    }
    gen = agent_pipeline_workflow(ctx, task)
    gen.send(None)

    try:
        pending = gen.throw(RuntimeError("collect failed"))
    except RuntimeError:
        pytest.fail("阶段异常未触发终态回写活动")

    assert ctx.calls[-1][0:2] == ("activity", "finalize_activity")
    assert ctx.calls[-1][2]["status"] == "failed"
    assert ctx.calls[-1][2]["error"] == "collect failed"

    with pytest.raises(RuntimeError):
        gen.send(None)


def test_finalize_activity_backfills_business_rows(monkeypatch):
    from app.workflows.pipeline import finalize_activity

    updated: dict[str, list[Any]] = {}

    def fake_update_workflow_run(
        workflow_id,
        *,
        status=None,
        checkpoint=None,
        error=None,
    ) -> dict[str, Any]:
        updated["workflow"] = [workflow_id, status, checkpoint, error]
        return {}

    def fake_update_agent_run_status(agent_run_id, status) -> dict[str, Any]:
        updated["agent_run"] = [agent_run_id, status]
        return {}

    def fake_update_message_status(message_id, status) -> dict[str, Any]:
        updated["message"] = [message_id, status]
        return {}

    monkeypatch.setattr(
        "app.workflows.pipeline.update_workflow_run",
        fake_update_workflow_run,
    )
    monkeypatch.setattr(
        "app.workflows.pipeline.update_agent_run_status",
        fake_update_agent_run_status,
    )
    monkeypatch.setattr(
        "app.workflows.pipeline.update_message_status",
        fake_update_message_status,
    )

    class FakeActivityContext:
        workflow_id = "wf-1"

    finalize_activity(
        FakeActivityContext(),
        {
            "workflow_id": "wf-1",
            "agent_run_id": "run-1",
            "message_id": "msg-1",
            "status": "completed",
            "checkpoint": {"status": "completed"},
            "error": None,
        },
    )

    assert updated["workflow"] == [
        "wf-1",
        "completed",
        {"status": "completed"},
        None,
    ]
    assert updated["agent_run"] == ["run-1", "completed"]
    assert updated["message"] == ["msg-1", "completed"]
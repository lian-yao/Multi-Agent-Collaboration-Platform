from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agents.roles import get_role, role_ids
from app.workflows.pipeline import (
    PIPELINE_STEPS,
    WorkflowTask,
    advance_pipeline_stage,
    agent_pipeline_workflow,
)
from app.orchestration.pipeline import (
    PipelineStage,
    PipelineStatus,
    deserialize_pipeline_state,
    new_pipeline_state,
    start,
)


class ScriptedChatModel(BaseChatModel):
    """按序返回固定回复并记录调用消息，替代真实 LLM（不访问网络）。"""

    replies: list[str]
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_pipeline_has_three_ordered_steps():
    assert PIPELINE_STEPS == ("collect", "analyze", "report")


def test_workflow_task_is_json_serializable():
    task = WorkflowTask(
        workflow_id="wf-1",
        session_id="session-1",
        agent_run_id="run-1",
        task="hello",
        hold_seconds=2,
    )
    payload = task.asdict()
    assert payload["workflow_id"] == "wf-1"
    assert payload["session_id"] == "session-1"
    assert payload["hold_seconds"] == 2


def test_advance_pipeline_stage_returns_model_content():
    """阶段结果必须来自模型，而不是 D3-D4 的占位串（ADR-007）。"""
    fake = ScriptedChatModel(replies=["要点一\n要点二"])
    state = start(new_pipeline_state(task="演示任务"))

    outcome = advance_pipeline_stage(
        state,
        PipelineStage.COLLECT,
        task="演示任务",
        llm=fake,
    )

    restored = deserialize_pipeline_state(outcome["state"])
    content = restored.results[PipelineStage.COLLECT]["content"]
    assert content == "要点一\n要点二"
    assert "collected task" not in content


def test_advance_pipeline_stage_uses_each_role_system_prompt():
    fake = ScriptedChatModel(replies=["要点", "结论", "报告"])
    state = start(new_pipeline_state(task="演示任务"))

    for stage in PIPELINE_STEPS:
        outcome = advance_pipeline_stage(state, stage, task="演示任务", llm=fake)
        state = deserialize_pipeline_state(outcome["state"])

    assert [call[0].content for call in fake.calls] == [
        get_role(role).system_prompt for role in role_ids()
    ]


def test_advance_pipeline_stage_feeds_upstream_content_downstream():
    fake = ScriptedChatModel(replies=["上游要点", "分析结论", "最终报告"])
    state = start(new_pipeline_state(task="演示任务"))

    for stage in PIPELINE_STEPS:
        outcome = advance_pipeline_stage(state, stage, task="演示任务", llm=fake)
        state = deserialize_pipeline_state(outcome["state"])

    assert "上游要点" in fake.calls[1][1].content
    assert "分析结论" in fake.calls[2][1].content
    report = state.results[PipelineStage.REPORT]
    assert report["content"] == "最终报告"
    assert report["previous"]["previous"]["content"] == "上游要点"


def test_advance_pipeline_stage_propagates_model_failure():
    """模型不可用时错误必须抛出，交由 Workflow 异常分支回写 failed（ADR-007）。"""

    class UnavailableChatModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("ollama unreachable")

    state = start(new_pipeline_state(task="演示任务"))

    with pytest.raises(RuntimeError, match="ollama unreachable"):
        advance_pipeline_stage(
            state,
            PipelineStage.COLLECT,
            task="演示任务",
            llm=UnavailableChatModel(replies=[]),
        )


def test_advance_pipeline_stage_advances_contract_state_and_summary():
    state = start(new_pipeline_state(task="演示任务"))

    outcome = advance_pipeline_stage(
        state,
        PipelineStage.COLLECT,
        task="演示任务",
        llm=ScriptedChatModel(replies=["要点"]),
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
    fake = ScriptedChatModel(replies=["要点", "结论", "报告"])

    for stage in (
        PipelineStage.COLLECT,
        PipelineStage.ANALYZE,
        PipelineStage.REPORT,
    ):
        outcome = advance_pipeline_stage(state, stage, task="演示任务", llm=fake)
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


def test_workflow_function_is_generator():
    assert inspect_isgeneratorfunction(agent_pipeline_workflow)


def inspect_isgeneratorfunction(fn):
    import inspect

    return inspect.isgeneratorfunction(fn)


class _ScriptedTask:
    def __init__(self) -> None:
        self.result: Any = None


class _ScriptedWorkflowContext:
    """Minimal stand-in for DaprWorkflowContext used to drive the orchestrator.

    call_activity/create_timer return tasks in scheduling order; tests resolve
    them by sending the task back into the generator. No real Dapr runtime is
    involved, so the assertions describe the workflow's durable task schedule.
    """

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.calls: list[tuple[str, Any]] = []

    def call_activity(self, activity, *, input=None, **_kwargs) -> _ScriptedTask:
        name = (
            activity
            if isinstance(activity, str)
            else getattr(activity, "__name__", str(activity))
        )
        self.calls.append((name, input))
        return _ScriptedTask()

    def create_timer(self, _delta) -> _ScriptedTask:
        return _ScriptedTask()

    def set_custom_status(self, _status: str) -> None:
        pass


def _stage_outputs(task_text: str) -> list[dict[str, Any]]:
    """Return the serialized stage outputs the real activities would produce."""
    state = start(new_pipeline_state(task=task_text))
    fake = ScriptedChatModel(replies=["收集结果", "分析结论", "报告正文"])
    outputs: list[dict[str, Any]] = []
    for stage in (PipelineStage.COLLECT, PipelineStage.ANALYZE, PipelineStage.REPORT):
        outcome = advance_pipeline_stage(state, stage, task=task_text, llm=fake)
        state = deserialize_pipeline_state(outcome["state"])
        outputs.append(outcome)
    return outputs


def test_completed_workflow_schedules_terminal_finalize_activity():
    ctx = _ScriptedWorkflowContext(instance_id="wf-1")
    task = {
        "workflow_id": "wf-1",
        "session_id": "session-1",
        "agent_run_id": "run-1",
        "message_id": "msg-1",
        "task": "演示任务",
        "hold_seconds": 0,
    }
    gen = agent_pipeline_workflow(ctx, task)

    gen.send(None)
    for output in _stage_outputs("演示任务"):
        pending = gen.send(output)

    # After the final report stage, the workflow must schedule one more durable
    # activity that back-fills workflow_runs / agent_runs / messages. Today it
    # simply returns, which is why the API row stays running forever.
    assert ctx.calls[-1][0] == "finalize_activity"
    assert ctx.calls[-1][1]["status"] == "completed"
    assert ctx.calls[-1][1]["workflow_id"] == "wf-1"
    assert ctx.calls[-1][1]["agent_run_id"] == "run-1"
    assert ctx.calls[-1][1]["message_id"] == "msg-1"
    assert ctx.calls[-1][1]["checkpoint"]["status"] == PipelineStatus.COMPLETED.value

    try:
        gen.send(None)  # resolve finalizer; generator finishes
    except StopIteration:
        pass


def test_failed_stage_schedules_failure_finalize_before_reraise():
    ctx = _ScriptedWorkflowContext(instance_id="wf-1")
    task = {
        "workflow_id": "wf-1",
        "session_id": "session-1",
        "agent_run_id": "run-1",
        "message_id": "msg-1",
        "task": "演示任务",
        "hold_seconds": 0,
    }
    gen = agent_pipeline_workflow(ctx, task)
    gen.send(None)  # collect task is pending

    try:
        pending = gen.throw(RuntimeError("collect failed"))
    except RuntimeError:
        pytest.fail("阶段异常未触发终态回写活动")

    assert ctx.calls[-1][0] == "finalize_activity"
    assert ctx.calls[-1][1]["status"] == "failed"
    assert ctx.calls[-1][1]["error"] == "collect failed"

    with pytest.raises(RuntimeError):
        gen.send(None)  # resolve finalizer; original error is re-raised


def test_finalize_activity_backfills_business_rows(monkeypatch):
    """Terminal states must be persisted for workflow run, agent run, and message."""
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

from app.workflows.pipeline import (
    PIPELINE_STEPS,
    WorkflowTask,
    advance_pipeline_stage,
    agent_pipeline_workflow,
    fake_stage_result,
)
from app.orchestration.pipeline import (
    PipelineStage,
    PipelineStatus,
    deserialize_pipeline_state,
    new_pipeline_state,
    start,
)


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
        outcome = advance_pipeline_stage(state, stage, task="演示任务")
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

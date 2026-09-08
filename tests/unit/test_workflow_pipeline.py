from app.workflows.pipeline import (
    PIPELINE_STEPS,
    WorkflowTask,
    agent_pipeline_workflow,
    fake_stage_result,
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


def test_workflow_function_is_generator():
    assert inspect_isgeneratorfunction(agent_pipeline_workflow)


def inspect_isgeneratorfunction(fn):
    import inspect

    return inspect.isgeneratorfunction(fn)
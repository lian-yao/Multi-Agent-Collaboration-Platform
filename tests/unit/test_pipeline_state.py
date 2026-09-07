import json

import pytest

from app.orchestration.pipeline import (
    PIPELINE_STEPS,
    PipelineStage,
    PipelineStatus,
    build_step_payload,
    build_step_result,
    complete_step,
    deserialize_pipeline_state,
    fail,
    new_pipeline_state,
    parse_step_payload,
    pause,
    pipeline_checkpoint_summary,
    resume,
    serialize_pipeline_state,
    start,
)


def test_new_state_begins_pending_without_current_step():
    state = new_pipeline_state(task="演示任务")

    assert state.status is PipelineStatus.PENDING
    assert state.current_step is None
    assert state.completed_steps == []


def test_start_marks_running_and_selects_collect():
    state = start(new_pipeline_state(task="演示任务"))

    assert state.status is PipelineStatus.RUNNING
    assert state.current_step is PipelineStage.COLLECT


def test_start_rejects_already_started_state():
    state = start(new_pipeline_state(task="演示任务"))

    with pytest.raises(ValueError, match="只能从 pending 开始"):
        start(state)


def test_complete_step_advances_in_fixed_order():
    state = start(new_pipeline_state(task="演示任务"))

    state = complete_step(state, PipelineStage.COLLECT, {"items": ["资料一"]})

    assert state.completed_steps == [PipelineStage.COLLECT]
    assert state.current_step is PipelineStage.ANALYZE

    state = complete_step(state, PipelineStage.ANALYZE, {"summary": "摘要"})

    assert state.completed_steps == [PipelineStage.COLLECT, PipelineStage.ANALYZE]
    assert state.current_step is PipelineStage.REPORT


def test_complete_last_step_finishes_pipeline():
    state = start(new_pipeline_state(task="演示任务"))
    for stage, result in (
        (PipelineStage.COLLECT, {"items": []}),
        (PipelineStage.ANALYZE, {"summary": ""}),
        (PipelineStage.REPORT, {"report": "报告"}),
    ):
        state = complete_step(state, stage, result)

    assert state.status is PipelineStatus.COMPLETED
    assert state.current_step is None
    assert state.completed_steps == list(PIPELINE_STEPS)


def test_complete_step_rejects_out_of_order_step():
    state = start(new_pipeline_state(task="演示任务"))

    with pytest.raises(ValueError, match="当前步骤是 collect"):
        complete_step(state, PipelineStage.ANALYZE, {"summary": "摘要"})


def test_complete_step_rejects_duplicate_step():
    state = start(new_pipeline_state(task="演示任务"))
    state = complete_step(state, PipelineStage.COLLECT, {"items": []})

    with pytest.raises(ValueError, match="已经完成"):
        complete_step(state, PipelineStage.COLLECT, {"items": ["重复"]})


def test_pause_keeps_step_and_resume_restores_running():
    state = pause(start(new_pipeline_state(task="演示任务")))

    assert state.status is PipelineStatus.PAUSED
    assert state.current_step is PipelineStage.COLLECT

    resumed = resume(state)

    assert resumed.status is PipelineStatus.RUNNING
    assert resumed.current_step is PipelineStage.COLLECT


def test_pause_rejects_state_not_running():
    with pytest.raises(ValueError, match="只能从 running 暂停"):
        pause(new_pipeline_state(task="演示任务"))


def test_resume_rejects_state_not_paused():
    with pytest.raises(ValueError, match="只能从 paused 恢复"):
        resume(start(new_pipeline_state(task="演示任务")))


def test_fail_marks_state_failed_with_error():
    state = fail(
        start(new_pipeline_state(task="演示任务")),
        error="LLM 调用超时",
    )

    assert state.status is PipelineStatus.FAILED
    assert state.error == "LLM 调用超时"


def test_fail_rejects_already_failed_state():
    state = fail(start(new_pipeline_state(task="演示任务")), error="第一次失败")

    with pytest.raises(ValueError, match="终态"):
        fail(state, error="第二次失败")


def test_step_payload_roundtrips_with_attempt_number():
    state = start(new_pipeline_state(task="演示任务"))

    payload = build_step_payload(PipelineStage.ANALYZE, state, attempt=2)
    step, restored, attempt = parse_step_payload(payload)

    assert step is PipelineStage.ANALYZE
    assert restored == state
    assert attempt == 2


def test_step_result_carries_state_and_checkpoint_summary():
    state = complete_step(
        start(new_pipeline_state(task="演示任务")),
        PipelineStage.COLLECT,
        {"items": ["资料一"]},
    )

    result = build_step_result(state)
    restored = deserialize_pipeline_state(result["state"])
    summary = result["checkpoint"]

    assert restored == state
    assert summary["status"] == "running"
    assert summary["current_step"] == "analyze"


def test_pipeline_state_roundtrips_through_json():
    state = start(new_pipeline_state(task="演示任务"))
    state = complete_step(state, PipelineStage.COLLECT, {"items": ["资料一"]})

    restored = deserialize_pipeline_state(json.dumps(serialize_pipeline_state(state)))

    assert restored == state
    assert restored.results[PipelineStage.COLLECT] == {"items": ["资料一"]}


def test_checkpoint_summary_excludes_full_results():
    state = start(new_pipeline_state(task="演示任务"))
    state = complete_step(state, PipelineStage.COLLECT, {"items": ["资料一"]})

    summary = pipeline_checkpoint_summary(state)

    assert set(summary) == {"status", "current_step", "completed_steps", "updated_at"}
    assert summary["status"] == "running"
    assert summary["current_step"] == "analyze"
    assert summary["completed_steps"] == ["collect"]
    assert "results" not in summary

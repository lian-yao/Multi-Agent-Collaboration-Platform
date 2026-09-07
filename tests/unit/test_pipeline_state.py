import json

import pytest

from app.orchestration.pipeline import (
    PIPELINE_STEPS,
    PipelineStage,
    PipelineStatus,
    complete_step,
    deserialize_pipeline_state,
    new_pipeline_state,
    pipeline_checkpoint_summary,
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

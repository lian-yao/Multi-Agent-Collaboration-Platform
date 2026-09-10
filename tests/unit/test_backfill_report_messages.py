"""历史报告补写脚本的单元测试（ADR-008）。

这里只覆盖纯解析逻辑；真实写入走与在线路径同一个幂等 upsert，不在单测里连库。
"""

from app.orchestration.pipeline import (
    PipelineStage,
    complete_step,
    new_pipeline_state,
    serialize_pipeline_state,
    start,
)
from scripts.backfill_report_messages import report_from_payload


def _report_payload(content: str) -> dict:
    state = start(new_pipeline_state(task="演示任务"))
    for stage in (PipelineStage.COLLECT, PipelineStage.ANALYZE):
        state = complete_step(
            state,
            stage,
            {
                "step": stage.value,
                "status": "completed",
                "content": "上游内容",
                "previous": None,
            },
        )
    state = complete_step(
        state,
        PipelineStage.REPORT,
        {
            "step": "report",
            "status": "completed",
            "content": content,
            "previous": None,
        },
    )
    return {
        "workflow_id": "wf-1",
        "step": "report",
        "result": serialize_pipeline_state(state),
    }


def test_report_from_payload_extracts_report_content():
    assert report_from_payload(_report_payload("# 报告正文")) == "# 报告正文"


def test_report_from_payload_returns_none_when_result_missing():
    assert report_from_payload({"workflow_id": "wf-1", "step": "report"}) is None


def test_report_from_payload_returns_none_when_result_is_not_pipeline_state():
    assert report_from_payload({"result": "not-a-state"}) is None

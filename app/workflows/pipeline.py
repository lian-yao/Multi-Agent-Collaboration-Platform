from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

import dapr.ext.workflow as wf

from app.core.checkpoint import update_workflow_run
from app.workflows.state import save_step_result

WORKFLOW_NAME = "agent_pipeline"
COLLECT_STEP = "collect"
ANALYZE_STEP = "analyze"
REPORT_STEP = "report"
PIPELINE_STEPS = (COLLECT_STEP, ANALYZE_STEP, REPORT_STEP)


@dataclass
class WorkflowTask:
    workflow_id: str
    task: str
    session_id: str | None = None
    agent_run_id: str | None = None
    hold_seconds: int = 0

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def fake_stage_result(
    step: str,
    task: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic fake LLM output used by the D3-D4 workflow POC."""
    if step == COLLECT_STEP:
        content = f"collected task: {task}"
    elif step == ANALYZE_STEP:
        content = f"analyzed task: {task} (chars={len(task)})"
    elif step == REPORT_STEP:
        content = f"report: {task}"
    else:
        raise ValueError(f"unknown pipeline step: {step}")
    return {
        "step": step,
        "status": "completed",
        "content": content,
        "previous": previous,
    }


def _record_checkpoint(workflow_id: str, step: str, result: dict[str, Any]) -> None:
    save_step_result(workflow_id, step, result)
    update_workflow_run(
        workflow_id,
        status="running",
        current_step=step,
        checkpoint=result,
    )


def collect_activity(ctx: wf.WorkflowActivityContext, task: dict[str, Any]) -> dict[str, Any]:
    result = fake_stage_result(COLLECT_STEP, task["task"])
    _record_checkpoint(ctx.workflow_id, COLLECT_STEP, result)
    return result


def analyze_activity(
    ctx: wf.WorkflowActivityContext, payload: dict[str, Any]
) -> dict[str, Any]:
    task = payload["task"]
    collected = payload["collected"]
    result = fake_stage_result(ANALYZE_STEP, task["task"], previous=collected)
    _record_checkpoint(ctx.workflow_id, ANALYZE_STEP, result)
    return result


def report_activity(
    ctx: wf.WorkflowActivityContext, payload: dict[str, Any]
) -> dict[str, Any]:
    task = payload["task"]
    analyzed = payload["analyzed"]
    result = fake_stage_result(REPORT_STEP, task["task"], previous=analyzed)
    _record_checkpoint(ctx.workflow_id, REPORT_STEP, result)
    return result


def agent_pipeline_workflow(
    ctx: wf.DaprWorkflowContext, task: dict[str, Any]
) -> dict[str, Any]:
    ctx.set_custom_status(COLLECT_STEP)
    collected = yield ctx.call_activity(collect_activity, input=task)

    hold_seconds = int(task.get("hold_seconds") or 0)
    if hold_seconds > 0:
        yield ctx.create_timer(timedelta(seconds=hold_seconds))

    ctx.set_custom_status(ANALYZE_STEP)
    analyzed = yield ctx.call_activity(
        analyze_activity,
        input={"task": task, "collected": collected},
    )

    ctx.set_custom_status(REPORT_STEP)
    reported = yield ctx.call_activity(
        report_activity,
        input={"task": task, "collected": collected, "analyzed": analyzed},
    )
    ctx.set_custom_status("completed")
    return reported
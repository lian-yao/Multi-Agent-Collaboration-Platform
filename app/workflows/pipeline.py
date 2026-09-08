"""固定三步流水线的 Dapr Workflow，编排状态统一消费 app.orchestration 的契约。

成员 B 的 D3-D4 实现此前自带 WorkflowTask/字段，未引用成员 A 冻结的
PipelineState。本文件把每一步活动改为：

- 输入使用 ``app.orchestration.pipeline.build_step_payload``；
- 状态推进使用 ``PipelineState`` 与 ``complete_step``；
- 落库/展示只写 ``pipeline_checkpoint_summary``，完整结果保留在活动输出与
  Dapr State Store 中。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

import dapr.ext.workflow as wf

from app.core.checkpoint import update_workflow_run
from app.orchestration.pipeline import (
    PIPELINE_STEPS as PIPELINE_CONTRACT_STEPS,
    PipelineStage,
    PipelineState,
    PipelineStatus,
    build_step_payload,
    build_step_result,
    complete_step,
    deserialize_pipeline_state,
    new_pipeline_state,
    parse_step_payload,
    pipeline_checkpoint_summary,
    serialize_pipeline_state,
    start,
)
from app.workflows.state import save_step_result

WORKFLOW_NAME = "agent_pipeline"
COLLECT_STEP = PipelineStage.COLLECT.value
ANALYZE_STEP = PipelineStage.ANALYZE.value
REPORT_STEP = PipelineStage.REPORT.value
PIPELINE_STEPS = tuple(stage.value for stage in PIPELINE_CONTRACT_STEPS)


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


def _to_stage(step: PipelineStage | str) -> PipelineStage:
    return step if isinstance(step, PipelineStage) else PipelineStage(step)


def _record_checkpoint(
    workflow_id: str,
    step: PipelineStage,
    state: PipelineState,
) -> None:
    save_step_result(workflow_id, step.value, serialize_pipeline_state(state))
    update_workflow_run(
        workflow_id,
        status=PipelineStatus.RUNNING.value,
        current_step=step.value,
        checkpoint=pipeline_checkpoint_summary(state),
    )


def advance_pipeline_stage(
    state: PipelineState,
    step: PipelineStage | str,
    task: str,
) -> dict[str, Any]:
    """用编排契约推进一个阶段，返回活动输出所需的状态与 checkpoint。"""

    stage = _to_stage(step)
    if stage != state.current_step:
        current = state.current_step.value if state.current_step else "无"
        raise ValueError(f"当前步骤是 {current}，不能执行 {stage.value}")

    previous = None
    if stage is PipelineStage.ANALYZE:
        previous = state.results.get(PipelineStage.COLLECT.value)
    elif stage is PipelineStage.REPORT:
        previous = state.results.get(PipelineStage.ANALYZE.value)

    result = fake_stage_result(stage.value, task, previous=previous)
    updated = complete_step(state, stage, result)
    return {**build_step_result(updated), "result": result}


def _run_stage_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
    expected_stage: PipelineStage,
) -> dict[str, Any]:
    task = activity_input["task"]
    stage, state, _attempt = parse_step_payload(activity_input["payload"])
    if stage is not expected_stage:
        raise ValueError(
            f"activity {expected_stage.value} 收到错误阶段: {stage.value}"
        )

    outcome = advance_pipeline_stage(state, stage, task["task"])
    _record_checkpoint(
        ctx.workflow_id,
        expected_stage,
        deserialize_pipeline_state(outcome["state"]),
    )
    return outcome


def collect_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    return _run_stage_activity(ctx, activity_input, PipelineStage.COLLECT)


def analyze_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    return _run_stage_activity(ctx, activity_input, PipelineStage.ANALYZE)


def report_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    return _run_stage_activity(ctx, activity_input, PipelineStage.REPORT)


def agent_pipeline_workflow(
    ctx: wf.DaprWorkflowContext,
    task: dict[str, Any],
) -> dict[str, Any]:
    state = start(new_pipeline_state(task=task["task"]))

    ctx.set_custom_status(COLLECT_STEP)
    collected = yield ctx.call_activity(
        collect_activity,
        input={
            "task": task,
            "payload": build_step_payload(PipelineStage.COLLECT, state),
        },
    )
    state = deserialize_pipeline_state(collected["state"])

    hold_seconds = int(task.get("hold_seconds") or 0)
    if hold_seconds > 0:
        yield ctx.create_timer(timedelta(seconds=hold_seconds))

    ctx.set_custom_status(ANALYZE_STEP)
    analyzed = yield ctx.call_activity(
        analyze_activity,
        input={
            "task": task,
            "payload": build_step_payload(PipelineStage.ANALYZE, state),
        },
    )
    state = deserialize_pipeline_state(analyzed["state"])

    ctx.set_custom_status(REPORT_STEP)
    reported = yield ctx.call_activity(
        report_activity,
        input={
            "task": task,
            "payload": build_step_payload(PipelineStage.REPORT, state),
        },
    )
    state = deserialize_pipeline_state(reported["state"])

    ctx.set_custom_status("completed")
    return {
        "output": state.results[PipelineStage.REPORT.value],
        "state": serialize_pipeline_state(state),
    }

"""固定三步流水线的 Dapr Workflow，编排状态统一消费 app.orchestration 的契约。

成员 B 的 D5-D6 实现把每个阶段封装为独立子 Workflow：

- 父 Workflow ``agent_pipeline_workflow`` 只负责阶段顺序与终态回写；
- 每个阶段通过 ``call_child_workflow`` 调度 ``agent_subtask_workflow``；
- 子 Workflow 使用稳定的 ``{workflow_id}:{stage}`` 实例 ID，可跨进程恢复；
- 子 Workflow 内部调用角色活动，活动结果由 Dapr 持久化，应用侧只写摘要。

``use_fake_model=True`` 时保留 D3-D4 POC 的确定性假模型，供故障恢复演练
和没有 Ollama 的本地测试使用；默认使用成员 A 提供的真实角色阶段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

import dapr.ext.workflow as wf

from app.config import AgentSettings
from app.core.checkpoint import (
    update_agent_run_status,
    update_message_status,
    update_workflow_run,
)
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
from app.orchestration.pipeline_graph import run_role_stage
from app.workflows.state import save_step_result

WORKFLOW_NAME = "agent_pipeline"
SUBTASK_WORKFLOW_NAME = "agent_subtask"
COLLECT_STEP = PipelineStage.COLLECT.value
ANALYZE_STEP = PipelineStage.ANALYZE.value
REPORT_STEP = PipelineStage.REPORT.value
PIPELINE_STEPS = tuple(stage.value for stage in PIPELINE_CONTRACT_STEPS)

SUBTASK_RETRY_POLICY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=1),
    max_number_of_attempts=3,
    backoff_coefficient=2,
    max_retry_interval=timedelta(seconds=10),
)


@dataclass
class WorkflowTask:
    workflow_id: str
    task: str
    session_id: str | None = None
    agent_run_id: str | None = None
    message_id: str | None = None
    hold_seconds: int = 0
    use_fake_model: bool = False

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def fake_stage_result(
    step: str,
    task: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """确定性假模型输出，供恢复演练与无 Ollama 环境使用。"""
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
    *,
    llm: Any | None = None,
    settings: AgentSettings | None = None,
    use_fake_model: bool = False,
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

    if use_fake_model:
        result = fake_stage_result(stage.value, task, previous=previous)
    else:
        result = run_role_stage(
            stage,
            task,
            previous=previous,
            llm=llm,
            settings=settings,
        )
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

    workflow_id = str(
        activity_input.get("workflow_id")
        or task.get("workflow_id")
        or ctx.workflow_id
    )
    outcome = advance_pipeline_stage(
        state,
        stage,
        task["task"],
        use_fake_model=bool(task.get("use_fake_model")),
    )
    _record_checkpoint(
        workflow_id,
        expected_stage,
        deserialize_pipeline_state(outcome["state"]),
    )
    return {**outcome, "subtask_instance_id": ctx.workflow_id}


def run_stage_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """子 Workflow 内部使用的通用阶段活动。"""

    stage = PipelineStage(activity_input["step"])
    return _run_stage_activity(ctx, activity_input, stage)


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


def finalize_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """Back-fill terminal state to workflow_runs / agent_runs / messages.

    The stage activities only ever report ``running`` progress. Without this
    durable activity, an API-scheduled workflow completes inside Dapr but the
    business rows stay ``running`` forever because nothing waits for completion.
    Executed inside the workflow makes the back-fill survive process restarts.
    """
    status = activity_input["status"]
    if status not in {"completed", "failed"}:
        raise ValueError(f"unsupported terminal status: {status}")

    workflow_id = activity_input["workflow_id"]
    update_workflow_run(
        workflow_id,
        status=status,
        checkpoint=activity_input.get("checkpoint"),
        error=activity_input.get("error"),
    )
    agent_run_id = activity_input.get("agent_run_id")
    if agent_run_id:
        update_agent_run_status(agent_run_id, status)
    message_id = activity_input.get("message_id")
    if message_id:
        update_message_status(message_id, status)
    return {"workflow_id": workflow_id, "status": status}


def build_subtask_input(
    workflow_id: str,
    task: dict[str, Any],
    state: PipelineState,
    stage: PipelineStage | str,
) -> dict[str, Any]:
    """构造子 Workflow 载荷；实例 ID 由父 Workflow 固定。"""

    resolved = _to_stage(stage)
    return {
        "workflow_id": workflow_id,
        "step": resolved.value,
        "task": task,
        "payload": build_step_payload(resolved, state),
    }


def subtask_instance_id(workflow_id: str, stage: PipelineStage | str) -> str:
    return f"{workflow_id}:{_to_stage(stage).value}"


def agent_subtask_workflow(
    ctx: wf.DaprWorkflowContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """单个可恢复子任务：一个持久化边界内执行一个阶段活动。"""

    stage = PipelineStage(activity_input["step"])
    ctx.set_custom_status(f"subtask:{stage.value}")
    outcome = yield ctx.call_activity(run_stage_activity, input=activity_input)
    return outcome


def _call_subtask(
    ctx: wf.DaprWorkflowContext,
    workflow_id: str,
    task: dict[str, Any],
    state: PipelineState,
    stage: PipelineStage,
) -> Any:
    """调用一个固定实例 ID 的子 Workflow，重放时复用已完成结果。"""

    return ctx.call_child_workflow(
        agent_subtask_workflow,
        input=build_subtask_input(workflow_id, task, state, stage),
        instance_id=subtask_instance_id(workflow_id, stage),
        retry_policy=SUBTASK_RETRY_POLICY,
    )


def agent_pipeline_workflow(
    ctx: wf.DaprWorkflowContext,
    task: dict[str, Any],
) -> dict[str, Any]:
    state = start(new_pipeline_state(task=task["task"]))
    workflow_id = str(task.get("workflow_id") or ctx.instance_id)

    terminal_input: dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent_run_id": task.get("agent_run_id"),
        "message_id": task.get("message_id"),
    }
    try:
        ctx.set_custom_status(COLLECT_STEP)
        collected = yield _call_subtask(
            ctx,
            workflow_id,
            task,
            state,
            PipelineStage.COLLECT,
        )
        state = deserialize_pipeline_state(collected["state"])

        hold_seconds = int(task.get("hold_seconds") or 0)
        if hold_seconds > 0:
            yield ctx.create_timer(timedelta(seconds=hold_seconds))

        ctx.set_custom_status(ANALYZE_STEP)
        analyzed = yield _call_subtask(
            ctx,
            workflow_id,
            task,
            state,
            PipelineStage.ANALYZE,
        )
        state = deserialize_pipeline_state(analyzed["state"])

        ctx.set_custom_status(REPORT_STEP)
        reported = yield _call_subtask(
            ctx,
            workflow_id,
            task,
            state,
            PipelineStage.REPORT,
        )
        state = deserialize_pipeline_state(reported["state"])
    except Exception as exc:
        ctx.set_custom_status("failed")
        yield ctx.call_activity(
            finalize_activity,
            input={
                **terminal_input,
                "status": "failed",
                "error": str(exc),
            },
        )
        raise

    ctx.set_custom_status("completed")
    yield ctx.call_activity(
        finalize_activity,
        input={
            **terminal_input,
            "status": "completed",
            "checkpoint": pipeline_checkpoint_summary(state),
        },
    )
    return {
        "output": state.results[PipelineStage.REPORT.value],
        "state": serialize_pipeline_state(state),
    }
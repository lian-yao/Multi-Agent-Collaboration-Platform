"""模式 B 的编排状态契约：LangGraph 侧负责步骤顺序与状态 Schema。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PipelineStage(StrEnum):
    COLLECT = "collect"
    ANALYZE = "analyze"
    REPORT = "report"


PIPELINE_STEPS: tuple[PipelineStage, ...] = (
    PipelineStage.COLLECT,
    PipelineStage.ANALYZE,
    PipelineStage.REPORT,
)


class PipelineStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineState(BaseModel):
    """一次用户级执行的可序列化编排状态。

    设计说明（doc/dapr-integration.md）：
    - 完整状态用于 Workflow 活动载荷，由 Dapr State Store 持久化；
    - checkpoint 摘要（pipeline_checkpoint_summary）用于 workflow_runs.checkpoint 展示与审计。
    """

    task: str = Field(min_length=1)
    status: PipelineStatus = PipelineStatus.PENDING
    current_step: PipelineStage | None = None
    completed_steps: list[PipelineStage] = Field(default_factory=list)
    results: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_pipeline_state(task: str) -> PipelineState:
    return PipelineState(task=task)


def start(state: PipelineState) -> PipelineState:
    """将 pending 状态置为 running，并选择第一个未完成步骤。"""

    if state.status is not PipelineStatus.PENDING:
        raise ValueError(f"只能从 pending 开始，当前状态是 {state.status.value}")
    return state.model_copy(
        update={
            "status": PipelineStatus.RUNNING,
            "current_step": PIPELINE_STEPS[0],
            "updated_at": _now(),
        }
    )


def complete_step(
    state: PipelineState,
    step: PipelineStage | str,
    result: Any,
) -> PipelineState:
    """记录一个步骤的结果，并按固定顺序推进到下一阶段。"""

    stage = PipelineStage(step) if isinstance(step, str) else step
    if state.status is not PipelineStatus.RUNNING:
        raise ValueError(f"只能在 running 状态下完成步骤，当前状态是 {state.status.value}")
    if stage in state.completed_steps:
        raise ValueError(f"步骤 {stage.value} 已经完成")
    if state.current_step is None or stage != state.current_step:
        current = state.current_step.value if state.current_step else "无"
        raise ValueError(f"当前步骤是 {current}，不能执行 {stage.value}")

    completed_steps = [*state.completed_steps, stage]
    results = {**state.results, stage.value: result}
    remaining = [
        candidate
        for candidate in PIPELINE_STEPS
        if candidate not in completed_steps
    ]
    next_step = remaining[0] if remaining else None
    next_status = (
        PipelineStatus.COMPLETED if next_step is None else PipelineStatus.RUNNING
    )
    return state.model_copy(
        update={
            "status": next_status,
            "current_step": next_step,
            "completed_steps": completed_steps,
            "results": results,
            "updated_at": _now(),
        }
    )


def pause(state: PipelineState) -> PipelineState:
    """将 running 状态置为 paused，保留当前步骤供恢复。"""

    if state.status is not PipelineStatus.RUNNING:
        raise ValueError(
            f"只能从 running 暂停，当前状态是 {state.status.value}"
        )
    return state.model_copy(
        update={
            "status": PipelineStatus.PAUSED,
            "updated_at": _now(),
        }
    )


def resume(state: PipelineState) -> PipelineState:
    """将 paused 状态恢复为 running，并从当前/下一未完成步骤继续。"""

    if state.status is not PipelineStatus.PAUSED:
        raise ValueError(
            f"只能从 paused 恢复，当前状态是 {state.status.value}"
        )
    remaining = [
        step for step in PIPELINE_STEPS if step not in state.completed_steps
    ]
    current_step = state.current_step or (remaining[0] if remaining else None)
    return state.model_copy(
        update={
            "status": PipelineStatus.RUNNING,
            "current_step": current_step,
            "updated_at": _now(),
        }
    )


def fail(state: PipelineState, error: str) -> PipelineState:
    """将未终止的执行置为 failed 并记录失败原因。"""

    if state.status in (PipelineStatus.COMPLETED, PipelineStatus.FAILED):
        raise ValueError(f"终态 {state.status.value} 不能再标记失败")
    return state.model_copy(
        update={
            "status": PipelineStatus.FAILED,
            "error": error,
            "updated_at": _now(),
        }
    )


def serialize_pipeline_state(state: PipelineState) -> dict[str, Any]:
    """输出可跨进程传输/持久化的 JSON 载荷。"""

    return state.model_dump(mode="json")


def deserialize_pipeline_state(
    payload: dict[str, Any] | str | bytes,
) -> PipelineState:
    """从 Workflow 载荷还原 PipelineState。"""

    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return PipelineState.model_validate(payload)


def pipeline_checkpoint_summary(state: PipelineState) -> dict[str, Any]:
    """生成落库到 workflow_runs.checkpoint 的轻量摘要，不包含完整结果。"""

    return {
        "status": state.status.value,
        "current_step": state.current_step.value if state.current_step else None,
        "completed_steps": [step.value for step in state.completed_steps],
        "updated_at": state.updated_at.isoformat(),
    }


def build_step_payload(
    step: PipelineStage | str,
    state: PipelineState,
    attempt: int = 1,
) -> dict[str, Any]:
    """构造单个 Workflow 活动的输入载荷：步骤名 + 完整状态 + 尝试次数。"""

    if attempt < 1:
        raise ValueError(f"attempt 必须大于等于 1，当前是 {attempt}")
    stage = PipelineStage(step) if isinstance(step, str) else step
    return {
        "step": stage.value,
        "state": serialize_pipeline_state(state),
        "attempt": attempt,
    }


def parse_step_payload(
    payload: dict[str, Any],
) -> tuple[PipelineStage, PipelineState, int]:
    """还原 Workflow 活动输入载荷。"""

    return (
        PipelineStage(payload["step"]),
        deserialize_pipeline_state(payload["state"]),
        int(payload["attempt"]),
    )


def build_step_result(state: PipelineState) -> dict[str, Any]:
    """构造单个 Workflow 活动的输出载荷：更新后状态 + checkpoint 摘要。"""

    return {
        "state": serialize_pipeline_state(state),
        "checkpoint": pipeline_checkpoint_summary(state),
    }

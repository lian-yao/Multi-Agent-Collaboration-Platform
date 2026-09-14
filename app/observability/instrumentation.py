"""编排层观测接入点（成员 C D7-8）。

把追踪、指标与标签上下文打包成**一个**上下文管理器，供流水线阶段使用：
进入时设置 `workflow_id/stage/role` 标签并开 Span，退出时按成功/失败记录阶段指标。
接入点只有一处（`app.orchestration.pipeline_graph._run_role_stage`），
因此阶段观测不会散落在编排逻辑里。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 5——追踪 Agent 调用链路，
统计任务完成率与平均执行时间；`doc/api.md` §5.5——指标需关联 `labels.workflow_id`。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from app.observability.context import ObservationContext, observe
from app.observability.metrics import record_stage
from app.observability.tracing import record_exception, span

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
UNKNOWN = "unknown"


@contextmanager
def observed_stage(
    *,
    workflow_id: str | None = None,
    stage: str | None = None,
    role: str | None = None,
    agent_id: str | None = None,
    model: str | None = None,
) -> Iterator[ObservationContext]:
    """阶段级观测：标签上下文 + Span + 阶段指标（成功与异常都会记录）。"""

    started = time.perf_counter()
    with observe(
        workflow_id=workflow_id,
        stage=stage,
        role=role,
        agent_id=agent_id,
        model=model,
    ) as observation:
        with span(
            "stage.run",
            workflow_id=workflow_id,
            stage=stage,
            role=role,
            agent_id=agent_id,
        ) as current:
            try:
                yield observation
            except BaseException as exc:
                record_exception(current, exc)
                _record(stage, role, STATUS_FAILED, started, workflow_id)
                raise
            else:
                _record(stage, role, STATUS_SUCCEEDED, started, workflow_id)


def _record(
    stage: str | None,
    role: str | None,
    status: str,
    started: float,
    workflow_id: str | None,
) -> None:
    record_stage(
        stage or UNKNOWN,
        role or UNKNOWN,
        status,
        round((time.perf_counter() - started) * 1000, 1),
        workflow_id=workflow_id,
    )

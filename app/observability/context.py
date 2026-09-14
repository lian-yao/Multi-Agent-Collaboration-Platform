"""当前执行的观测上下文（成员 C D7-8）。

阶段执行期间，追踪属性与指标标签需要知道「这次调用属于哪个 Workflow / 阶段 / 角色」。
这些信息由调用方通过 `observe()` 写入 contextvar，工具调用与模型回调在执行过程中
读取 `current_context()` 补齐标签——因此工具与回调不需要各自接收一波参数。

对齐事实源：`doc/api.md` §5.5 要求指标关联 `labels.workflow_id`（可选 agent_id/model）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from typing import Iterator

@dataclass(frozen=True)
class ObservationContext:
    """一次阶段执行的观测标签集合。"""

    workflow_id: str | None = None
    stage: str | None = None
    role: str | None = None
    agent_id: str | None = None
    model: str | None = None

    def labels(self) -> dict[str, str]:
        """转成指标标签，省略空值。"""

        return {
            key: str(value)
            for key, value in asdict(self).items()
            if value is not None and str(value) != ""
        }


_CONTEXT: ContextVar[ObservationContext] = ContextVar(
    "macp_observation_context", default=ObservationContext()
)


def current_context() -> ObservationContext:
    return _CONTEXT.get()


def current_labels() -> dict[str, str]:
    return current_context().labels()


@contextmanager
def observe(**fields: str | None) -> Iterator[ObservationContext]:
    """在 with 块内设置观测标签；退出时恢复上一层。"""

    previous = _CONTEXT.get()
    observed = replace(
        previous,
        **{
            key: value
            for key, value in fields.items()
            if key in ObservationContext.__dataclass_fields__
        },
    )
    token = _CONTEXT.set(observed)
    try:
        yield observed
    finally:
        _CONTEXT.reset(token)

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    desc,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.storage import get_session_factory

ALLOWED_WORKFLOW_RUN_STATUSES = frozenset(
    {"pending", "running", "paused", "completed", "failed", "cancelled"}
)

ALLOWED_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_sessions_user_id", "user_id"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_agent_runs_session", "session_id", "created_at"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_messages_session_created", "session_id", "created_at"),)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    instance_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_workflow_runs_agent_run_id"),
        Index("idx_workflow_runs_session", "session_id"),
        Index("idx_workflow_runs_instance", "instance_id"),
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="running", nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_tool_calls_run", "run_id", "created_at"),)


class MetricRecord(Base):
    """`metrics` 表：观测采样落库（`doc/data-model.md` §3）。

    写入方是 `app/observability/metrics.py::PostgresMetricSink`（按列名反射插入），
    读取方是只读接口 `GET /api/v1/metrics`；建表只走 `init_checkpoint_schema()`，
    采样与读取都不建表。
    """

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_metrics_name_time", "metric_name", desc("recorded_at")),
    )


UNSET = object()
"""哨兵：区分「调用方没有传该字段」与「显式传 null 清除覆盖」（`doc/api.md` §5.7、§5.8）。"""

PROVIDER_CONFIG_ID = "default"
"""`provider_configs` 单行表的主键固定值（`doc/data-model.md` §3）。"""


class AgentConfigRecord(Base):
    """`agent_configs` 表：Agent 模型的覆盖配置（`doc/data-model.md` §3）。

    只存**被覆盖的字段**：列值为 NULL 表示回退 API 进程的环境配置。
    写入方是 `PATCH /api/v1/config/agents/{agent_id}`（`doc/api.md` §5.7），
    读取方是 `app/core/agent_config.py::resolve_agent_settings`（API 与阶段活动共用）。
    """

    __tablename__ = "agent_configs"

    agent_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ProviderConfigRecord(Base):
    """`provider_configs` 表：模型 Provider 的运行期覆盖配置（`doc/data-model.md` §3）。

    单行表（`id='default'`），只存**被覆盖的字段**：列值为 NULL 表示回退 API 进程的
    环境配置。写入方是 `PUT /api/v1/config/provider`（`doc/api.md` §5.8），
    读取方是 `app/core/provider_config.py`（API 与阶段活动共用）。

    `api_key` 以明文存储，属于运行期凭据：不回传、不落日志，访问边界由数据库权限与
    部署网络保证（ADR-014、ADR-015）。
    """

    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


def _provider_config_to_dict(row: ProviderConfigRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "api_key": row.api_key,
        "temperature": row.temperature,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def get_provider_config() -> dict[str, Any] | None:
    """返回 Provider 覆盖行；没有写入过返回 None。"""

    with get_session_factory()() as session:
        row = session.get(ProviderConfigRecord, PROVIDER_CONFIG_ID)
        return _provider_config_to_dict(row) if row else None


def upsert_provider_config(
    *,
    provider: Any = UNSET,
    model: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    temperature: Any = UNSET,
    updated_by: str | None = None,
) -> dict[str, Any]:
    """写入 Provider 覆盖值；未传的字段保持原值，显式传 None 表示清除该字段。"""

    with get_session_factory()() as session:
        row = session.get(ProviderConfigRecord, PROVIDER_CONFIG_ID)
        if row is None:
            row = ProviderConfigRecord(id=PROVIDER_CONFIG_ID)
            session.add(row)
        if provider is not UNSET:
            row.provider = provider
        if model is not UNSET:
            row.model = model
        if base_url is not UNSET:
            row.base_url = base_url
        if api_key is not UNSET:
            row.api_key = api_key
        if temperature is not UNSET:
            row.temperature = temperature
        if updated_by is not None:
            row.updated_by = updated_by
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _provider_config_to_dict(row)


def _agent_config_to_dict(row: AgentConfigRecord) -> dict[str, Any]:
    return {
        "agent_id": row.agent_id,
        "model": row.model,
        "temperature": row.temperature,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def get_agent_config(agent_id: str) -> dict[str, Any] | None:
    """按角色返回覆盖配置；没有覆盖行返回 None。"""

    with get_session_factory()() as session:
        row = session.get(AgentConfigRecord, agent_id)
        return _agent_config_to_dict(row) if row else None


def list_agent_configs() -> list[dict[str, Any]]:
    """返回全部覆盖行（角色数固定，不分页）。"""

    with get_session_factory()() as session:
        rows = session.scalars(select(AgentConfigRecord)).all()
        return [_agent_config_to_dict(row) for row in rows]


def upsert_agent_config(
    agent_id: str,
    *,
    model: Any = UNSET,
    temperature: Any = UNSET,
    updated_by: str | None = None,
) -> dict[str, Any]:
    """写入覆盖值；未传的字段保持原值，显式传 None 表示清除该字段的覆盖。"""

    with get_session_factory()() as session:
        row = session.get(AgentConfigRecord, agent_id)
        if row is None:
            row = AgentConfigRecord(agent_id=agent_id)
            session.add(row)
        if model is not UNSET:
            row.model = model
        if temperature is not UNSET:
            row.temperature = temperature
        if updated_by is not None:
            row.updated_by = updated_by
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _agent_config_to_dict(row)


def _tool_call_to_dict(row: ToolCall) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "workflow_run_id": (
            str(row.workflow_run_id) if row.workflow_run_id else None
        ),
        "tool_name": row.tool_name,
        "input": row.input,
        "output": row.output,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }

def _row_to_dict(row: WorkflowRun) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id) if row.session_id else None,
        "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
        "instance_id": row.instance_id,
        "status": row.status,
        "checkpoint": row.checkpoint,
        "current_step": row.current_step,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
    }


def _session_to_dict(row: SessionRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_id": row.user_id,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _agent_run_to_dict(row: AgentRun) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _message_to_dict(row: Message) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "role": row.role,
        "content": row.content,
        "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
        "status": row.status,
        "created_at": row.created_at,
    }


def create_session(user_id: str | None = None) -> dict[str, Any]:
    row = SessionRecord(id=uuid.uuid4(), user_id=user_id, status="active")
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def get_session(session_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(SessionRecord, _as_uuid(session_id))
        return _session_to_dict(row) if row else None


def update_session_status(session_id: str | uuid.UUID, status: str) -> dict[str, Any]:
    if status not in {"active", "paused"}:
        raise ValueError(f"unknown session status: {status}")
    with get_session_factory()() as session:
        row = session.get(SessionRecord, _as_uuid(session_id))
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def create_agent_run(
    session_id: str | uuid.UUID,
    *,
    agent_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = AgentRun(
        id=uuid.uuid4(),
        session_id=_as_uuid(session_id),
        agent_id=_as_uuid(agent_id) if agent_id else None,
        status="queued",
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _agent_run_to_dict(row)


def update_agent_run_status(
    agent_run_id: str | uuid.UUID,
    status: str,
) -> dict[str, Any]:
    if status not in {"queued", "running", "paused", "completed", "failed"}:
        raise ValueError(f"unknown agent run status: {status}")
    with get_session_factory()() as session:
        row = session.get(AgentRun, _as_uuid(agent_run_id))
        if row is None:
            raise KeyError(f"agent run not found: {agent_run_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _agent_run_to_dict(row)


def update_message_status(
    message_id: str | uuid.UUID,
    status: str,
) -> dict[str, Any]:
    if status not in {"queued", "running", "completed", "failed"}:
        raise ValueError(f"unknown message status: {status}")
    with get_session_factory()() as session:
        row = session.get(Message, _as_uuid(message_id))
        if row is None:
            raise KeyError(f"message not found: {message_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _message_to_dict(row)


def create_message(
    session_id: str | uuid.UUID,
    *,
    content: str,
    role: str = "user",
    status: str = "queued",
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = Message(
        id=uuid.uuid4(),
        session_id=_as_uuid(session_id),
        role=role,
        content=content,
        status=status,
        agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _message_to_dict(row)


def upsert_message(
    session_id: str | uuid.UUID,
    *,
    message_id: str | uuid.UUID,
    content: str,
    role: str,
    status: str = "queued",
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """按固定 ID 写入消息，重复调用返回已有记录。

    Dapr 活动允许重放，因此终态写入的报告消息必须幂等：ID 由调用方按
    Workflow 派生，这里用 ``ON CONFLICT DO NOTHING`` 保证同一执行只留一条
    （见 ADR-008）。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    message_uuid = _as_uuid(message_id)
    with get_session_factory()() as session:
        session.execute(
            pg_insert(Message)
            .values(
                id=message_uuid,
                session_id=_as_uuid(session_id),
                role=role,
                content=content,
                status=status,
                agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()
        row = session.get(Message, message_uuid)
        if row is None:
            raise KeyError(f"message upsert failed: {message_id}")
        return _message_to_dict(row)


def list_messages(
    session_id: str | uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    from sqlalchemy import func, select

    session_uuid = _as_uuid(session_id)
    with get_session_factory()() as session:
        total = session.scalar(
            select(func.count()).select_from(Message).where(Message.session_id == session_uuid)
        ) or 0
        rows = session.scalars(
            select(Message)
            .where(Message.session_id == session_uuid)
            .order_by(Message.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return ([_message_to_dict(row) for row in reversed(rows)], int(total))


def get_latest_workflow(
    session_id: str | uuid.UUID,
    *,
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    from sqlalchemy import select

    statement = (
        select(WorkflowRun)
        .where(WorkflowRun.session_id == _as_uuid(session_id))
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    if statuses:
        statement = statement.where(WorkflowRun.status.in_(statuses))
    with get_session_factory()() as session:
        row = session.scalars(statement).first()
        return _row_to_dict(row) if row else None


def get_workflow_run(workflow_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(WorkflowRun, _as_uuid(workflow_id))
        return _row_to_dict(row) if row else None


def init_checkpoint_schema() -> None:
    from app.core.storage import get_engine

    Base.metadata.create_all(get_engine())


def create_workflow_run(
    *,
    workflow_id: str | uuid.UUID,
    session_id: str | uuid.UUID | None = None,
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = WorkflowRun(
        id=_as_uuid(workflow_id),
        session_id=_as_uuid(session_id) if session_id else None,
        agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
        status="pending",
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def _validate_transition(current_status: str, next_status: str | None) -> None:
    if next_status is None or next_status == current_status:
        return
    if current_status not in ALLOWED_TRANSITIONS:
        raise ValueError(f"unknown workflow status: {current_status}")
    if next_status not in ALLOWED_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid workflow status transition: {current_status} -> {next_status}"
        )


def update_workflow_run(
    workflow_id: str | uuid.UUID,
    *,
    status: str | None = None,
    current_step: str | None = None,
    checkpoint: dict[str, Any] | None = None,
    instance_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    workflow_uuid = _as_uuid(workflow_id)
    with get_session_factory()() as session:
        row = session.get(WorkflowRun, workflow_uuid)
        if row is None:
            raise KeyError(f"workflow run not found: {workflow_id}")
        _validate_transition(row.status, status)
        if status is not None:
            row.status = status
        if current_step is not None:
            row.current_step = current_step
        if checkpoint is not None:
            row.checkpoint = checkpoint
        if instance_id is not None:
            row.instance_id = instance_id
        if error is not None:
            row.error = error
        if status in {"completed", "failed", "cancelled"} and row.completed_at is None:
            row.completed_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def get_tool_call(call_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(ToolCall, _as_uuid(call_id))
        return _tool_call_to_dict(row) if row else None


def create_tool_call(
    *,
    call_id: str | uuid.UUID,
    run_id: str | uuid.UUID,
    tool_name: str,
    tool_input: dict[str, Any],
    workflow_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    tool_name = tool_name.strip()
    if not tool_name:
        raise ValueError('tool_name must not be empty')
    call_uuid = _as_uuid(call_id)
    values = {
        "id": call_uuid,
        "run_id": _as_uuid(run_id),
        "workflow_run_id": (
            _as_uuid(workflow_run_id) if workflow_run_id else None
        ),
        "tool_name": tool_name,
        "input": tool_input,
        "status": "running",
    }
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with get_session_factory()() as session:
        session.execute(
            pg_insert(ToolCall)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call upsert failed: {call_id}")
        return _tool_call_to_dict(row)


def mark_tool_call_running(call_id: str | uuid.UUID) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        if row.status == "succeeded":
            return _tool_call_to_dict(row)
        row.status = "running"
        row.output = None
        row.error = None
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def complete_tool_call(
    call_id: str | uuid.UUID,
    *,
    output: Any,
) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        row.status = "succeeded"
        row.output = output
        row.error = None
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def fail_tool_call(
    call_id: str | uuid.UUID,
    *,
    error: str,
) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        row.status = "failed"
        row.output = None
        row.error = error
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def list_tool_calls(
    *,
    run_id: str | uuid.UUID | None = None,
    workflow_run_id: str | uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    statement = select(ToolCall).order_by(ToolCall.created_at.desc()).limit(limit)
    if run_id is not None:
        statement = statement.where(ToolCall.run_id == _as_uuid(run_id))
    if workflow_run_id is not None:
        statement = statement.where(
            ToolCall.workflow_run_id == _as_uuid(workflow_run_id)
        )
    with get_session_factory()() as session:
        rows = session.scalars(statement).all()
        return [_tool_call_to_dict(row) for row in rows]

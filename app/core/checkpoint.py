import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
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

"""API persistence adapters.

The production adapter delegates to the PostgreSQL-backed checkpoint repository.
The in-memory adapter is intentionally small and is used by API tests so they do
not require a running Dapr or PostgreSQL instance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.core import checkpoint


class SqlApiStore:
    def create_session(self, user_id: str | None) -> dict[str, Any]:
        return checkpoint.create_session(user_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return checkpoint.get_session(session_id)

    def update_session_status(self, session_id: str, status: str) -> dict[str, Any]:
        return checkpoint.update_session_status(session_id, status)

    def create_agent_run(self, session_id: str) -> dict[str, Any]:
        return checkpoint.create_agent_run(session_id)

    def update_agent_run_status(self, agent_run_id: str, status: str) -> dict[str, Any]:
        return checkpoint.update_agent_run_status(agent_run_id, status)

    def update_message_status(self, message_id: str, status: str) -> dict[str, Any]:
        return checkpoint.update_message_status(message_id, status)

    def create_message(
        self,
        session_id: str,
        *,
        content: str,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        return checkpoint.create_message(
            session_id,
            content=content,
            agent_run_id=agent_run_id,
        )

    def list_messages(
        self, session_id: str, *, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        return checkpoint.list_messages(session_id, page=page, page_size=page_size)

    def create_workflow(
        self,
        workflow_id: str,
        *,
        session_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        return checkpoint.create_workflow_run(
            workflow_id=workflow_id,
            session_id=session_id,
            agent_run_id=agent_run_id,
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return checkpoint.get_workflow_run(workflow_id)

    def update_workflow(
        self,
        workflow_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        return checkpoint.update_workflow_run(workflow_id, status=status)

    def latest_workflow(
        self,
        session_id: str,
        *,
        statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        return checkpoint.get_latest_workflow(session_id, statuses=statuses)


class InMemoryApiStore:
    """Deterministic store for API tests and local contract checks."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.agent_runs: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.workflows: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def create_session(self, user_id: str | None) -> dict[str, Any]:
        now = self._now()
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self.sessions[row["id"]] = row
        return row.copy()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.sessions.get(session_id)
        return row.copy() if row else None

    def update_session_status(self, session_id: str, status: str) -> dict[str, Any]:
        row = self.sessions[session_id]
        row["status"] = status
        row["updated_at"] = self._now()
        return row.copy()

    def create_agent_run(self, session_id: str) -> dict[str, Any]:
        now = self._now()
        row = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "agent_id": None,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
        }
        self.agent_runs[row["id"]] = row
        return row.copy()

    def update_agent_run_status(self, agent_run_id: str, status: str) -> dict[str, Any]:
        row = self.agent_runs[agent_run_id]
        row["status"] = status
        row["updated_at"] = self._now()
        return row.copy()

    def update_message_status(self, message_id: str, status: str) -> dict[str, Any]:
        row = self.messages[message_id]
        row["status"] = status
        return row.copy()

    def create_message(
        self,
        session_id: str,
        *,
        content: str,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "role": "user",
            "content": content,
            "agent_run_id": agent_run_id,
            "status": "queued",
            "created_at": self._now(),
        }
        self.messages[row["id"]] = row
        return row.copy()

    def list_messages(
        self, session_id: str, *, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [
            row for row in self.messages.values() if row["session_id"] == session_id
        ]
        rows.sort(key=lambda row: row["created_at"])
        total = len(rows)
        start = (page - 1) * page_size
        return [row.copy() for row in rows[start : start + page_size]], total

    def create_workflow(
        self,
        workflow_id: str,
        *,
        session_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        now = self._now()
        row = {
            "id": workflow_id,
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "instance_id": None,
            "status": "pending",
            "checkpoint": None,
            "current_step": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.workflows[workflow_id] = row
        return row.copy()

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self.workflows.get(workflow_id)
        return row.copy() if row else None

    def update_workflow(self, workflow_id: str, *, status: str | None = None) -> dict[str, Any]:
        row = self.workflows[workflow_id]
        if status is not None:
            row["status"] = status
        row["updated_at"] = self._now()
        if status in {"completed", "failed", "cancelled"}:
            row["completed_at"] = row["updated_at"]
        return row.copy()

    def latest_workflow(
        self, session_id: str, *, statuses: set[str] | None = None
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.workflows.values()
            if row["session_id"] == session_id
            and (statuses is None or row["status"] in statuses)
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: row["created_at"]).copy()

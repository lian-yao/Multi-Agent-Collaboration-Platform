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

    def list_sessions(
        self, *, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        return checkpoint.list_sessions(page=page, page_size=page_size)

    def delete_session(self, session_id: str) -> bool:
        return checkpoint.delete_session(session_id)

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

    # —— 附件（ADR-021）——
    # 附件也走这个接缝：否则「上传附件」会成为接口测试里唯一需要真实数据库的路径。
    def create_attachment(self, **fields: Any) -> dict[str, Any]:
        return checkpoint.create_attachment(**fields)

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        return checkpoint.get_attachment(attachment_id)

    def get_attachment_content(self, attachment_id: str) -> dict[str, Any] | None:
        return checkpoint.get_attachment_content(attachment_id)

    def delete_attachment(self, attachment_id: str) -> bool:
        return checkpoint.delete_attachment(attachment_id)

    def link_attachments(
        self,
        attachment_ids: list[str],
        *,
        message_id: str,
        session_id: str,
    ) -> list[str]:
        return checkpoint.link_attachments(
            attachment_ids, message_id=message_id, session_id=session_id
        )

    def list_attachments_for_messages(
        self, message_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return checkpoint.list_attachments_for_messages(message_ids)

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
        self.attachments: dict[str, dict[str, Any]] = {}

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

    def list_sessions(
        self, *, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        # 与 SQL 实现（`checkpoint.list_sessions`）保持同一契约：只列出至少有一条
        # 消息的会话，`total` 同条件过滤（`doc/api.md` §5.13）。
        with_messages = {m["session_id"] for m in self.messages.values()}
        rows = sorted(
            (row for row in self.sessions.values() if row["id"] in with_messages),
            key=lambda row: row["updated_at"],
            reverse=True,
        )
        total = len(rows)
        start = (page - 1) * page_size
        items = []
        for row in rows[start : start + page_size]:
            item = row.copy()
            # 摘要字段：首条用户消息 + 最新 workflow 终态（与 SQL 实现对齐）。
            user_msgs = sorted(
                (
                    m
                    for m in self.messages.values()
                    if m["session_id"] == row["id"] and m["role"] == "user"
                ),
                key=lambda m: m["created_at"],
            )
            if user_msgs:
                content = (user_msgs[0]["content"] or "").strip()
                item["title"] = (
                    content[:60] + ("…" if len(content) > 60 else "")
                ) if content else "（无文本消息）"
            else:
                item["title"] = "（暂无消息）"
            wfs = [
                w
                for w in self.workflows.values()
                if w["session_id"] == row["id"]
            ]
            latest_wf = max(wfs, key=lambda w: w["created_at"]) if wfs else None
            item["latest_workflow_status"] = latest_wf["status"] if latest_wf else None
            item["latest_workflow_id"] = latest_wf["id"] if latest_wf else None
            items.append(item)
        return items, total

    def delete_session(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        # 自底向上清理关联数据，与 SQL 实现对齐。
        run_ids = {
            rid
            for rid, run in self.agent_runs.items()
            if run["session_id"] == session_id
        }
        wf_ids = {
            wid
            for wid, wf in self.workflows.items()
            if wf["session_id"] == session_id
        }
        self.agent_runs = {
            rid: run for rid, run in self.agent_runs.items() if rid not in run_ids
        }
        self.workflows = {
            wid: wf for wid, wf in self.workflows.items() if wid not in wf_ids
        }
        self.messages = {
            mid: msg
            for mid, msg in self.messages.items()
            if msg["session_id"] != session_id
        }
        self.attachments = {
            aid: item
            for aid, item in self.attachments.items()
            if item["session_id"] != session_id
        }
        return True

    # —— 附件（ADR-021）——

    def create_attachment(self, **fields: Any) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "session_id": None,
            "message_id": None,
            "name": fields.get("name") or "附件",
            "mime": fields.get("mime") or "",
            "size_bytes": int(fields.get("size_bytes") or 0),
            "kind": fields.get("kind") or "",
            "status": fields.get("status") or "",
            "error": fields.get("error"),
            "created_at": self._now(),
            "data": fields.get("data"),
            "text_content": fields.get("text_content"),
        }
        self.attachments[row["id"]] = row
        return self._attachment_view(row)

    @staticmethod
    def _attachment_view(row: dict[str, Any]) -> dict[str, Any]:
        """与 SQL 实现同口径：元数据不含字节与正文，只带一个「原件还在不在」。"""

        view = {
            key: value
            for key, value in row.items()
            if key not in {"data", "text_content"}
        }
        view["has_original"] = row.get("data") is not None
        return view

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        row = self.attachments.get(attachment_id)
        return self._attachment_view(row) if row else None

    def get_attachment_content(self, attachment_id: str) -> dict[str, Any] | None:
        row = self.attachments.get(attachment_id)
        return row.copy() if row else None

    def delete_attachment(self, attachment_id: str) -> bool:
        return self.attachments.pop(attachment_id, None) is not None

    def link_attachments(
        self,
        attachment_ids: list[str],
        *,
        message_id: str,
        session_id: str,
    ) -> list[str]:
        linked: list[str] = []
        for value in attachment_ids:
            row = self.attachments.get(value)
            # 与 SQL 实现同口径：只认「尚未归属任何消息」的附件。
            if row is None or row["message_id"] is not None:
                continue
            row["message_id"] = message_id
            row["session_id"] = session_id
            linked.append(value)
        return linked

    def list_attachments_for_messages(
        self, message_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        wanted = set(message_ids)
        grouped: dict[str, list[dict[str, Any]]] = {}
        rows = sorted(self.attachments.values(), key=lambda row: row["created_at"])
        for row in rows:
            if row["message_id"] in wanted:
                grouped.setdefault(row["message_id"], []).append(
                    self._attachment_view(row)
                )
        return grouped

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

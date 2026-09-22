"""工作区审批：登记、决策、消费与过期（ADR-033 §6、`doc/api.md` §5.20）。

存储用替身；核心断言是四条不变量：

1. **一次一授权**——批准只放行「同工作区 + 同动作 + 同目标」的下一次调用；
2. **不覆盖既成决定**——已决策的审批不能再次决策；
3. **过期不放行**——超时未决策置 `expired`，不是自动同意；
4. **不刷屏**——同一个待决策目标反复请求会复用同一条记录。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core import checkpoint
from app.workspace import approvals as approvals_module
from app.workspace.config import WorkspaceSettings
from app.workspace.errors import WorkspaceError


class FakeApprovalStore:
    """复现 `checkpoint` 里审批相关函数的读写语义。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def install(self, monkeypatch) -> "FakeApprovalStore":
        for name in (
            "create_approval",
            "get_approval",
            "list_approvals",
            "find_approval",
            "update_approval",
        ):
            monkeypatch.setattr(checkpoint, name, getattr(self, name))
        return self

    def create_approval(
        self,
        *,
        approval_id,
        kind: str,
        target: str,
        workspace_id=None,
        session_id=None,
        run_id=None,
        reason=None,
        payload=None,
    ) -> dict:
        row = {
            "id": str(approval_id),
            "workspace_id": str(workspace_id) if workspace_id else None,
            "session_id": str(session_id) if session_id else None,
            "run_id": str(run_id) if run_id else None,
            "kind": kind,
            "target": target,
            "reason": reason,
            "payload": dict(payload or {}),
            "status": "pending",
            "decided_by": None,
            "requested_at": datetime.now(timezone.utc),
            "decided_at": None,
        }
        self.rows[row["id"]] = row
        return dict(row)

    def get_approval(self, approval_id) -> dict | None:
        row = self.rows.get(str(approval_id))
        return dict(row) if row else None

    def list_approvals(self, *, session_id=None, status=None) -> list[dict]:
        rows = sorted(self.rows.values(), key=lambda row: (row["requested_at"], row["id"]))
        if session_id is not None:
            rows = [row for row in rows if row["session_id"] == str(session_id)]
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        return [dict(row) for row in rows]

    def find_approval(self, *, workspace_id, kind, target, status) -> dict | None:
        matched = [
            row
            for row in self.rows.values()
            if row["workspace_id"] == str(workspace_id)
            and row["kind"] == kind
            and row["target"] == target
            and row["status"] == status
        ]
        if not matched:
            return None
        latest = max(matched, key=lambda row: row["requested_at"])
        return dict(latest)

    def update_approval(self, approval_id, *, status: str, decided_by=None) -> dict | None:
        row = self.rows.get(str(approval_id))
        if row is None:
            return None
        row["status"] = status
        if decided_by is not None:
            row["decided_by"] = decided_by
        if status != "pending":
            row["decided_at"] = datetime.now(timezone.utc)
        return dict(row)


@pytest.fixture
def store(monkeypatch) -> FakeApprovalStore:
    return FakeApprovalStore().install(monkeypatch)


@pytest.fixture
def settings() -> WorkspaceSettings:
    return WorkspaceSettings(_env_file=None, approval_ttl_seconds=900)


WORKSPACE_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())


def _request(settings, *, kind="overwrite", target="a.txt", session_id=SESSION_ID):
    return approvals_module.request_or_reuse(
        workspace_id=WORKSPACE_ID,
        session_id=session_id,
        kind=kind,
        target=target,
        reason="覆盖已有文件",
        settings=settings,
    )


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #


def test_request_creates_a_pending_approval(store, settings):
    row = _request(settings)

    assert row["status"] == "pending"
    assert row["kind"] == "overwrite"
    assert row["target"] == "a.txt"
    assert row["workspace_id"] == WORKSPACE_ID
    assert row["session_id"] == SESSION_ID


def test_repeated_requests_reuse_the_pending_row(store, settings):
    """模型反复调用同一个破坏性动作，不该把审批列表刷满。"""

    first = _request(settings)
    second = _request(settings)

    assert first["id"] == second["id"]
    assert len(store.rows) == 1


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #


def test_approve_then_consume_releases_exactly_one_call(store, settings):
    row = _request(settings)

    decided = approvals_module.decide(row["id"], decision="approved", actor="ui")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "ui"

    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="overwrite", target="a.txt"
    ) is True
    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="overwrite", target="a.txt"
    ) is False, "批准只放行一次"
    assert store.get_approval(row["id"])["status"] == "consumed"


def test_denied_approval_does_not_release_anything(store, settings):
    row = _request(settings)
    approvals_module.decide(row["id"], decision="denied", actor="ui")

    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="overwrite", target="a.txt"
    ) is False


def test_approval_is_scoped_to_workspace_action_and_target(store, settings):
    row = _request(settings)
    approvals_module.decide(row["id"], decision="approved")

    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="overwrite", target="b.txt"
    ) is False
    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="delete", target="a.txt"
    ) is False
    assert approvals_module.consume(
        workspace_id=str(uuid.uuid4()), kind="overwrite", target="a.txt"
    ) is False


def test_deciding_twice_is_rejected(store, settings):
    row = _request(settings)
    approvals_module.decide(row["id"], decision="approved")

    with pytest.raises(approvals_module.ApprovalNotPendingError):
        approvals_module.decide(row["id"], decision="denied")


def test_deciding_a_missing_approval_is_not_found(store):
    with pytest.raises(approvals_module.ApprovalNotFoundError):
        approvals_module.decide("missing", decision="approved")


# --------------------------------------------------------------------------- #
# 过期与列表
# --------------------------------------------------------------------------- #


def test_stale_pending_expires_instead_of_being_released(store, settings):
    row = _request(settings)
    store.rows[row["id"]]["requested_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=settings.approval_ttl_seconds + 1
    )

    assert approvals_module.expire_stale(settings=settings) == 1
    assert store.get_approval(row["id"])["status"] == "expired"
    assert approvals_module.consume(
        workspace_id=WORKSPACE_ID, kind="overwrite", target="a.txt"
    ) is False


def test_fresh_pending_is_not_expired(store, settings):
    _request(settings)

    assert approvals_module.expire_stale(settings=settings) == 0


def test_list_reports_pending_count_and_filters(store, settings):
    first = _request(settings, target="a.txt")
    _request(settings, target="b.txt")
    approvals_module.decide(first["id"], decision="approved")

    listing = approvals_module.list_session_approvals(SESSION_ID, settings=settings)
    assert listing["total"] == 2
    assert listing["pending"] == 1

    only_pending = approvals_module.list_session_approvals(
        SESSION_ID, status="pending", settings=settings
    )
    assert [item["target"] for item in only_pending["items"]] == ["b.txt"]


def test_list_rejects_an_unknown_status(store, settings):
    with pytest.raises(WorkspaceError, match="status 取值无效"):
        approvals_module.list_session_approvals(
            SESSION_ID, status="whatever", settings=settings
        )

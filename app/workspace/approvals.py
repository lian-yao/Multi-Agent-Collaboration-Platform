"""工作区破坏性动作的人工审批（ADR-033 §6、`doc/api.md` §5.20）。

流程（阶段 3 实现口径，见 ADR-033 的修订说明）：

1. 破坏性动作（覆盖已有文件、删除、覆盖式移动）命中时，Agent 侧拿到一个
   **非重试**失败，文案里带审批 id——它不该自己重试，而是把这件事交给用户；
2. 用户在 Web UI / API 上批准或拒绝（`POST /api/v1/approvals/{id}/decision`）；
3. 批准后，**同工作区、同动作、同目标**的下一次调用放行一次，并把该审批置 `consumed`。

为什么不做工作流级挂起：挂起需要让阶段活动在中途停下来等外部事件（Dapr 外部事件 +
恢复语义），那是编排层的改动，而审批的价值在于"人看过之后才放行"。当前实现把
放行条件收敛成"一次一授权、目标精确匹配"，既不改变正在运行的 Workflow，
也不会出现"批准一次、以后都能覆盖"的长期放行。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core import checkpoint
from app.observability.logging import get_logger, log_event
from app.workspace.config import WorkspaceSettings, get_workspace_settings
from app.workspace.errors import WorkspaceError

logger = get_logger("workspace.approvals")

APPROVAL_KINDS = ("overwrite", "delete")
"""目前需要审批的动作：覆盖已有内容、删除。"""

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CONSUMED = "consumed"

ALL_STATUSES = (PENDING, APPROVED, DENIED, EXPIRED, CONSUMED)


class ApprovalNotFoundError(WorkspaceError):
    """审批记录不存在。"""

    code = "APPROVAL_NOT_FOUND"

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"审批记录不存在：{approval_id}")
        self.approval_id = approval_id


class ApprovalNotPendingError(WorkspaceError):
    """审批已被决策过（或已过期）——不覆盖既成的决定。"""

    code = "APPROVAL_NOT_PENDING"


def _settings(settings: WorkspaceSettings | None) -> WorkspaceSettings:
    return settings or get_workspace_settings()


def request_or_reuse(
    *,
    workspace_id: str,
    session_id: str | None,
    kind: str,
    target: str,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """取一条可用的审批：已批准的优先，其次是仍待决策的；都没有才新建。

    复用而不是每次新建，是为了让"模型反复调用同一个破坏性动作"不会把审批列表刷满。
    """

    resolved = _settings(settings)
    for status in (APPROVED, PENDING):
        existing = checkpoint.find_approval(
            workspace_id=workspace_id, kind=kind, target=target, status=status
        )
        if existing is not None:
            return existing

    row = checkpoint.create_approval(
        approval_id=uuid.uuid4(),
        workspace_id=workspace_id,
        session_id=session_id,
        kind=kind,
        target=target,
        reason=reason,
        payload=payload or {},
    )
    log_event(
        logger,
        "workspace.approval_requested",
        approval_id=row["id"],
        workspace_id=workspace_id,
        session_id=session_id,
        kind=kind,
        target=target,
        ttl_seconds=resolved.approval_ttl_seconds,
    )
    return row


def consume(*, workspace_id: str, kind: str, target: str) -> bool:
    """拿一条"已批准且未消费"的审批并标记为已消费；没有则返回 `False`。"""

    approved = checkpoint.find_approval(
        workspace_id=workspace_id, kind=kind, target=target, status=APPROVED
    )
    if approved is None:
        return False
    checkpoint.update_approval(approved["id"], status=CONSUMED)
    log_event(
        logger,
        "workspace.approval_consumed",
        approval_id=approved["id"],
        workspace_id=workspace_id,
        kind=kind,
        target=target,
    )
    return True


def decide(
    approval_id: str,
    *,
    decision: str,
    actor: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """批准或拒绝一次审批；只有 `pending` 可以决策。"""

    row = checkpoint.get_approval(approval_id)
    if row is None:
        raise ApprovalNotFoundError(approval_id)
    if row["status"] != PENDING:
        raise ApprovalNotPendingError(
            f"审批 {approval_id} 已经是 {row['status']}，不能再次决策"
        )
    status = APPROVED if decision == "approved" else DENIED
    updated = checkpoint.update_approval(approval_id, status=status, decided_by=actor)
    if updated is None:  # pragma: no cover - 并发删除
        raise ApprovalNotFoundError(approval_id)
    log_event(
        logger,
        "workspace.approval_decided",
        approval_id=approval_id,
        kind=row["kind"],
        target=row["target"],
        decision=status,
        actor=actor,
    )
    return _view(updated, settings=settings)


def list_session_approvals(
    session_id: str,
    *,
    status: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """列出会话的审批记录；读取时顺手把超时的 `pending` 标成 `expired`。"""

    resolved = _settings(settings)
    expire_stale(session_id=session_id, settings=resolved)
    statuses = _expand_statuses(status)
    rows: list[dict[str, Any]] = []
    if statuses is None:
        rows = checkpoint.list_approvals(session_id=session_id)
    else:
        seen: dict[str, dict[str, Any]] = {}
        for item in statuses:
            for row in checkpoint.list_approvals(session_id=session_id, status=item):
                seen[row["id"]] = row
        rows = sorted(seen.values(), key=lambda row: str(row["requested_at"]))
    items = [_view(row, settings=resolved) for row in rows]
    pending = sum(1 for item in items if item["status"] == PENDING)
    return {"items": items, "total": len(items), "pending": pending}


def expire_stale(
    *, session_id: str | None = None, settings: WorkspaceSettings | None = None
) -> int:
    """把超过 TTL 仍未决策的审批标成 `expired`（不放行、也不删记录）。"""

    resolved = _settings(settings)
    deadline = datetime.now(timezone.utc) - timedelta(
        seconds=resolved.approval_ttl_seconds
    )
    expired = 0
    for row in checkpoint.list_approvals(session_id=session_id, status=PENDING):
        requested_at = row.get("requested_at")
        if requested_at is None or requested_at > deadline:
            continue
        checkpoint.update_approval(row["id"], status=EXPIRED)
        expired += 1
        log_event(
            logger,
            "workspace.approval_expired",
            approval_id=row["id"],
            kind=row["kind"],
            target=row["target"],
        )
    return expired


def _expand_statuses(status: str | None) -> list[str] | None:
    if status is None or status == "all":
        return None
    if status not in ALL_STATUSES:
        raise WorkspaceError(
            f"status 取值无效：{status}（可选 all / {' / '.join(ALL_STATUSES)}）"
        )
    return [status]


def _view(row: dict[str, Any], *, settings: WorkspaceSettings | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row.get("workspace_id"),
        "session_id": row.get("session_id"),
        "run_id": row.get("run_id"),
        "kind": row["kind"],
        "target": row["target"],
        "reason": row.get("reason"),
        "status": row["status"],
        "payload": row.get("payload") or {},
        "decided_by": row.get("decided_by"),
        "requested_at": row.get("requested_at"),
        "decided_at": row.get("decided_at"),
    }


__all__ = [
    "ALL_STATUSES",
    "APPROVAL_KINDS",
    "APPROVED",
    "CONSUMED",
    "DENIED",
    "EXPIRED",
    "PENDING",
    "ApprovalNotFoundError",
    "ApprovalNotPendingError",
    "consume",
    "decide",
    "expire_stale",
    "list_session_approvals",
    "request_or_reuse",
]

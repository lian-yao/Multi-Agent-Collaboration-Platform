import { ShieldQuestion } from "lucide-react";
import type { Approval, ApprovalDecision, ApprovalStatus } from "../types/api";

/**
 * 工作区审批卡片（`doc/api.md` §5.20 / §7.1、ADR-033 §6）。
 *
 * 三件必须对齐服务端语义、否则会误导用户的事：
 *
 * 1. **`pending` 不是失败**：Agent 那边的 `409 WORKSPACE_APPROVAL_REQUIRED` 是流程的
 *    第二段——它按设计停下来等人。这里把它渲染成「需要你确认」，而不是错误。
 * 2. **批准 ≠ 以后都行**：服务端是「一次一授权」，批准后放行**一次**、记录置 `consumed`，
 *    而且需要 Agent **重试同一调用**。文案要写清这两点，否则用户会以为已经执行完了。
 * 3. **不给假开关**：`allowAutoExecution` 至今没有消费方，界面里不出现。
 */

const KIND_LABEL: Record<Approval["kind"], string> = {
  overwrite: "覆盖",
  delete: "删除",
};

export const APPROVAL_STATUS_TEXT: Record<ApprovalStatus, string> = {
  pending: "待你决定",
  approved: "已允许——Agent 重试同一调用时才会执行",
  denied: "已拒绝",
  expired: "已过期，未放行",
  consumed: "已放行过一次",
};

export function describeApproval(approval: Approval): string {
  return `${KIND_LABEL[approval.kind]} ${approval.target || "/"}`;
}

export function ApprovalCard({
  approvals,
  busy = false,
  error = "",
  onDecide,
}: {
  approvals: Approval[];
  busy?: boolean;
  error?: string;
  onDecide?: (id: string, decision: ApprovalDecision) => void;
}) {
  if (!approvals.length && !error) return null;
  const pending = approvals.filter((item) => item.status === "pending").length;

  return (
    <section className="approval-card" aria-label="工作区审批">
      <header className="approval-head">
        <ShieldQuestion size={16} />
        <b>需要你确认</b>
        <span className="approval-count">
          {pending ? `${pending} 项待决定` : "没有待决定项"}
        </span>
      </header>

      {error && <p className="approval-error">{error}</p>}

      <ul className="approval-list">
        {approvals.map((approval) => (
          <li key={approval.id} className={`approval-item ${approval.status}`}>
            <div className="approval-main">
              <b>{describeApproval(approval)}</b>
              {approval.reason && <p className="approval-reason">{approval.reason}</p>}
              <p className="approval-status">{APPROVAL_STATUS_TEXT[approval.status]}</p>
            </div>
            {approval.status === "pending" && (
              <div className="approval-actions">
                <button
                  type="button"
                  className="approval-allow"
                  disabled={busy}
                  onClick={() => onDecide?.(approval.id, "approved")}
                >
                  允许一次
                </button>
                <button
                  type="button"
                  className="approval-deny"
                  disabled={busy}
                  onClick={() => onDecide?.(approval.id, "denied")}
                >
                  拒绝
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      <p className="approval-hint">
        一次授权只放行一次：批准后 Agent 需要重试同一调用才会执行，之后再动同一个目标要重新申请。
      </p>
    </section>
  );
}

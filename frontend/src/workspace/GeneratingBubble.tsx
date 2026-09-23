import { Bot } from "lucide-react";

/**
 * 答复正文落库前的占位气泡。
 *
 * ## 为什么需要它
 *
 * 助手正文是工作流走到**终态**时一次性落库的（`app/workflows/pipeline.py::finalize_activity`
 * → `upsert_message`，见 ADR-031 §3），前端靠轮询拿。在那之前对话流末尾一个「文字位」
 * 都没有——读的人只看到一行灰字在转，然后整段正文一次冒出来。这里先把那个位置占住，
 * 正文到达时 `reportIndex` 转正、真气泡原地替换掉它，衔接上看不出切换。
 *
 * ## 它不宣称的事
 *
 * 文案说「正在生成答复」，不说「正在接收」——**后端没有事件流**，逐 token 的说法在这里
 * 是假的（ADR-031 §3 那条边界）。这个气泡只说明「这一步还在跑」，不说明粒度。
 *
 * ## 判据（`placeholderNote` 是唯一实现，冒烟直接渲染它来断言）
 *
 * | 情形 | 是否显示 | 文案 |
 * | --- | --- | --- |
 * | 执行中（含刚提交、排队中） | 显示 | 正在生成答复… |
 * | 已完成、但报告正文还没被轮询拉回来 | 显示 | 正在整理答复… |
 * | 有审批挂起 | **不显示** | —— |
 * | 失败 / 取消 / 已有正文 | **不显示** | —— |
 *
 * 审批那一行是这张表里最容易写错的：`pending` 不是失败，流程是**按设计停住等人**
 * （`workspace/ApprovalCard.tsx` 的文件头注释）。此时说「正在生成」会把「等你决策」
 * 误报成「正在跑」，用户就不知道该去点审批卡片。
 */

export type PlaceholderInput = {
  /** 本次执行的 workflow 状态；还没有 workflow（历史会话刚打开）传 `null`。 */
  workflowStatus: string | null;
  /** 是否正在跑：提交中 / `running` / `pending`。 */
  busy: boolean;
  /** 这次执行的报告正文是否已经到达（`reportIndex >= 0`）。 */
  hasReport: boolean;
  /** 待决策的审批条数。 */
  pendingApprovals: number;
};

/** 占位气泡该不该出现、写什么。返回 `null` = 不显示。 */
export function placeholderNote({
  workflowStatus,
  busy,
  hasReport,
  pendingApprovals,
}: PlaceholderInput): string | null {
  if (hasReport) return null;
  if (pendingApprovals > 0) return null;
  if (busy) return "正在生成答复…";
  // 状态先落、消息后写的那个间隙：正文马上就到，别让末尾空着。
  if (workflowStatus === "completed") return "正在整理答复…";
  return null;
}

export function GeneratingBubble(props: PlaceholderInput) {
  const note = placeholderNote(props);
  if (!note) return null;
  return (
    <article className="message-bubble assistant generating-bubble">
      <div className="message-avatar">
        <Bot size={17} />
      </div>
      <div className="message-body">
        <div className="message-meta">
          <b>Agent 团队</b>
          {/* 占的是普通消息里时间（`.message-meta time`）的那个位置，同档灰字。
              位置或字号对不上，替换成真气泡时头部会跳一下。 */}
          <span className="generating-label">生成中</span>
        </div>
        {/* 借 `.md-body.is-streaming` 只为拿正文行尾那支光标（`styles.css`），
            不另写一套动画——两支光标必须长得一样，否则切换时看得出来。 */}
        <div className="md-body is-streaming">
          <p>{note}</p>
        </div>
      </div>
    </article>
  );
}

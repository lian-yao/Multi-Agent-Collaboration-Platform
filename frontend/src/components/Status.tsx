/**
 * 状态文案与状态胶囊。
 *
 * 全站唯一的 `statusText` 来源：原先散在 `App.tsx` 与 `Inspection.tsx`，
 * 两处措辞一旦漂移就会出现「等待 / 排队中」并存的观感问题。
 */

export const statusText: Record<string, string> = {
  pending: "排队中",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

/** 工具调用状态：与 workflow 状态是两套词汇，不要复用。 */
export const toolCallStatusText: Record<string, string> = {
  running: "执行中",
  succeeded: "成功",
  failed: "失败",
};

export function Status({ status }: { status: string }) {
  return (
    <span className={`status-pill ${status}`}>
      <i />
      {status === "idle"
        ? "待命"
        : status === "pending"
          ? "等待"
          : (statusText[status] ?? status)}
    </span>
  );
}

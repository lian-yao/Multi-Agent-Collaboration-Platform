/**
 * 可展开 / 收起的折叠块。
 *
 * ## 为什么不用 `<details>`
 *
 * 原生 `<details>` 的展开状态**不受控**：父组件无法「运行中默认展开、跑完自动收起」，
 * 也无法在切换消息时重置。执行过程正需要这种控制（正在跑的那一步要自动摊开，跑完的
 * 收成一行），所以这里用受控组件——开关状态在调用方手里。
 *
 * ## 外观是纯文本行，不画框
 *
 * 唯一使用方是对话流的执行过程（`workspace/RunActivity.tsx`），那里要的是网页 AI
 * 思维链那种「一行灰字、点开才铺开」的观感；套上边框与底色会把它从消息里割出来。
 * 层级靠缩进与一条左侧细线表达。原先的 `tone`（左侧竖条按状态着色）随之删除——
 * 状态改由调用方在摘要行里用状态文字承担。
 *
 * ## 可读性口径
 *
 * - 摘要行本身就是**按钮**，`aria-expanded` 跟着状态走，键盘可达（原生 `button`）。
 * - `aria-controls` 只在展开时指向真实存在的元素；收起时**不写**——指向一个不存在的
 *   id 会让人以为内容只是被隐藏了。
 * - 折叠不等于「可以省掉信息」：跑完的步骤收起后，摘要行仍须写清楚
 *   **谁 / 什么阶段 / 动了几次工具 / 什么状态**，否则收起就成了信息丢失。
 */
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

export function Disclosure({
  id,
  title,
  glyph,
  meta,
  open,
  onToggle,
  children,
}: {
  /** 与 `aria-controls` 配对的稳定 id。 */
  id: string;
  /** 摘要行主文案。 */
  title: ReactNode;
  /** 摘要行最前面的图标（角色图标 / 工具图标）。 */
  glyph?: ReactNode;
  /** 摘要行右侧的补充信息（阶段名、工具次数、状态）。 */
  meta?: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={`disclosure${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="disclosure-head"
        aria-expanded={open}
        {...(open ? { "aria-controls": id } : {})}
        onClick={onToggle}
      >
        <ChevronRight size={13} className="disclosure-chevron" aria-hidden="true" />
        {glyph ? <span className="disclosure-glyph">{glyph}</span> : null}
        <span className="disclosure-title">{title}</span>
        {meta ? <span className="disclosure-meta">{meta}</span> : null}
      </button>
      {open ? (
        <div className="disclosure-body" id={id}>
          {children}
        </div>
      ) : null}
    </div>
  );
}

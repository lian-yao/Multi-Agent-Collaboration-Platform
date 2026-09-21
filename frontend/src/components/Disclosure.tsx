/**
 * 可展开 / 收起的折叠块。
 *
 * ## 为什么不用 `<details>`
 *
 * 原生 `<details>` 的展开状态**不受控**：父组件无法「运行中默认展开、跑完自动收起」，
 * 也无法在切换消息时重置。执行轨迹正需要这种控制（正在跑的那一步要自动摊开，跑完的
 * 收成一行），所以这里用受控组件——开关状态在调用方手里。
 *
 * ## 可读性口径
 *
 * - 摘要行本身就是**按钮**，`aria-expanded` 跟着状态走，键盘可达（原生 `button`）。
 * - `aria-controls` 只在展开时指向真实存在的元素；收起时**不写**——指向一个不存在的
 *   id 会让人以为内容只是被隐藏了。
 * - 折叠不等于「可以省掉信息」：跑完的步骤收起后，摘要行仍须写清楚
 *   **谁 / 什么状态 / 动了几次工具**，否则收起就成了信息丢失。
 */
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

export function Disclosure({
  id,
  title,
  glyph,
  meta,
  tone,
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
  /** 摘要行右侧的补充信息（状态胶囊、计数）。 */
  meta?: ReactNode;
  /** 左侧竖条与图标底色：`green` / `rose` / `amber` / `accent`。 */
  tone?: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={`disclosure${open ? " is-open" : ""}${tone ? ` tone-${tone}` : ""}`}>
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

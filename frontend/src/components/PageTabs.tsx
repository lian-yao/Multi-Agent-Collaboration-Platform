import { useRef, type ComponentType, type KeyboardEvent } from "react";
import "./page-tabs.css";

export type TabIcon = ComponentType<{ size?: number | string; className?: string }>;

export type PageTab<T extends string> = {
  id: T;
  /** 已本地化的短标签；页面级分区里也用它做次级说明。 */
  label: string;
  icon?: TabIcon;
  /** 悬停提示，可放该分区的完整释义。 */
  title?: string;
};

export type PageTabsProps<T extends string> = {
  tabs: readonly PageTab<T>[];
  active: T;
  onChange: (id: T) => void;
  /**
   * `page`（默认）用于页面级副路由：单独一行、靠左起排，宽度贴合内容，
   * 与 `.page-heading` 的左置版式对齐。
   * `inline` 用于卡片内的次级切换，取消外框与背景，嵌进工具栏。
   */
  variant?: "page" | "inline";
  label?: string;
};

/**
 * 全站统一的副路由控件（`role="tablist"`）。
 *
 * 关键约束：**只占内容宽度**（`width: fit-content`）。整页居中曾经把标题、
 * 分区与卡片挤成一条中轴线，这里用 `fit-content` + 父级左对齐彻底规避。
 */
export function PageTabs<T extends string>({
  tabs,
  active,
  onChange,
  variant = "page",
  label = "页面分区",
}: PageTabsProps<T>) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  /** ←/→ 在分区之间移动焦点并按「随焦切换」的惯例直接切换分区。 */
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = tabs[(index + step + tabs.length) % tabs.length];
    onChange(next.id);
    refs.current[next.id]?.focus();
  };

  return (
    <nav
      className={`ui-tabs${variant === "inline" ? " ui-tabs-inline" : ""}`}
      role="tablist"
      aria-label={label}
      aria-orientation="horizontal"
    >
      {tabs.map((tab, index) => {
        const Icon = tab.icon;
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(node) => {
              refs.current[tab.id] = node;
            }}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            title={tab.title ?? tab.label}
            className={`ui-tab${selected ? " active" : ""}`}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
          >
            {Icon && <Icon size={14} />}
            <span>{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

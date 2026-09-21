import { useEffect, useId, type ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* 文案与格式化                                                                */
/* -------------------------------------------------------------------------- */

/** 把任意抛出的东西转成一句可读文案：`ApiError` 已经带好后端 message。 */
export function describeError(cause: unknown, fallback = "操作失败，请稍后重试"): string {
  if (cause instanceof Error && cause.message) return cause.message;
  if (typeof cause === "string" && cause) return cause;
  return fallback;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

/**
 * 历史记录、消息气泡、大纲用的紧凑时间戳：**必带日期**。
 *
 * 原来这些位置只显示 `HH:mm`，于是「昨天的 14:12」和「今天的 14:12」在界面上完全
 * 一样——历史会话列表因此看不出哪条是新的。反过来，今天的记录再重复一遍完整日期
 * 也没有信息量，所以按远近分三档：今天 / 昨天 / 「M月D日」，跨年才补年份。
 *
 * `now` 可注入：冒烟测试必须能固定「今天是哪天」，否则断言会跟着系统时钟漂。
 */
export function formatStamp(value: string | null | undefined, now: Date = new Date()): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;

  const clock = `${String(parsed.getHours()).padStart(2, "0")}:${String(parsed.getMinutes()).padStart(2, "0")}`;
  const sameDay = (left: Date, right: Date) =>
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate();

  if (sameDay(parsed, now)) return `今天 ${clock}`;
  // 用日期构造而不是减 24 小时：后者在夏令时切换那天会偏一天。
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(parsed, yesterday)) return `昨天 ${clock}`;
  if (parsed.getFullYear() === now.getFullYear()) {
    return `${parsed.getMonth() + 1}月${parsed.getDate()}日 ${clock}`;
  }
  return `${parsed.getFullYear()}年${parsed.getMonth() + 1}月${parsed.getDate()}日 ${clock}`;
}

/** 卡片里只展示一小段描述，完整内容收进折叠区。 */
export function truncate(text: string, max = 96): string {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length > max ? `${compact.slice(0, max - 1)}…` : compact;
}

/** `KEY=VALUE` 多行文本 ⇄ 字符串映射；两边都接受 `说明` 注释行与空行。 */
export function parsePairs(text: string): Record<string, string> | null {
  const result: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator <= 0) return null;
    const key = line.slice(0, separator).trim();
    if (!key) return null;
    result[key] = line.slice(separator + 1).trim();
  }
  return result;
}

export function formatPairs(pairs: Record<string, string> | null | undefined): string {
  if (!pairs) return "";
  return Object.entries(pairs)
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

export function parseLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** 数字输入框的统一解析：空串 → `null`（= 清除该字段），非法 → `undefined`。 */
export function parseNumber(raw: string, min: number, max?: number): number | null | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return undefined;
  if (parsed < min) return undefined;
  if (max !== undefined && parsed > max) return undefined;
  return parsed;
}

export function parseInteger(raw: string, min = 1): number | null | undefined {
  const parsed = parseNumber(raw, min);
  if (parsed === null || parsed === undefined) return parsed;
  return Number.isInteger(parsed) ? parsed : undefined;
}

export function describeIntervalError(raw: string, label: string, min: number, max?: number): string {
  if (!raw.trim()) return "";
  const parsed = parseNumber(raw, min, max);
  if (parsed === undefined) {
    return `${label} 需要是 ${min}${max === undefined ? " 以上" : `–${max}`} 之间的数字。`;
  }
  return "";
}

/* -------------------------------------------------------------------------- */
/* 基础控件                                                                    */
/* -------------------------------------------------------------------------- */

export type NoticeTone = "ok" | "bad" | "info";
export type NoticeState = { tone: NoticeTone; text: string } | null;

export function NoticeBar({ notice, id }: { notice: NoticeState; id?: string }) {
  return (
    <p className={`cfg-notice ${notice ? notice.tone : "idle"}`} role="status" aria-live="polite" id={id}>
      {notice ? notice.text : "\u00a0"}
    </p>
  );
}

export function Field({
  label,
  hint,
  children,
  wide = false,
  htmlFor,
  tone,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  wide?: boolean;
  htmlFor?: string;
  tone?: "bad" | undefined;
}) {
  return (
    <div className={`cfg-field${wide ? " cfg-field-wide" : ""}`}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint !== undefined && <small className={tone === "bad" ? "bad" : undefined}>{hint}</small>}
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  disabled = false,
  label,
  title,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={title ?? label}
      className={`cfg-switch${checked ? " on" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <i />
    </button>
  );
}

export function Chip({
  children,
  tone = "slate",
  title,
}: {
  children: ReactNode;
  tone?: string;
  title?: string;
}) {
  return (
    <span className={`cfg-chip tint-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function Monogram({ text, tint, size = 34 }: { text: string; tint?: string | null; size?: number }) {
  return (
    <span
      className={`cfg-monogram tint-${tint ?? "slate"}`}
      style={{ width: size, height: size, fontSize: Math.max(9, size * 0.32) }}
      aria-hidden="true"
    >
      {text}
    </span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="cfg-empty">
      <b>{title}</b>
      {hint && <span>{hint}</span>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 弹层                                                                        */
/* -------------------------------------------------------------------------- */

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  wide = false,
  labelledBy,
}: {
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  labelledBy?: string;
}) {
  const titleId = useId();
  const headingId = labelledBy ?? titleId;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="cfg-modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className={`cfg-modal${wide ? " cfg-modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="cfg-modal-head">
          <div>
            <h2 id={headingId}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button type="button" className="cfg-icon-button" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>
        <div className="cfg-modal-body">{children}</div>
        {footer && <footer className="cfg-modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 覆盖（override）语义：省略 = 不改动，null = 清除                            */
/* -------------------------------------------------------------------------- */

/**
 * 只把**变化过的**字段写进 patch。
 *
 * 后端 PATCH 用 `model_fields_set` 区分「未提供」与「显式 `null`」，因此前端必须在
 * JSON 里省略未改动的键（`JSON.stringify` 会丢弃 `undefined` 的值），
 * 而显式清除写 `null`（= 回退下一层配置 / 回到未设置）。
 */
export function assignIfChanged<T>(patch: Record<string, unknown>, key: string, next: T, current: T): void {
  if (!Object.is(next, current)) patch[key] = next;
}

/** 数组按元素比较：避免每次保存都因为新数组引用而误判为「改过」。 */
export function sameStrings(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export function sameParameters(
  left: readonly { key: string; value: string; type: string }[],
  right: readonly { key: string; value: string; type: string }[],
): boolean {
  return (
    left.length === right.length &&
    left.every(
      (item, index) =>
        item.key === right[index].key &&
        item.value === right[index].value &&
        item.type === right[index].type,
    )
  );
}

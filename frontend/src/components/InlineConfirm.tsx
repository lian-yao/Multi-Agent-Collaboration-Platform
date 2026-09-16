import { useCallback, useEffect, useRef, useState, type FocusEvent, type KeyboardEvent, type ReactNode } from "react";
import "./inline-confirm.css";

export type InlineConfirmProps = {
  /** 动作名，用于 aria-label 与默认确认文案，如「删除会话」。 */
  label: string;
  /** 确认按钮文案，默认「确认」；文案位宽的地方可写「确认删除」。 */
  confirmLabel?: string;
  /** 触发按钮的内容（图标或文字）。 */
  children: ReactNode;
  /** 触发按钮的类名：沿用各区域既有样式（`cfg-quiet danger` / `record-run-delete` …）。 */
  triggerClassName?: string;
  /** 触发按钮的 aria-label。 */
  triggerLabel: string;
  triggerTitle?: string;
  disabled?: boolean;
  /** armed 之后显示在按钮前的短问句；窄行可省略，细节交给 triggerTitle。 */
  question?: string;
  /** 自动收回时间（毫秒），0 表示不自动收回。 */
  autoCancelMs?: number;
  /** `sm` 用于 26–28px 的图标位，`md`（默认）用于文本按钮。 */
  size?: "sm" | "md";
  /** 承载外壳的附加类名（例如绝对定位到卡片角上的 slot）。 */
  slotClassName?: string;
  onConfirm: () => void;
};

/**
 * 危险操作的行内二次确认：点一次进入 armed 态，就地变成「确认 / 取消」，再点确认才执行。
 *
 * 存在的理由：**不用原生 `window.confirm`**。原生对话框由宿主提供，内置预览的
 * sandbox iframe（没有 `allow-modals`）、浏览器勾选「阻止此页面创建更多对话框」、
 * Electron/CEF 外壳都会直接屏蔽它——被屏蔽时 `confirm()` 不弹窗、立即返回 `false`，
 * 把删除挂在返回值上的写法就会静默失效（表现为「点了删除没反应」）。
 * 行内确认是普通 DOM，不可能被屏蔽，也不需要额外的浮层。
 *
 * 交互约定：armed 后焦点自动落到确认按钮；`Esc`、焦点移出、超时（默认 6s）都会收回，
 * 避免停留在「待确认」状态里误触。
 */
export function InlineConfirm({
  label,
  confirmLabel = "确认",
  children,
  triggerClassName,
  triggerLabel,
  triggerTitle,
  disabled = false,
  question,
  autoCancelMs = 6000,
  size = "md",
  slotClassName,
  onConfirm,
}: InlineConfirmProps) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | null>(null);
  const yesRef = useRef<HTMLButtonElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => clearTimer, [clearTimer]);

  const arm = () => {
    setArmed(true);
    clearTimer();
    if (autoCancelMs > 0) {
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setArmed(false);
      }, autoCancelMs);
    }
  };

  const cancel = useCallback(() => {
    clearTimer();
    setArmed(false);
  }, [clearTimer]);

  // 进入 armed 态后把焦点移到确认按钮：键盘用户不必再找，也让 focus-within 保持可见。
  useEffect(() => {
    if (armed) yesRef.current?.focus();
  }, [armed]);

  const confirm = () => {
    clearTimer();
    setArmed(false);
    onConfirm();
  };

  /** 焦点移出整组才收回；组内两个按钮之间切换不算离开。 */
  const onBlur = (event: FocusEvent<HTMLSpanElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    cancel();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLSpanElement>) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    cancel();
    triggerRef.current?.focus();
  };

  return (
    <span className={`ui-confirm-slot${slotClassName ? ` ${slotClassName}` : ""}`}>
      {armed ? (
        <span
          className={`ui-confirm${size === "sm" ? " ui-confirm-sm" : ""}`}
          role="group"
          aria-label={`${label}：待确认`}
          onClick={(event) => event.stopPropagation()}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
        >
          {question ? <span className="ui-confirm-q">{question}</span> : null}
          <button
            type="button"
            ref={yesRef}
            className="ui-confirm-yes"
            onClick={confirm}
            disabled={disabled}
          >
            {confirmLabel}
          </button>
          <button type="button" className="ui-confirm-no" onClick={cancel}>
            取消
          </button>
        </span>
      ) : (
        <button
          type="button"
          ref={triggerRef}
          className={triggerClassName}
          aria-label={triggerLabel}
          title={triggerTitle}
          disabled={disabled}
          onClick={(event) => {
            // 行内的删除入口常嵌在可点击行/卡片里，触发与确认都不应冒泡成「打开」。
            event.stopPropagation();
            arm();
          }}
        >
          {children}
        </button>
      )}
    </span>
  );
}

export type InlineConfirmBarProps = {
  question: string;
  confirmLabel?: string;
  disabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

/**
 * 已经处在「需要用户再点一次」的状态时直接渲染的确认条（无触发按钮）。
 *
 * 用于后端先拒绝、再由用户显式升级的场景（如 `PROVIDER_IN_USE` 后的强制级联删除）：
 * 第一跳由后端 409 触发，不需要再套一层 armed 循环，但视觉与 `InlineConfirm` 保持一致。
 */
export function InlineConfirmBar({
  question,
  confirmLabel = "确认",
  disabled = false,
  onConfirm,
  onCancel,
}: InlineConfirmBarProps) {
  return (
    <span
      className="ui-confirm"
      role="group"
      aria-label={question}
      onClick={(event) => event.stopPropagation()}
    >
      <span className="ui-confirm-q">{question}</span>
      <button type="button" className="ui-confirm-yes" onClick={onConfirm} disabled={disabled}>
        {confirmLabel}
      </button>
      <button type="button" className="ui-confirm-no" onClick={onCancel}>
        取消
      </button>
    </span>
  );
}

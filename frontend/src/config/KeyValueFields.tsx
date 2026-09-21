import { useRef, type ReactNode } from "react";

/**
 * 键值对行编辑器。
 *
 * 用于 MCP Server 的环境变量与请求头：这两处都是「一组 KEY=VALUE」，
 * 用多行文本域表达时，用户得记住分隔符规则，删掉一行也只能整段重打。
 * 逐行成对输入则可以直接增删、逐项看到自己填了什么。
 *
 * 校验口径由调用方决定（见 `mcpConfig.ts::draftProblem` 与 `McpPanel` 的 `serverProblem`）：
 * 本组件只负责取值与编辑，不判断合法性。
 */

export type KeyValueEntry = { id: string; key: string; value: string };

/** 映射 → 行；`prefix` 只用于生成稳定的 React key 前缀。 */
export function recordToEntries(record: Record<string, string> | null | undefined, prefix: string): KeyValueEntry[] {
  return Object.entries(record ?? {}).map(([key, value], index) => ({
    id: `${prefix}-${index}`,
    key,
    value,
  }));
}

/**
 * 行 → 映射；返回 `null` 表示有「填了值但没写键」或「键重复」的行。
 *
 * 键与值都为空的行按「尚未填写」忽略——用户点了「添加」还没填是常态，
 * 不该因此拦住保存。
 */
export function entriesToRecord(entries: readonly KeyValueEntry[]): Record<string, string> | null {
  const result: Record<string, string> = {};
  for (const entry of entries) {
    const key = entry.key.trim();
    if (!key) {
      if (entry.value.trim()) return null;
      continue;
    }
    if (key in result) return null;
    result[key] = entry.value;
  }
  return result;
}

/** 两个映射是否逐项相等（用于「只提交改动字段」的 patch 判定）。 */
export function samePairs(
  left: Record<string, string> | null | undefined,
  right: Record<string, string> | null | undefined,
): boolean {
  const a = left ?? {};
  const b = right ?? {};
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((key) => a[key] === b[key]);
}

export function KeyValueFields({
  title,
  hint,
  entries,
  onChange,
  disabled = false,
  addLabel,
  keyPlaceholder,
  valuePlaceholder,
  required = false,
}: {
  title: string;
  hint?: ReactNode;
  entries: KeyValueEntry[];
  onChange: (entries: KeyValueEntry[]) => void;
  disabled?: boolean;
  addLabel: string;
  keyPlaceholder: string;
  valuePlaceholder: string;
  required?: boolean;
}) {
  const counter = useRef(0);
  const idPrefix = useRef(`kv-${Math.random().toString(36).slice(2, 8)}`).current;

  const create = (): KeyValueEntry => {
    counter.current += 1;
    return { id: `${idPrefix}-new-${counter.current}`, key: "", value: "" };
  };

  const update = (id: string, patch: Partial<KeyValueEntry>) =>
    onChange(entries.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry)));

  return (
    <div className="cfg-kv">
      <div className="cfg-kv-head">
        <div>
          <span className="cfg-kv-title">
            {title}
            {required && <span className="cfg-kv-required">必填</span>}
          </span>
          {hint && <p className="cfg-kv-hint">{hint}</p>}
        </div>
        <button type="button" className="cfg-quiet" onClick={() => onChange([...entries, create()])} disabled={disabled}>
          {addLabel}
        </button>
      </div>

      {entries.length === 0 && <p className="cfg-kv-empty">尚未添加。留空则不发送任何 {title}。</p>}

      {entries.map((entry) => (
        <div className="cfg-kv-row" key={entry.id}>
          <input
            value={entry.key}
            onChange={(event) => update(entry.id, { key: event.target.value })}
            placeholder={keyPlaceholder}
            aria-label={`${title} 键`}
            autoComplete="off"
            spellCheck={false}
            disabled={disabled}
          />
          <span className="cfg-kv-eq" aria-hidden="true">
            =
          </span>
          <input
            value={entry.value}
            onChange={(event) => update(entry.id, { value: event.target.value })}
            placeholder={valuePlaceholder}
            aria-label={`${title} 值`}
            autoComplete="off"
            spellCheck={false}
            disabled={disabled}
          />
          <button
            type="button"
            className="cfg-kv-remove"
            onClick={() => onChange(entries.filter((candidate) => candidate.id !== entry.id))}
            aria-label={`移除 ${entry.key || title} 这一项`}
            title="移除这一项"
            disabled={disabled}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

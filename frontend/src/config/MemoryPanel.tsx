import { useCallback, useEffect, useState } from "react";
import { Brain } from "lucide-react";
import { ApiError, api } from "../api/client";
import { InlineConfirm } from "../components/InlineConfirm";
import type { LongTermMemoryEntry } from "../types/api";
import {
  Chip,
  EmptyState,
  NoticeBar,
  describeError,
  formatTime,
  truncate,
  type NoticeState,
} from "./shared";

/**
 * 长期记忆面板（`doc/api.md` §5.22、ADR-036）。
 *
 * 它回答的是「平台到底记住我什么了」——长期记忆**跨会话、无 TTL**，所以这个"看见"的入口
 * 是必需品，不是装饰；删除则跟着同一条记录走，免得使用者在面板上看着某条却要切到对话里
 * 打一句 `忘记：…` 才能收回。
 *
 * **写入刻意不出现在这里**：一条记忆是"使用者说出来的话"，只能通过在对话里说 `记住：…`
 * 产生。给面板加一个"新增"按钮，等于让记忆可以凭空造出来，也就没有"我什么时候说过这句"
 * 可查了。
 *
 * 单用户场景：记忆按**使用者**存一份（保留 id `user`），所有 Agent 与规划节点读同一份，
 * 所以这里不分角色、不做筛选。
 */

/** 纯 props 驱动的展示体，便于 `frontend/rendercheck` 直接挂载断言。 */
export function MemoryBoundary({
  items,
  loading = false,
  error = "",
  busy = false,
  notice,
  onDelete,
  onReload,
}: {
  items: LongTermMemoryEntry[];
  loading?: boolean;
  error?: string;
  busy?: boolean;
  notice?: NoticeState;
  onDelete?: (entry: LongTermMemoryEntry) => void;
  onReload?: () => void;
}) {
  return (
    <section className="cfg-block">
      <div className="cfg-block-head">
        <div className="cfg-block-title">
          <Brain size={16} />
          <div>
            <h3>长期记忆</h3>
            <p>
              平台跨会话记住的偏好与信息，所有 Agent 共用同一份。新增只能在对话里说
              「记住：…」——记忆是你说过的话，不由界面凭空造；这里负责**看见**与**收回**。
            </p>
          </div>
        </div>
        <div className="cfg-row-actions">
          <Chip tone="slate">{items.length} 条</Chip>
          {onReload && (
            <button type="button" className="cfg-quiet" onClick={onReload} disabled={loading}>
              重新读取
            </button>
          )}
        </div>
      </div>

      <NoticeBar notice={notice ?? null} />

      {error && (
        <p role="alert" className="cfg-alert">
          {error}{" "}
          {onReload && (
            <button type="button" className="cfg-quiet" onClick={onReload}>
              重试
            </button>
          )}
        </p>
      )}
      {!error && loading && <p className="cfg-hint">读取中…</p>}

      {!error && !loading && items.length === 0 && (
        <EmptyState
          title="还没有长期记忆"
          hint="在对话里说一句「记住：…」（可带名称，如「记住 称呼：叫我张三」），它就会出现在这里，并在之后每次执行时提供给 Agent。"
        />
      )}

      {!error && !loading && items.length > 0 && (
        <ul className="cfg-memory-list">
          {items.map((entry) => (
            <li key={entry.key}>
              <div className="cfg-memory-item">
                <b title={entry.key}>{entry.key}</b>
                <p title={entry.content}>{truncate(entry.content, 160)}</p>
                <span className="cfg-hint">更新于 {formatTime(entry.updated_at)}</span>
              </div>
              <InlineConfirm
                label="忘记"
                triggerLabel="忘记"
                triggerClassName="cfg-quiet"
                question={`删除「${truncate(entry.key, 24)}」？之后 Agent 不再记得这条。`}
                disabled={busy}
                onConfirm={() => onDelete?.(entry)}
              >
                忘记
              </InlineConfirm>
            </li>
          ))}
        </ul>
      )}

      <p className="cfg-hint">
        删除只影响长期记忆，不会删掉任何会话与消息；已在对话里说过的内容仍在「任务记录」里。
      </p>
    </section>
  );
}

/** 容器：负责取数与动作，展示交给 `MemoryBoundary`。 */
export function MemoryPanel() {
  const [items, setItems] = useState<LongTermMemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const listing = await api.listLongTermMemory();
      setItems(listing.items);
      setError("");
    } catch (cause) {
      setItems([]);
      setError(describeError(cause, "长期记忆读取失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const remove = async (entry: LongTermMemoryEntry) => {
    setBusy(true);
    try {
      await api.deleteLongTermMemory(entry.key);
      setNotice({ tone: "ok", text: `已忘记：${truncate(entry.key, 40)}` });
      await reload();
    } catch (cause) {
      // 「已经不在了」不是故障：多半是刚在对话里用 `忘记：` 删过，或另一个窗口删过。
      // 这种情况直接刷新，让使用者看到最新状态，而不是读一条吓人的错误。
      // 按**错误码**判而不是匹配文案——文案会改，码是契约（`doc/api.md` §5.22）。
      if (cause instanceof ApiError && cause.code === "MEMORY_ENTRY_NOT_FOUND") {
        setNotice({ tone: "info", text: "这条已经不在了，已为你刷新列表。" });
        await reload();
      } else {
        setNotice({ tone: "bad", text: describeError(cause, "删除失败") });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <MemoryBoundary
      items={items}
      loading={loading}
      error={error}
      busy={busy}
      notice={notice}
      onDelete={(entry) => void remove(entry)}
      onReload={() => void reload()}
    />
  );
}

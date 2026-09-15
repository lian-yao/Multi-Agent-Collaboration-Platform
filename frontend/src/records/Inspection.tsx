import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Wrench, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { Chip, EmptyState, formatTime } from "../config/shared";
import { toolCallStatusText } from "../components/Status";
import type { DataPage, Metric, ToolCall, Workflow } from "../types/api";
// cfg-* 三级层次是所有观测类卡片的共同底座，这里显式引入以免依赖加载顺序。
import "../config/config.css";
import "./records.css";

/* -------------------------------------------------------------------------- */
/* 通用记录容器：五种状态 + 按需分页                                            */
/* -------------------------------------------------------------------------- */

export type RecordsProps<T> = {
  title: string;
  /** 标题右侧的一句话释义，说明这份记录的口径。 */
  hint?: ReactNode;
  load: (page: number) => Promise<DataPage<T>>;
  render: (item: T, index: number) => ReactNode;
  /** 任务仍在进行时才轮询；终态只读一次。 */
  poll?: boolean;
  /** 变化即重新从第一页拉取（通常传 `workflow.updated_at`）。 */
  refreshKey?: string;
  empty?: { title: string; hint?: ReactNode };
};

/**
 * 记录列表容器。
 *
 * 状态口径固定为五种，避免每个调用点各写一套：
 * 加载中 → 读取失败 → 数据源未接入 → 暂无记录 → 有数据。
 * 分页只在 `pages > 1` 时出现，单页记录不再占一行高度。
 */
export function Records<T>({
  title,
  hint,
  load,
  render,
  poll = false,
  refreshKey = "",
  empty,
}: RecordsProps<T>) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DataPage<T> | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setError("");

    async function read() {
      try {
        const result = await load(page);
        if (!live) return;
        setData(result);
        setError("");
      } catch (cause) {
        if (live) setError(cause instanceof Error ? cause.message : "读取失败");
      } finally {
        if (live && poll) timer = setTimeout(read, 2000);
      }
    }

    void read();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [load, page, revision, poll, refreshKey]);

  // 数据源换了（切工作流）就回到第一页，否则会停在一个不存在的页码上。
  useEffect(() => {
    setPage(1);
  }, [refreshKey]);

  const pages = data ? Math.max(1, Math.ceil(data.total / Math.max(1, data.page_size))) : 1;
  const busy = poll && !error;

  return (
    <section className="cfg-block record-block" aria-label={title}>
      <div className="cfg-block-head record-head">
        <div>
          <h3>
            {title}
            {data && <span className="cfg-count">{data.total} 条</span>}
          </h3>
          {hint && <p>{hint}</p>}
        </div>
        <button
          type="button"
          className="cfg-quiet"
          onClick={() => setRevision((value) => value + 1)}
          disabled={!data && !error}
        >
          <RefreshCw size={13} className={busy ? "spin" : undefined} />
          刷新
        </button>
      </div>

      {error && (
        <p role="alert" className="cfg-alert">
          {error}（下方如有内容为上次读取结果）
        </p>
      )}
      {!data && !error && <p className="cfg-hint">加载中…</p>}

      {data?.availability === "not_integrated" && (
        <p className="cfg-hint">数据源未接入：后端尚未提供这份记录。</p>
      )}

      {data?.availability === "available" && !data.items.length && (
        <EmptyState
          title={empty?.title ?? "暂无记录"}
          hint={empty?.hint ?? "产生新的执行后，记录会出现在这里。"}
        />
      )}

      {data && data.items.length > 0 && (
        <div className="record-list">{data.items.map(render)}</div>
      )}

      {data && pages > 1 && (
        <div className="record-pager">
          <button type="button" className="cfg-quiet" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            <ChevronLeft size={13} />
            上一页
          </button>
          <span>
            第 {page} / {pages} 页
          </span>
          <button
            type="button"
            className="cfg-quiet"
            disabled={page >= pages}
            onClick={() => setPage(page + 1)}
          >
            下一页
            <ChevronRight size={13} />
          </button>
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 行渲染器                                                                    */
/* -------------------------------------------------------------------------- */

const METRIC_NAMES: Record<string, string> = {
  input_tokens: "输入 Token",
  output_tokens: "输出 Token",
  total_tokens: "总 Token",
};

const labelText = (value: unknown): string =>
  typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);

/** 指标采样行：原值展示，不做累加（doc/api.md §5.6）。 */
export function metricRow(metric: Metric, index: number) {
  const labels = Object.entries(metric.labels ?? {});
  return (
    <article className="record-row record-metric" key={metric.id ?? index}>
      <div className="record-metric-head">
        <b>{METRIC_NAMES[metric.metric_name] ?? metric.metric_name}</b>
        <strong>{metric.value.toLocaleString("zh-CN")}</strong>
      </div>
      <div className="record-meta">
        <span className="record-label">{formatTime(metric.recorded_at)}</span>
        {labels.map(([key, value]) => (
          <span className="record-label" title={`${key}: ${labelText(value)}`} key={key}>
            {key} = {labelText(value)}
          </span>
        ))}
      </div>
    </article>
  );
}

/** 工具调用行：成功态收起输入输出，失败态直接把 error 顶出来。 */
export function toolCallRow(call: ToolCall, index: number) {
  const tone = call.status === "succeeded" ? "green" : call.status === "failed" ? "rose" : "amber";
  return (
    <article className="record-row" key={call.id ?? index}>
      <div className="record-row-main">
        <Wrench size={14} />
        <b>{call.tool_name}</b>
        <Chip tone={tone}>{toolCallStatusText[call.status] ?? call.status}</Chip>
        <span className="record-row-time">{formatTime(call.updated_at)}</span>
      </div>
      {call.error && (
        <p role="alert" className="cfg-alert">
          {call.error}
        </p>
      )}
      <details className="cfg-tool-schema">
        <summary>输入与输出</summary>
        <pre>{JSON.stringify({ input: call.input, output: call.output }, null, 2)}</pre>
      </details>
    </article>
  );
}

/* -------------------------------------------------------------------------- */
/* 面向 Workflow 的两个导出                                                     */
/* -------------------------------------------------------------------------- */

/** 本次任务的工具调用链路。 */
export function ToolCallRecords({ workflow }: { workflow: Workflow }) {
  const load = useCallback((page: number) => api.getToolCalls(workflow.id, page), [workflow.id]);
  const poll = !["completed", "failed", "cancelled"].includes(workflow.status);
  return (
    <Records
      title="工具调用链路"
      hint="按调用顺序记录工具入参、出参与失败原因。"
      load={load}
      poll={poll}
      refreshKey={workflow.updated_at}
      render={toolCallRow}
      empty={{ title: "本次任务没有工具调用", hint: "任务未触发任何工具时不会产生记录。" }}
    />
  );
}

/** 指标采样：传了 workflow 就是本次任务，没传就是全局。 */
export function RuntimeSampling({ workflow }: { workflow: Workflow | null }) {
  const workflowId = workflow?.id ?? null;
  const load = useCallback(
    (page: number) => (workflowId ? api.getMetrics(page, workflowId) : api.getMetrics(page)),
    [workflowId],
  );
  const poll = workflow !== null && !["completed", "failed", "cancelled"].includes(workflow.status);
  return (
    <Records
      title={workflowId ? `本次任务指标采样 · ${workflowId.slice(0, 8)}` : "全局指标采样"}
      hint="按采样原值展示，缺少 Token 记录表示暂无采样，不计为 0；不同采样不累加。"
      load={load}
      poll={poll}
      refreshKey={workflow?.updated_at ?? ""}
      render={metricRow}
      empty={{ title: "暂无指标采样", hint: "任务执行后才有 Token 与耗时采样。" }}
    />
  );
}

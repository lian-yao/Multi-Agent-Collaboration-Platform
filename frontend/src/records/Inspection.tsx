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
  /** 计数单位。归集后的列表数的不是「条采样」而是「项指标」，用默认的「条」会说谎。 */
  unit?: string;
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
  unit = "条",
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
            {data && (
              <span className="cfg-count">
                {data.total} {unit}
              </span>
            )}
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
  tool_call_duration_ms: "工具调用耗时",
  stage_duration_ms: "阶段耗时",
  workflow_duration_ms: "任务总耗时",
};

const labelText = (value: unknown): string =>
  typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);

/**
 * 量纲。必须分开，否则 Token 数（几百上千）与毫秒数（几十几百）会被画进同一条
 * 曲线的同一把尺子上，「耗时从 80ms 涨到 120ms」会被 Token 的量级压成一条直线。
 */
type MetricKind = "token" | "duration" | "count";

const METRIC_KINDS: Record<string, MetricKind> = {
  input_tokens: "token",
  output_tokens: "token",
  total_tokens: "token",
  tool_call_duration_ms: "duration",
  stage_duration_ms: "duration",
  workflow_duration_ms: "duration",
};

function metricKind(name: string): MetricKind {
  const known = METRIC_KINDS[name];
  if (known) return known;
  // 认不出来的名字按后缀收敛，而不是一律当计数：后端以后加一个新指标，
  // 界面至少还能把它分到同类量纲里，而不是把毫秒和 Token 混在一张图上。
  if (name.endsWith("_ms")) return "duration";
  if (name.endsWith("_tokens")) return "token";
  return "count";
}

const KIND_LABEL: Record<MetricKind, string> = { token: "Token", duration: "耗时", count: "计数" };
const KIND_TONE: Record<MetricKind, string> = { token: "blue", duration: "amber", count: "slate" };

/** 数值单位；加起来只有一个词，所以放在数值后面而不是塞进数值里。 */
const kindUnit = (kind: MetricKind): string => (kind === "duration" ? "ms" : "");

const metricTitle = (name: string): string => METRIC_NAMES[name] ?? name;

/** 与采样点一一对应的标签前缀；同前缀才归到同一条序列。 */
function labelSignature(labels: Record<string, unknown>): string {
  return Object.entries(labels ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${labelText(value)}`)
    .join("|");
}

/** 归集后的一条指标序列：一个指标名 + 一组标签 = 一行。 */
export interface MetricSeriesGroup {
  key: string;
  metric_name: string;
  labels: Record<string, unknown>;
  /** 按 `recorded_at` **升序**的采样点，火花线左旧右新。 */
  samples: Metric[];
}

/**
 * 按「指标名 + 标签组」归集（`doc/api.md` §5.6）。
 *
 * 只按指标名归集会出错：`stage_duration_ms` 带 `stage` 标签，三个阶段的耗时是三条
 * 互不相干的序列，混成一条会让人以为「耗时忽高忽低」。所以标签一起进 key。
 */
export function groupMetrics(items: readonly Metric[]): MetricSeriesGroup[] {
  const buckets = new Map<string, MetricSeriesGroup>();
  for (const item of items) {
    const labels = item.labels ?? {};
    const key = `${item.metric_name}|${labelSignature(labels)}`;
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.samples.push(item);
    } else {
      buckets.set(key, { key, metric_name: item.metric_name, labels, samples: [item] });
    }
  }
  const groups = [...buckets.values()];
  for (const group of groups) {
    group.samples.sort((left, right) => left.recorded_at.localeCompare(right.recorded_at));
  }
  // 序列之间按「最近一次采样」倒序：刚发生的排前面，与其余记录页一致。
  const latest = (group: MetricSeriesGroup) =>
    group.samples[group.samples.length - 1]?.recorded_at ?? "";
  return groups.sort((left, right) => latest(right).localeCompare(latest(left)));
}

/** 火花线：只表达形状，不标刻度——精确值就在同一行的最新/最小/最大里。 */
function Sparkline({ values, kind }: { values: number[]; kind: MetricKind }) {
  if (values.length < 2) {
    return <span className="record-spark-empty">仅 1 次采样，画不出趋势</span>;
  }
  const width = 132;
  const height = 26;
  const min = Math.min(...values);
  const max = Math.max(...values);
  // 全等时给一个假跨度，否则会除以 0；此时曲线是一条居中的水平线，正好是事实。
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const points = values.map((value, index) => {
    const x = index * step;
    const y = height - 3 - ((value - min) / span) * (height - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const stroke = kind === "duration" ? "#c08a4a" : "#4d7f92";
  return (
    <svg
      className="record-spark"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={`${values.length} 次采样的走势`}
      preserveAspectRatio="none"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={stroke}
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        cx={width}
        cy={Number(points[points.length - 1].split(",")[1])}
        r="2.2"
        fill={stroke}
      />
    </svg>
  );
}

/** 指标采样行：一次采样一行原值，不做累加（doc/api.md §5.6）。 */
export function metricRow(metric: Metric, index: number) {
  const kind = metricKind(metric.metric_name);
  const labels = Object.entries(metric.labels ?? {});
  return (
    <article className="record-row record-metric" key={metric.id ?? index}>
      <div className="record-metric-head">
        <b>{metricTitle(metric.metric_name)}</b>
        <strong>
          {metric.value.toLocaleString("zh-CN")}
          {kindUnit(kind) && <small>{kindUnit(kind)}</small>}
        </strong>
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

/** 归集后的一行：一个指标的一个标签组。 */
export function MetricSeries({ group }: { group: MetricSeriesGroup }) {
  const kind = metricKind(group.metric_name);
  const values = group.samples.map((sample) => sample.value);
  const latest = group.samples[group.samples.length - 1];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const labels = Object.entries(group.labels ?? {});
  const unit = kindUnit(kind);

  return (
    <article className="record-row record-series">
      <div className="record-series-head">
        <div className="record-series-title">
          <b>{metricTitle(group.metric_name)}</b>
          <Chip tone={KIND_TONE[kind]}>{KIND_LABEL[kind]}</Chip>
          <span className="record-label">{group.samples.length} 次采样</span>
        </div>
        <div className="record-series-latest">
          <span className="record-label">最新</span>
          <strong>
            {latest ? latest.value.toLocaleString("zh-CN") : "—"}
            {unit && <small>{unit}</small>}
          </strong>
        </div>
      </div>

      <div className="record-series-body">
        <Sparkline values={values} kind={kind} />
        <dl className="record-series-facts">
          <div>
            <dt>最小</dt>
            <dd>
              {metricValuePair(min, unit)}
            </dd>
          </div>
          <div>
            <dt>最大</dt>
            <dd>
              {metricValuePair(max, unit)}
            </dd>
          </div>
          <div>
            <dt>均值</dt>
            <dd>
              {metricValuePair(Math.round(mean * 100) / 100, unit)}
            </dd>
          </div>
          <div>
            <dt>最近采样</dt>
            <dd>{latest ? formatTime(latest.recorded_at) : "—"}</dd>
          </div>
        </dl>
      </div>

      {labels.length > 0 && (
        <div className="record-meta">
          {labels.map(([key, value]) => (
            <span className="record-label" title={`${key}: ${labelText(value)}`} key={key}>
              {/* `workflow_id` 是整串 UUID，直接铺出来会把一行标签撑爆；短 id 足够对照。 */}
              {key === "workflow_id" && typeof value === "string"
                ? `任务 ${value.slice(0, 8)}`
                : `${key} = ${labelText(value)}`}
            </span>
          ))}
        </div>
      )}

      <details className="cfg-tool-schema">
        <summary>逐次原值（{group.samples.length} 条）</summary>
        <div className="record-list">
          {group.samples.map((sample, index) => metricRow(sample, index))}
        </div>
      </details>
    </article>
  );
}

/** 毫秒值顺带给出秒数：`12,840 ms` 要用户自己心算，`（12.84 s）`不用。 */
function metricValuePair(value: number, unit: string): string {
  if (unit === "ms" && value >= 1000) {
    return `${value.toLocaleString("zh-CN")} ms（${(value / 1000).toFixed(2)} s）`;
  }
  return unit ? `${value.toLocaleString("zh-CN")} ${unit}` : value.toLocaleString("zh-CN");
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

/** `/api/v1/metrics` 的 `page_size` 上限。 */
const METRIC_PAGE_SIZE = 100;
const METRIC_PAGE_LIMIT = 20;

/**
 * 取回**全部**采样，而不是一页。
 *
 * 归集必须先有全量：分页拿 20 条再归集，得到的是「最近 20 条里出现过的指标」，
 * 而且是按采样时间切的——同一条序列会被页边界劈成两半，最小值、均值全都不对，
 * 界面却看不出来哪里不对。宁可多几次请求。
 */
async function collectMetrics(
  workflowId: string | null,
  // 容器会传页码进来，但这里**故意忽略**：归集必须拿全量，见上面的说明。
  _page: number,
): Promise<DataPage<MetricSeriesGroup>> {
  const all: Metric[] = [];
  let availability: DataPage<Metric>["availability"] = "available";
  for (let index = 1; index <= METRIC_PAGE_LIMIT; index += 1) {
    const chunk = workflowId
      ? await api.getMetrics(index, workflowId)
      : await api.getMetrics(index);
    availability = chunk.availability;
    all.push(...chunk.items);
    if (all.length >= chunk.total) break;
  }
  // 分页信息归一成「一页装完」：归集后行数由序列数决定，与采样条数无关。
  const groups = groupMetrics(all);
  return {
    items: groups,
    page: 1,
    page_size: Math.max(1, groups.length),
    total: groups.length,
    availability,
  };
}

/**
 * 指标采样：传了 workflow 就是本次任务，没传就是全局。
 *
 * 展示口径（§5.6）仍然是「采样原值、不累加」——归集只是把同一条序列的多次采样
 * 收进一行，逐次数值原样保留在展开区里，没有做任何求和或差分。
 */
export function RuntimeSampling({ workflow }: { workflow: Workflow | null }) {
  const workflowId = workflow?.id ?? null;
  const load = useCallback(
    (page: number) => collectMetrics(workflowId, page),
    [workflowId],
  );
  const poll = workflow !== null && !["completed", "failed", "cancelled"].includes(workflow.status);
  return (
    <Records<MetricSeriesGroup>
      title={workflowId ? `本次任务指标采样 · ${workflowId.slice(0, 8)}` : "全局指标采样"}
      hint="同指标同标签归成一行时间序列；数值为采样原值，不累加、不做差分。缺少 Token 记录表示暂无采样，不计为 0。"
      unit="项指标"
      load={load}
      poll={poll}
      refreshKey={workflow?.updated_at ?? ""}
      render={(group) => <MetricSeries key={group.key} group={group} />}
      empty={{ title: "暂无指标采样", hint: "任务执行后才有 Token 与耗时采样。" }}
    />
  );
}

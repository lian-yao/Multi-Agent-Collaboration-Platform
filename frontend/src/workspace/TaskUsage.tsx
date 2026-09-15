/**
 * 任务级用量统计。
 *
 * 口径遵循 `doc/api.md` §5.5：**按采样原值展示，不做累加**——分页里可能重复的采样
 * 一旦相加就会虚高，所以同一指标出现多次时并排列出并标注「多次采样」，而不是求和。
 *
 * 逐条采样明细不在这里展开，它属于「任务记录」页（同一份数据不在两个页面各写一遍）。
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowUpRight, Gauge } from "lucide-react";
import { api } from "../api/client";
import type { Metric, Workflow } from "../types/api";
import "./workspace.css";

/** 只展示能读懂的用量指标；其余采样交给「任务记录」页看原值。 */
const METRIC_LABEL: Record<string, string> = {
  input_tokens: "输入 Token",
  output_tokens: "输出 Token",
  total_tokens: "总 Token",
  stage_duration_ms: "阶段耗时",
  workflow_duration_ms: "任务耗时",
};

/** 一行 = 同一作用域下的同一个指标，`values` 是**已格式化**的采样原值。 */
export type UsageRow = {
  label: string;
  values: string[];
};

export type UsageGroup = {
  /** 作用域：优先 `agent_id`，其次 `model`，都没有就是任务级。 */
  scope: string;
  rows: UsageRow[];
};

const format = (metricName: string, value: number) =>
  metricName.endsWith("_duration_ms")
    ? `${(value / 1000).toFixed(1)}s`
    : value.toLocaleString("zh-CN");

function scopeOf(metric: Metric): string {
  const { agent_id: agentId, model } = metric.labels ?? {};
  if (typeof agentId === "string" && agentId) return agentId;
  if (typeof model === "string" && model) return model;
  return "任务级";
}

/** 纯函数：把采样聚成「作用域 → 指标 → 原值列表」，不做任何求和。 */
export function groupUsage(metrics: Metric[]): UsageGroup[] {
  const grouped = new Map<string, Map<string, string[]>>();
  for (const metric of metrics) {
    const label = METRIC_LABEL[metric.metric_name];
    if (!label) continue;
    if (typeof metric.value !== "number" || !Number.isFinite(metric.value)) continue;
    const scope = scopeOf(metric);
    if (!grouped.has(scope)) grouped.set(scope, new Map());
    const rows = grouped.get(scope)!;
    rows.set(label, [...(rows.get(label) ?? []), format(metric.metric_name, metric.value)]);
  }
  return [...grouped].map(([scope, rows]) => ({
    scope,
    rows: [...rows].map(([label, values]) => ({ label, values })),
  }));
}

/** 纯展示层：按显式 props 驱动，可被离屏冒烟直接挂载。 */
export function TaskUsagePanel({
  groups,
  loading,
  error,
  onOpenRecords,
}: {
  groups: UsageGroup[];
  loading: boolean;
  error: string;
  onOpenRecords?: () => void;
}) {
  return (
    <div className="usage-panel">
      {loading && !groups.length && <p className="cfg-hint">加载中…</p>}
      {error && <p className="cfg-hint">{error}</p>}
      {!loading && !error && !groups.length && (
        <p className="cfg-hint">
          <Gauge size={12} /> 暂无用量采样。缺少 Token 记录表示没有采样，不计为 0。
        </p>
      )}
      {groups.map((group) => (
        <div className="usage-group" key={group.scope}>
          <span className="usage-scope">{group.scope}</span>
          <ul>
            {group.rows.map((row) => (
              <li key={row.label}>
                <span>
                  {row.label}
                  {row.values.length > 1 && <em>多次采样</em>}
                </span>
                <strong>{row.values.join(" / ")}</strong>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {onOpenRecords && (
        <button type="button" className="cfg-quiet usage-more" onClick={onOpenRecords}>
          <ArrowUpRight size={13} />
          查看逐条采样与工具调用
        </button>
      )}
    </div>
  );
}

/** 容器层：拉取本次 Workflow 的采样，未到终态时轮询。 */
export function TaskUsage({
  workflow,
  onOpenRecords,
}: {
  workflow: Workflow;
  onOpenRecords?: () => void;
}) {
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const terminal = ["completed", "failed", "cancelled"].includes(workflow.status);

  const read = useCallback(async () => {
    try {
      const page = await api.getMetrics(1, workflow.id);
      setMetrics(page.items);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取用量失败");
    } finally {
      setLoading(false);
    }
  }, [workflow.id]);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    setMetrics([]);
    async function tick() {
      if (!live) return;
      await read();
      if (live && !terminal) timer = setTimeout(() => void tick(), 2000);
    }
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [read, terminal, workflow.updated_at]);

  return (
    <TaskUsagePanel
      groups={groupUsage(metrics)}
      loading={loading}
      error={error}
      onOpenRecords={onOpenRecords}
    />
  );
}

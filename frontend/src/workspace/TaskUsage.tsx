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

/**
 * 采样归到哪个作用域。
 *
 * `agent_id` 优先，但**后端目前打的是 `role`**（`app/observability/metrics.py` 的
 * `_labels` 走上下文，标签集是 `role` / `stage` / `model`）。只认 `agent_id` 的后果是
 * 三个 Agent 的 Token 全都退到 `model` 这一层、被合并成一个「gpt-5.5」分组——
 * 侧栏看起来就是「没有按 Agent 记 Token」。`role` 与 Agent 目录的 `id` 同值
 * （collector / analyst / reporter），所以它是可靠的中文别名。
 */
export function scopeOf(metric: Metric): string {
  const { agent_id: agentId, role, model } = metric.labels ?? {};
  if (typeof agentId === "string" && agentId) return agentId;
  if (typeof role === "string" && role) return role;
  if (typeof model === "string" && model) return model;
  return "任务级";
}

/** 作用域 → 指标名 → 采样原值列表。**只收集不求和**，求和会把重复采样算成虚高。 */
function collectUsage(metrics: Metric[]): Map<string, Map<string, string[]>> {
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
  return grouped;
}

/** 纯函数：把采样聚成「作用域 → 指标 → 原值列表」，不做任何求和。 */
export function groupUsage(metrics: Metric[]): UsageGroup[] {
  return [...collectUsage(metrics)].map(([scope, rows]) => ({
    scope,
    rows: [...rows].map(([label, values]) => ({ label, values })),
  }));
}

/**
 * 单个 Agent 的用量行。协作画布的 Agent 卡片按角色 id 取自己那一份。
 *
 * 取不到就回空数组——**不是 0**：没有采样和用量为零是两件事，前者说明这一步
 * 还没跑到或没开采样，后者才是真的没消耗。卡片据此显示「暂无采样」。
 */
export function usageFor(metrics: Metric[], agentId: string): UsageRow[] {
  const rows = collectUsage(metrics).get(agentId);
  return rows ? [...rows].map(([label, values]) => ({ label, values })) : [];
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

/**
 * 拉取某个 Workflow 的采样，未到终态时轮询。
 *
 * 做成 hook 而不是一个自带容器的组件：侧栏现在有两处要吃这份数据——用量面板与
 * 协作画布的 Agent 卡片。各自拉一次会变成同一个接口两倍请求，而且两处可能落在
 * 不同的采样批次上，卡片和面板显示的 Token 就对不上了。
 */
export function useWorkflowMetrics(workflow: Workflow | null): {
  metrics: Metric[];
  loading: boolean;
  error: string;
} {
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [loading, setLoading] = useState(Boolean(workflow));
  const [error, setError] = useState("");
  const workflowId = workflow?.id ?? null;
  const status = workflow?.status ?? "";
  const updatedAt = workflow?.updated_at ?? "";
  const terminal = ["completed", "failed", "cancelled"].includes(status);

  const read = useCallback(async () => {
    if (!workflowId) return;
    try {
      const page = await api.getMetrics(1, workflowId);
      setMetrics(page.items);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取用量失败");
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (!workflowId) {
      setMetrics([]);
      setLoading(false);
      setError("");
      return;
    }
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
  }, [read, terminal, updatedAt, workflowId]);

  return { metrics, loading, error };
}

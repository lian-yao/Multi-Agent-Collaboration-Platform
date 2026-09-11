import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "./api/client";
import type { DataPage, Metric, Provider, Workflow } from "./types/api";

function Records<T>({ title, load, render, poll = false, refreshKey = "" }: {
  title: string; load: (page: number) => Promise<DataPage<T>>;
  render: (item: T, index: number) => ReactNode; poll?: boolean; refreshKey?: string;
}) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DataPage<T> | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
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
    return () => { live = false; clearTimeout(timer); };
  }, [load, page, revision, poll, refreshKey]);
  return <section className="config-panel inspection-records" aria-label={title}>
    <div className="config-panel-head"><h3>{title}</h3><button type="button" onClick={() => setRevision(v => v + 1)}>刷新</button></div>
    {error && <p role="alert">{error}（请重试；下方如有记录为上次读取结果）</p>}
    {!data && !error && <p role="status">加载中…</p>}
    {data?.availability === "not_integrated" && <p>数据源未接入</p>}
    {data?.availability === "available" && !data.items.length && <p>暂无记录</p>}
    {data?.items.map(render)}
    {data && <div className="inspection-pagination"><button disabled={page <= 1} onClick={() => setPage(v => v - 1)}>上一页</button><span>第 {page} 页 · 共 {data.total} 条</span><button disabled={page * data.page_size >= data.total} onClick={() => setPage(v => v + 1)}>下一页</button></div>}
  </section>;
}

const metricNames: Record<string, string> = { input_tokens: "输入 Token", output_tokens: "输出 Token", total_tokens: "总 Token" };
function metricRow(metric: Metric, index: number) {
  return <article className="inspection-row" key={metric.id ?? index}>
    <b>{metricNames[metric.metric_name] ?? metric.metric_name}</b><strong>{metric.value.toLocaleString("zh-CN")}</strong>
    <small>{new Date(metric.recorded_at).toLocaleString("zh-CN")}</small>
    <pre>{JSON.stringify(metric.labels, null, 2)}</pre>
  </article>;
}

export function WorkflowInspection({ workflow }: { workflow: Workflow }) {
  const calls = useCallback((page: number) => api.getToolCalls(workflow.id, page), [workflow.id]);
  const metrics = useCallback((page: number) => api.getMetrics(page, workflow.id), [workflow.id]);
  const poll = !["completed", "failed", "cancelled"].includes(workflow.status);
  return <div className="inspection-grid">
    <Records title="工具调用链路" load={calls} poll={poll} refreshKey={workflow.updated_at} render={call => <article key={call.id} className="inspection-row">
      <b>{call.tool_name}</b><span>{({running: "执行中", succeeded: "成功", failed: "失败"})[call.status]}</span>
      <small>{new Date(call.created_at).toLocaleString("zh-CN")} → {new Date(call.updated_at).toLocaleString("zh-CN")}</small>
      {call.error && <p role="alert">{call.error}</p>}
      <details><summary>输入与输出</summary><pre>{JSON.stringify({ input: call.input, output: call.output }, null, 2)}</pre></details>
    </article>} />
    <Records title="本次任务 Token 与指标采样" load={metrics} poll={poll} refreshKey={workflow.updated_at} render={metricRow} />
    <p className="config-note">按采样原值展示，缺少 Token 记录表示暂无采样，不计为 0。不同采样不累加。</p>
  </div>;
}

export function RuntimeConfig() {
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    setError("");
    void api.getProviders().then(value => { if (live) setProviders(value); }).catch(cause => { if (live) setError(cause instanceof Error ? cause.message : "配置读取失败"); });
    return () => { live = false; };
  }, [revision]);
  return <div className="config-page">
    <section className="page-heading"><h1>工具与配置</h1><p>当前配置与执行采样</p></section>
    <div className="inspection-grid">
      <section className="config-panel" aria-label="Provider 配置">
        <div className="config-panel-head"><h2>Provider 配置</h2><button onClick={() => setRevision(v => v + 1)}>刷新配置</button></div>
        {error && <p role="alert">{error}</p>}
        {!providers && !error && <p>加载中…</p>}
        {providers?.map(provider => <dl className="config-facts" key={provider.id}>
          <div><dt>Provider</dt><dd>{provider.name}</dd></div>
          <div><dt>模型</dt><dd>{provider.model || "未设置"}</dd></div>
          <div><dt>地址</dt><dd>{provider.base_url ?? "使用 Provider 默认地址"}</dd></div>
          <div><dt>Temperature</dt><dd>{provider.temperature}</dd></div>
          <div><dt>配置状态</dt><dd>{provider.status === "configured" ? "已配置（未检测连通性）" : "缺少模型配置"}</dd></div>
        </dl>)}
        <p className="config-note">只读配置；修改模型参数的接口尚未开放。此处为 API 进程配置，Worker 需保持一致。</p>
      </section>
      <Records title="MCP 工具目录" load={api.getTools} render={tool => <article key={tool.name} className="inspection-row"><b>{tool.name}</b><span>{tool.status}</span><p>{tool.description}</p><details><summary>输入 Schema</summary><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre></details></article>} />
      <Records title="全局指标采样" load={api.getMetrics} render={metricRow} />
    </div>
  </div>;
}

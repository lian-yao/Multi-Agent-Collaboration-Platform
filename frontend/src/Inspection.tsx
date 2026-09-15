import { useCallback, useEffect, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { ApiError, api } from "./api/client";
import type { DataPage, Metric, ProviderConfig, ProviderConfigUpdate, Workflow } from "./types/api";

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

const providerNames: Record<string, string> = {
  openai: "OpenAI 兼容 API",
  ollama: "Ollama（本地备用）",
};

type ProviderForm = {
  provider: string;
  model: string;
  baseUrl: string;
  apiKey: string;
  temperature: string;
};

type Notice = { tone: "ok" | "bad"; text: string };

function formFromConfig(config: ProviderConfig): ProviderForm {
  return {
    provider: config.provider,
    model: config.model,
    baseUrl: config.base_url ?? "",
    // 凭据只写入不回读，因此表单永远从空白开始
    apiKey: "",
    temperature: String(config.temperature),
  };
}

/**
 * 只把真正改动的字段放进请求体：省略 = 不改动，显式 null = 清除覆盖（doc/api.md §5.8）。
 * 清空某项即回退环境配置；凭据留空表示不修改，不会被误清除。
 */
function buildUpdate(initial: ProviderForm, current: ProviderForm): ProviderConfigUpdate {
  const update: ProviderConfigUpdate = {};
  if (current.provider !== initial.provider) update.provider = current.provider;
  if (current.model !== initial.model) update.model = current.model.trim() || null;
  if (current.baseUrl !== initial.baseUrl) update.base_url = current.baseUrl.trim() || null;
  if (current.temperature !== initial.temperature) {
    const raw = current.temperature.trim();
    update.temperature = raw ? Number(raw) : null;
  }
  if (current.apiKey.trim()) update.api_key = current.apiKey.trim();
  return update;
}

function temperatureProblem(form: ProviderForm): string {
  const raw = form.temperature.trim();
  if (!raw) return "";
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 2) {
    return "Temperature 需要是 0–2 之间的数字。";
  }
  return "";
}

export function ProviderConfigPanel() {
  const [config, setConfig] = useState<ProviderConfig | null>(null);
  const [initial, setInitial] = useState<ProviderForm | null>(null);
  const [form, setForm] = useState<ProviderForm | null>(null);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api.getProviderConfig();
      const next = formFromConfig(value);
      setConfig(value);
      setInitial(next);
      setForm(next);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pending = useMemo(
    () => (initial && form ? buildUpdate(initial, form) : {}),
    [initial, form],
  );
  const dirty = Object.keys(pending).length > 0;
  const temperatureIssue = form ? temperatureProblem(form) : "";

  const edit = (field: keyof ProviderForm) => (
    event: ChangeEvent<HTMLInputElement | HTMLSelectElement>,
  ) => {
    const value = event.target.value;
    setForm((current) => (current ? { ...current, [field]: value } : current));
    setNotice(null);
  };

  const reportFailure = (cause: unknown, fallback: string) => {
    if (cause instanceof ApiError && cause.status === 403) {
      setNotice({ tone: "bad", text: "管理员令牌缺失或不正确，服务端拒绝了写入（403）。" });
      return;
    }
    setNotice({ tone: "bad", text: cause instanceof Error ? cause.message : fallback });
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form || !initial) return;
    if (temperatureIssue) {
      setNotice({ tone: "bad", text: temperatureIssue });
      return;
    }
    if (!dirty) {
      setNotice({ tone: "bad", text: "没有需要保存的改动。" });
      return;
    }
    if (!token.trim()) {
      setNotice({ tone: "bad", text: "请先填写管理员令牌（ADMIN_TOKEN）再保存。" });
      return;
    }
    setSaving(true);
    try {
      await api.updateProviderConfig(buildUpdate(initial, form), token.trim());
      await load();
      setNotice({ tone: "ok", text: "已保存。下一次任务即按新配置执行。" });
    } catch (cause) {
      reportFailure(cause, "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  };

  const clearOverrides = async () => {
    if (!token.trim()) {
      setNotice({ tone: "bad", text: "请先填写管理员令牌（ADMIN_TOKEN）再操作。" });
      return;
    }
    setSaving(true);
    try {
      await api.updateProviderConfig(
        { model: null, base_url: null, api_key: null, temperature: null },
        token.trim(),
      );
      await load();
      setNotice({ tone: "ok", text: "已清除覆盖，全部回退环境配置。" });
    } catch (cause) {
      reportFailure(cause, "清除覆盖失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="config-panel config-panel-wide" aria-label="模型 Provider 配置">
      <div className="config-panel-head">
        <div>
          <span className="eyebrow">模型接入</span>
          <h2>Provider 配置</h2>
        </div>
        {config && (
          <span className={`config-badge ${config.api_key_configured ? "online" : "pending"}`}>
            {config.api_key_configured ? "凭据已配置" : "缺少凭据"}
          </span>
        )}
      </div>

      {error && <p role="alert">{error}（下方为上次读取结果）</p>}
      {loading && !config && <p role="status">加载中…</p>}

      {config && form && (
        <>
          <dl className="config-facts">
            <div><dt>当前生效</dt><dd>{providerNames[config.provider] ?? config.provider}</dd></div>
            <div><dt>模型</dt><dd>{config.model || "未设置"}</dd></div>
            <div><dt>地址</dt><dd>{config.base_url ?? "提供方默认地址"}</dd></div>
            <div><dt>Temperature</dt><dd>{config.temperature}</dd></div>
            <div>
              <dt>最近写入</dt>
              <dd>
                {config.updated_at
                  ? `${new Date(config.updated_at).toLocaleString("zh-CN")}${config.updated_by ? ` · ${config.updated_by}` : ""}`
                  : "从未通过本页保存"}
              </dd>
            </div>
          </dl>

          <form className="config-form" onSubmit={submit} noValidate>
            <div className="config-form-grid">
              <div className="config-field">
                <label htmlFor="provider-kind">提供方</label>
                <select id="provider-kind" value={form.provider} onChange={edit("provider")}>
                  <option value="openai">OpenAI 兼容 API</option>
                  <option value="ollama">Ollama（本地备用）</option>
                </select>
                <small>切换后模型名与地址按该提供方解析。</small>
              </div>
              <div className="config-field">
                <label htmlFor="provider-model">模型名</label>
                <input
                  id="provider-model"
                  value={form.model}
                  onChange={edit("model")}
                  placeholder="例如 gpt-4o-mini"
                  autoComplete="off"
                  spellCheck={false}
                />
                <small>留空并保存会清除覆盖，回退环境配置。</small>
              </div>
              <div className="config-field">
                <label htmlFor="provider-base-url">接口地址</label>
                <input
                  id="provider-base-url"
                  value={form.baseUrl}
                  onChange={edit("baseUrl")}
                  placeholder="https://api.example.com/v1"
                  autoComplete="off"
                  spellCheck={false}
                />
                <small>留空表示使用提供方默认地址。</small>
              </div>
              <div className="config-field">
                <label htmlFor="provider-temperature">Temperature</label>
                <input
                  id="provider-temperature"
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={form.temperature}
                  onChange={edit("temperature")}
                  aria-invalid={Boolean(temperatureIssue)}
                  aria-describedby={temperatureIssue ? "provider-temperature-error" : undefined}
                />
                <small id="provider-temperature-error" className={temperatureIssue ? "bad" : undefined}>
                  {temperatureIssue || "0–2 之间，留空则回退环境配置。"}
                </small>
              </div>
              <div className="config-field config-field-wide">
                <label htmlFor="provider-api-key">API 密钥</label>
                <input
                  id="provider-api-key"
                  type="password"
                  value={form.apiKey}
                  onChange={edit("apiKey")}
                  placeholder={config.api_key_configured ? "已配置；留空表示不修改" : "服务端尚未配置凭据"}
                  autoComplete="off"
                />
                <small>只写入、不回读，保存后输入框会清空；接口与日志都不会返回原值。</small>
              </div>
              <div className="config-field config-field-wide">
                <label htmlFor="provider-admin-token">管理员令牌</label>
                <input
                  id="provider-admin-token"
                  type="password"
                  value={token}
                  onChange={(event) => { setToken(event.target.value); setNotice(null); }}
                  placeholder="服务端环境变量 ADMIN_TOKEN"
                  autoComplete="off"
                />
                <small>只保留在本页内存中，不写入浏览器存储；未配置令牌的服务端一律拒绝写入。</small>
              </div>
            </div>

            <div className="config-actions">
              <button className="config-primary" type="submit" disabled={saving || Boolean(temperatureIssue)}>
                {saving ? "保存中…" : "保存配置"}
              </button>
              <button className="quiet-button" type="button" onClick={() => void load()} disabled={saving}>
                重新读取
              </button>
              <button className="quiet-button" type="button" onClick={() => void clearOverrides()} disabled={saving}>
                清除覆盖并回退环境配置
              </button>
            </div>

            <p className="config-status" role="status" aria-live="polite">
              {notice
                ? <span className={notice.tone === "bad" ? "bad" : "ok"}>{notice.text}</span>
                : dirty
                  ? "有未保存的改动。"
                  : "当前显示的是生效配置。"}
            </p>
          </form>
        </>
      )}

      <p className="config-note">
        配置写入后先落 PostgreSQL 事实源、再刷新 Redis 缓存，下一次任务即生效，无需重启进程。
        缓存缺失或不可用时会回源事实源，因此删除缓存不会丢配置。
      </p>
    </section>
  );
}

export function RuntimeConfig() {
  return <div className="config-page">
    <section className="page-heading"><h1>工具与配置</h1><p>模型接入配置、MCP 工具目录与执行采样</p></section>
    <div className="inspection-grid">
      <ProviderConfigPanel />
      <Records title="MCP 工具目录" load={api.getTools} render={tool => <article key={tool.name} className="inspection-row"><b>{tool.name}</b><span>{tool.status}</span><p>{tool.description}</p><details><summary>输入 Schema</summary><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre></details></article>} />
      <Records title="全局指标采样" load={api.getMetrics} render={metricRow} />
    </div>
  </div>;
}

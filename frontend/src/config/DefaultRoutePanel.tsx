import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { AvailableModel, ProviderConfig, ProviderConfigUpdate } from "../types/api";
import { Chip, Field, NoticeBar, describeError, describeIntervalError, type NoticeState } from "./shared";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI 兼容 API",
  ollama: "Ollama（本地）",
};

type RouteForm = {
  defaultModelId: string;
  provider: string;
  model: string;
  baseUrl: string;
  apiKey: string;
  temperature: string;
};

function formFromConfig(config: ProviderConfig): RouteForm {
  return {
    defaultModelId: config.default_llm_model_id ?? "",
    provider: config.provider,
    model: config.model,
    baseUrl: config.base_url ?? "",
    // 凭据只写入不回读：表单永远从空白开始。
    apiKey: "",
    temperature: String(config.temperature),
  };
}

function temperatureProblem(form: RouteForm): string {
  return describeIntervalError(form.temperature, "Temperature", 0, 2);
}

/** 省略 = 不改动，显式 `null` = 清除该层的覆盖（doc/api.md §5.8）。 */
function buildUpdate(initial: RouteForm, current: RouteForm): ProviderConfigUpdate {
  const update: ProviderConfigUpdate = {};
  if (current.defaultModelId !== initial.defaultModelId) {
    update.default_llm_model_id = current.defaultModelId || null;
  }
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

function modelLabel(model: AvailableModel): string {
  return `${model.provider_id} · ${model.name || model.model}`;
}

export function DefaultRoutePanel() {
  const [config, setConfig] = useState<ProviderConfig | null>(null);
  const [models, setModels] = useState<AvailableModel[]>([]);
  const [initial, setInitial] = useState<RouteForm | null>(null);
  const [form, setForm] = useState<RouteForm | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [modelsError, setModelsError] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api.getProviderConfig();
      setConfig(value);
      const next = formFromConfig(value);
      setInitial(next);
      setForm(next);
      setError("");
    } catch (cause) {
      setError(describeError(cause, "配置读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const response = await api.listModelRegistry(void 0, true);
      setModels(response.items);
      setModelsError("");
    } catch (cause) {
      setModels([]);
      setModelsError(
        describeError(cause, "可用模型清单读取失败；仍可手动填写模型名与地址。"),
      );
    }
  }, []);

  useEffect(() => {
    void load();
    void loadModels();
  }, [load, loadModels]);

  const pending = useMemo(
    () => (initial && form ? buildUpdate(initial, form) : {}),
    [initial, form],
  );
  const dirty = Object.keys(pending).length > 0;
  const issue = form ? temperatureProblem(form) : "";
  const dangling =
    Boolean(config?.default_llm_model_id) &&
    !models.some((item) => item.id === config?.default_llm_model_id);

  const edit = (field: keyof RouteForm) => (value: string) => {
    setForm((current) => (current ? { ...current, [field]: value } : current));
    setNotice(null);
  };

  const submit = async () => {
    if (!form || !initial) return;
    if (issue) {
      setNotice({ tone: "bad", text: issue });
      return;
    }
    if (!dirty) {
      setNotice({ tone: "bad", text: "没有需要保存的改动。" });
      return;
    }
    setSaving(true);
    try {
      await api.updateProviderConfig(buildUpdate(initial, form));
      await load();
      setNotice({ tone: "ok", text: "已保存。下一次任务即按新配置执行。" });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  const clearOverrides = async () => {
    setSaving(true);
    try {
      await api.updateProviderConfig({
        default_llm_model_id: null,
        model: null,
        base_url: null,
        api_key: null,
        temperature: null,
      });
      await load();
      setNotice({ tone: "ok", text: "已清除覆盖，全部回退环境配置。" });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "清除覆盖失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="cfg-stack">
      <section className="cfg-block">
        <div className="cfg-block-head">
          <div>
            <h3>默认路由</h3>
            <p>未绑定注册表模型、且没有角色覆盖时，任务按这里的配置建模。</p>
          </div>
          {config && (
            <Chip tone={config.api_key_configured ? "green" : "amber"}>
              {config.api_key_configured ? "凭据已配置" : "缺少凭据"}
            </Chip>
          )}
        </div>

        {error && (
          <p role="alert" className="cfg-alert">
            {error}（下方为上次读取结果）
          </p>
        )}
        {loading && !config && <p className="cfg-hint">加载中…</p>}

        {config && form && (
          <>
            <dl className="cfg-facts">
              <div>
                <dt>实际生效来源</dt>
                <dd>
                  {config.llm_model_id
                    ? `注册表模型 ${config.llm_model_id}`
                    : `legacy 五列（${PROVIDER_LABELS[config.provider] ?? config.provider}）`}
                </dd>
              </div>
              <div>
                <dt>生效模型</dt>
                <dd>
                  <code>{config.model || "未设置"}</code>
                </dd>
              </div>
              <div>
                <dt>端点</dt>
                <dd>{config.base_url ?? "提供方默认地址"}</dd>
              </div>
              <div>
                <dt>协议族</dt>
                <dd>
                  {config.preset_type} · {config.api_type}
                </dd>
              </div>
              <div>
                <dt>特化参数</dt>
                <dd>
                  Temperature {config.temperature}
                  {config.top_p !== null ? ` · Top P ${config.top_p}` : ""}
                  {config.max_tokens !== null ? ` · 输出 ${config.max_tokens}` : ""}
                  {config.reasoning_type !== "none" ? ` · 推理 ${config.reasoning_type}` : ""}
                </dd>
              </div>
              <div>
                <dt>最近写入</dt>
                <dd>
                  {config.updated_at
                    ? `${new Date(config.updated_at).toLocaleString("zh-CN")}${config.updated_by ? ` · ${config.updated_by}` : ""}`
                    : "从未通过本页保存"}
                </dd>
              </div>
            </dl>

            {dangling && (
              <p className="cfg-alert" role="alert">
                默认路由指向的模型条目 <code>{config.default_llm_model_id}</code>{" "}
                已不存在，已按未设置处理。重新选择或清除即可。
              </p>
            )}

            <div className="cfg-form-grid">
              <Field
                label="默认路由模型"
                htmlFor="dr-model-id"
                hint="非空时它（连同 Provider、凭据与特化参数）优先于下面的 legacy 五列。"
                wide
              >
                <select
                  id="dr-model-id"
                  value={form.defaultModelId}
                  onChange={(event) => edit("defaultModelId")(event.target.value)}
                >
                  <option value="">不指定（使用下方 legacy 配置）</option>
                  {dangling && (
                    <option value={form.defaultModelId}>{form.defaultModelId}（条目已删除）</option>
                  )}
                  {models.map((model) => (
                    <option value={model.id} key={model.id}>
                      {modelLabel(model)}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="Provider（legacy）" htmlFor="dr-provider" hint="仅在未指定默认路由模型时生效。">
                <select
                  id="dr-provider"
                  value={form.provider}
                  onChange={(event) => edit("provider")(event.target.value)}
                >
                  <option value="openai">OpenAI 兼容 API</option>
                  <option value="ollama">Ollama（本地）</option>
                </select>
              </Field>

              <Field label="模型名（legacy）" htmlFor="dr-model" hint="留空并保存 = 清除覆盖，回退环境配置。">
                <input
                  id="dr-model"
                  value={form.model}
                  onChange={(event) => edit("model")(event.target.value)}
                  placeholder="例如 gpt-4o-mini"
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>

              <Field label="接口地址（legacy）" htmlFor="dr-base-url" hint="留空表示使用提供方默认地址。" wide>
                <input
                  id="dr-base-url"
                  value={form.baseUrl}
                  onChange={(event) => edit("baseUrl")(event.target.value)}
                  placeholder="https://api.example.com/v1"
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>

              <Field
                label="Temperature（legacy）"
                htmlFor="dr-temperature"
                hint={issue || "0–2；留空则回退环境配置。"}
                tone={issue ? "bad" : undefined}
              >
                <input
                  id="dr-temperature"
                  type="number"
                  min={0}
                  max={2}
                  step={0.01}
                  value={form.temperature}
                  onChange={(event) => edit("temperature")(event.target.value)}
                  aria-invalid={Boolean(issue)}
                />
              </Field>

              <Field
                label="API 密钥（legacy）"
                htmlFor="dr-api-key"
                hint="只写入、不回读；留空表示不修改。"
              >
                <input
                  id="dr-api-key"
                  type="password"
                  value={form.apiKey}
                  onChange={(event) => edit("apiKey")(event.target.value)}
                  placeholder={config.api_key_configured ? "已配置；留空表示不修改" : "服务端尚未配置凭据"}
                  autoComplete="off"
                />
              </Field>
            </div>

            <div className="cfg-actions">
              <button
                type="button"
                className="cfg-primary"
                onClick={() => void submit()}
                disabled={saving || Boolean(issue)}
              >
                {saving ? "保存中…" : "保存配置"}
              </button>
              <button
                type="button"
                className="cfg-quiet"
                onClick={() => {
                  void load();
                  void loadModels();
                }}
                disabled={saving}
              >
                重新读取
              </button>
              <button type="button" className="cfg-quiet" onClick={() => void clearOverrides()} disabled={saving}>
                清除覆盖并回退环境配置
              </button>
            </div>
          </>
        )}

        {modelsError && (
          <p className="cfg-hint" role="alert">
            {modelsError}
          </p>
        )}
        {!modelsError && !models.length && !dangling && (
          <p className="cfg-hint">模型注册表里还没有启用的条目；下面仍可直接填写 legacy 配置。</p>
        )}
        <p className="cfg-hint">
          写入先落 PostgreSQL 事实源、再刷新 Redis 缓存；缓存缺失或不可用时会回源事实源，因此删除缓存不会丢配置。
        </p>
      </section>

      <NoticeBar notice={notice} />
    </div>
  );
}

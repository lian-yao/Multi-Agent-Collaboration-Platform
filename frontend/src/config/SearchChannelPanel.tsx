/**
 * SearchChannelPanel.tsx — 「搜索渠道」分区（`doc/api.md` §5.24、ADR-039）。
 *
 * ## 为什么要有这块界面
 *
 * `web_search` 的出口在 ADR-037 之后是 `TOOL_SEARCH_PROVIDER` 这一个**环境变量**，
 * 于是「换渠道」要改 `.env` 再重建容器。后果是：凭据没配时只能靠**报错**发现
 * （`未配置豆包搜索 API key（TOOL_SEARCH_API_KEY）`），而界面上既看不到当前渠道、
 * 也换不了。这个面板把渠道提升为可运行期读写的配置。
 *
 * ## 端点字段的三条约定（都不是样式偏好，错了就换错渠道）
 *
 * 1. **回显生效值**：留空会让使用者分不清「没配」与「配了但没显示」；
 * 2. **切换渠道时清空**：控制端点的值必须跟着渠道走。若把旧渠道的端点当成显式覆盖
 *    发出去，就会「拿着豆包的 key 打只认 DuckDuckGo 契约的网关」——后端的换渠道联动
 *    （`app/tools/search_config.py`）也就被架空了；
 * 3. **留空 = 用该渠道默认地址**：提交 `endpoint: null`，由后端按渠道落默认端点。
 *
 * ## 凭据
 *
 * API key **只写入、不回读**（与「默认路由」面板同一口径）：输入框永远从空白开始，
 * 已配置时用 placeholder 说明「留空表示不修改」。后端只回 `api_key_configured`。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Globe } from "lucide-react";
import { api } from "../api/client";
import type { SearchChannelOption, SearchConfig, SearchConfigUpdate } from "../types/api";
import {
  Chip,
  Field,
  NoticeBar,
  describeError,
  formatTime,
  type NoticeState,
} from "./shared";

export type ChannelForm = {
  provider: string;
  endpoint: string;
  apiKey: string;
};

/** 渠道目录为空时的兜底（后端应答缺 `channels` 也能把界面画出来）。 */
const FALLBACK_CHANNEL: SearchChannelOption = {
  id: "duckduckgo",
  label: "DuckDuckGo 契约",
  needs_api_key: false,
  default_endpoint: "",
  description: "",
};

export function formFromConfig(config: SearchConfig): ChannelForm {
  return {
    provider: config.provider,
    endpoint: config.endpoint,
    // 凭据只写入不回读：表单永远从空白开始。
    apiKey: "",
  };
}

export function channelOf(
  channels: readonly SearchChannelOption[] | undefined,
  id: string,
): SearchChannelOption {
  return channels?.find((channel) => channel.id === id) ?? FALLBACK_CHANNEL;
}

/** 省略 = 不改动，显式 `null` = 清除该层覆盖（`doc/api.md` §5.24）。 */
export function buildSearchUpdate(
  initial: ChannelForm,
  current: ChannelForm,
): SearchConfigUpdate {
  const update: SearchConfigUpdate = {};
  if (current.provider !== initial.provider) update.provider = current.provider;
  const endpoint = current.endpoint.trim();
  if (endpoint !== initial.endpoint.trim()) update.endpoint = endpoint || null;
  if (current.apiKey.trim()) update.api_key = current.apiKey.trim();
  return update;
}

/** 切换渠道时把端点清空——见文件头第 2 条约定。 */
export function switchChannel(current: ChannelForm, provider: string): ChannelForm {
  return { ...current, provider, endpoint: "" };
}

/**
 * 选了「需要凭据」的渠道、且这次保存不会让它有凭据时，返回一句人话。
 *
 * 这条提示存在的唯一理由是：不配 key 的失败**只**以工具报错的形式出现
 * （ADR-037 §2 刻意不发无鉴权请求），使用者在界面上看不到任何预兆。
 */
export function missingKeyWarning(
  config: SearchConfig,
  form: ChannelForm,
): string {
  const channel = channelOf(config.channels, form.provider);
  if (!channel.needs_api_key) return "";
  const willHaveKey = Boolean(form.apiKey.trim()) || config.api_key_configured;
  if (willHaveKey) return "";
  return (
    `${channel.label}需要 API key，但当前还没有配置：此时调用 web_search 会直接报` +
    `「未配置豆包搜索 API key」并失败。在下面填入 API key 并保存即可。`
  );
}

/** 纯 props 驱动的展示体，便于 `frontend/rendercheck` 直接挂载断言。 */
export function SearchChannelBoundary({
  config,
  form,
  loading = false,
  saving = false,
  error = "",
  notice,
  onChange,
  onSubmit,
  onReload,
  onClearOverrides,
}: {
  config: SearchConfig | null;
  form: ChannelForm | null;
  loading?: boolean;
  saving?: boolean;
  error?: string;
  notice?: NoticeState;
  onChange?: (field: keyof ChannelForm, value: string) => void;
  onSubmit?: () => void;
  onReload?: () => void;
  onClearOverrides?: () => void;
}) {
  const channels = config?.channels?.length ? config.channels : [FALLBACK_CHANNEL];
  const pending = useMemo(
    () => (config && form ? buildSearchUpdate(formFromConfig(config), form) : {}),
    [config, form],
  );
  const dirty = Object.keys(pending).length > 0;
  const warning = config && form ? missingKeyWarning(config, form) : "";

  return (
    <section className="cfg-block">
      <div className="cfg-block-head">
        <div className="cfg-block-title">
          <Globe size={16} />
          <div>
            <h3>搜索渠道</h3>
            <p>
              <code>web_search</code> 走哪条出口、用哪份凭据。默认跟随环境配置
              （一键部署指向同网络的本地网关）；在这里保存的覆盖值优先于环境配置，
              <b>下一次任务即生效</b>，不需要重建容器。
            </p>
          </div>
        </div>
        {config && (
          <div className="cfg-row-actions">
            <Chip tone={config.overridden ? "amber" : "slate"}>
              {config.overridden ? "已覆盖环境配置" : "跟随环境配置"}
            </Chip>
            <Chip
              tone={
                channelOf(config.channels, config.provider).needs_api_key
                  ? config.api_key_configured
                    ? "green"
                    : "amber"
                  : "slate"
              }
            >
              {config.api_key_configured ? "凭据已配置" : "凭据未配置"}
            </Chip>
          </div>
        )}
      </div>

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
      {loading && !config && <p className="cfg-hint">读取中…</p>}

      {config && form && (
        <>
          <dl className="cfg-facts">
            <div>
              <dt>生效渠道</dt>
              <dd>{channelOf(config.channels, config.provider).label}</dd>
            </div>
            <div>
              <dt>生效端点</dt>
              <dd>
                <code>{config.endpoint || "（未设置）"}</code>
              </dd>
            </div>
            <div>
              <dt>环境配置</dt>
              <dd>
                {channelOf(config.channels, config.env_provider).label} ·{" "}
                <code>{config.env_endpoint || "（未设置）"}</code>
                {config.env_api_key_configured ? " · 环境凭据已配置" : ""}
              </dd>
            </div>
            <div>
              <dt>最近写入</dt>
              <dd>
                {config.updated_at
                  ? `${formatTime(config.updated_at)}${config.updated_by ? ` · ${config.updated_by}` : ""}`
                  : "从未通过本页保存"}
              </dd>
            </div>
          </dl>

          <div className="cfg-form-grid">
            <Field
              label="搜索渠道"
              htmlFor="sc-provider"
              hint={channelOf(config.channels, form.provider).description}
              wide
            >
              <select
                id="sc-provider"
                value={form.provider}
                onChange={(event) => onChange?.("provider", event.target.value)}
              >
                {channels.map((channel) => (
                  <option value={channel.id} key={channel.id}>
                    {channel.label}
                    {channel.needs_api_key ? "（需要 API key）" : "（无需凭据）"}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="端点地址"
              htmlFor="sc-endpoint"
              hint="留空 = 使用该渠道的默认地址；切换渠道会清空此项，交回后端按渠道取默认值。"
              wide
            >
              <input
                id="sc-endpoint"
                value={form.endpoint}
                onChange={(event) => onChange?.("endpoint", event.target.value)}
                placeholder={
                  channelOf(config.channels, form.provider).default_endpoint ||
                  "留空 = 该渠道默认地址"
                }
                autoComplete="off"
                spellCheck={false}
              />
            </Field>

            <Field
              label="API key"
              htmlFor="sc-api-key"
              hint={
                channelOf(config.channels, form.provider).needs_api_key
                  ? "只写入、不回读；留空表示不修改。该渠道必须配置，否则工具直接报配置错。"
                  : "只写入、不回读；留空表示不修改。该渠道不需要凭据，这里填的值不影响它。"
              }
              wide
            >
              <input
                id="sc-api-key"
                type="password"
                value={form.apiKey}
                onChange={(event) => onChange?.("apiKey", event.target.value)}
                placeholder={
                  config.api_key_configured ? "已配置；留空表示不修改" : "尚未配置凭据"
                }
                autoComplete="off"
              />
            </Field>
          </div>

          {warning && (
            <p className="cfg-alert" role="alert">
              {warning}
            </p>
          )}

          <div className="cfg-actions">
            <button
              type="button"
              className="cfg-primary"
              onClick={onSubmit}
              disabled={saving || !dirty}
            >
              {saving ? "保存中…" : "保存配置"}
            </button>
            <button
              type="button"
              className="cfg-quiet"
              onClick={onReload}
              disabled={saving || !onReload}
            >
              重新读取
            </button>
            <button
              type="button"
              className="cfg-quiet"
              onClick={onClearOverrides}
              disabled={saving || !config.overridden || !onClearOverrides}
            >
              恢复环境配置（清除覆盖）
            </button>
          </div>
        </>
      )}

      <p className="cfg-hint">
        覆盖值落 PostgreSQL（<code>tool_configs</code> 单行表）；保存后平台会失效内置工具与工具注册表
        两处进程缓存，因此无需重启。凭据以明文存在库里，不回传、不落日志。
      </p>

      <NoticeBar notice={notice ?? null} />
    </section>
  );
}

/** 容器：负责取数与动作，展示交给 `SearchChannelBoundary`。 */
export function SearchChannelPanel() {
  const [config, setConfig] = useState<SearchConfig | null>(null);
  const [form, setForm] = useState<ChannelForm | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api.getSearchConfig();
      setConfig(value);
      setForm(formFromConfig(value));
      setError("");
    } catch (cause) {
      setConfig(null);
      setForm(null);
      setError(describeError(cause, "搜索渠道配置读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const edit = (field: keyof ChannelForm, value: string) => {
    setNotice(null);
    setForm((current) => {
      if (!current) return current;
      // 切渠道时把端点清空：控制端点的值必须跟着渠道走（见文件头第 2 条约定）。
      if (field === "provider" && value !== current.provider) {
        return switchChannel(current, value);
      }
      return { ...current, [field]: value };
    });
  };

  const submit = async () => {
    if (!config || !form) return;
    const update = buildSearchUpdate(formFromConfig(config), form);
    if (Object.keys(update).length === 0) {
      setNotice({ tone: "bad", text: "没有需要保存的改动。" });
      return;
    }
    setSaving(true);
    try {
      const saved = await api.updateSearchConfig(update);
      setConfig(saved);
      setForm(formFromConfig(saved));
      setNotice({ tone: "ok", text: "已保存。下一次任务即按新渠道执行。" });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  const clearOverrides = async () => {
    setSaving(true);
    try {
      const saved = await api.updateSearchConfig({
        provider: null,
        endpoint: null,
        api_key: null,
      });
      setConfig(saved);
      setForm(formFromConfig(saved));
      setNotice({ tone: "ok", text: "已清除覆盖，搜索渠道全部回退环境配置。" });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "清除覆盖失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <SearchChannelBoundary
      config={config}
      form={form}
      loading={loading}
      saving={saving}
      error={error}
      notice={notice}
      onChange={edit}
      onSubmit={() => void submit()}
      onReload={() => void load()}
      onClearOverrides={() => void clearOverrides()}
    />
  );
}

export default SearchChannelPanel;

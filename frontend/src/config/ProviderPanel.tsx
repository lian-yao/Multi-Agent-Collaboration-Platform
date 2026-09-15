import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { api, ApiError } from "../api/client";
import { PageTabs } from "../components/PageTabs";
import type {
  ProviderPreset,
  ProviderPresetCatalog,
  ProviderRegistry,
  ProviderRegistryCreate,
  ProviderRegistryDetail,
  ProviderRegistryUpdate,
} from "../types/api";
import {
  Chip,
  EmptyState,
  Field,
  Modal,
  Monogram,
  NoticeBar,
  Switch,
  describeError,
  formatPairs,
  formatTime,
  parsePairs,
  type NoticeState,
} from "./shared";
import { ModelSection } from "./ModelSection";

/* -------------------------------------------------------------------------- */
/* 预设选择器                                                                  */
/* -------------------------------------------------------------------------- */

function PresetPicker({
  catalog,
  value,
  onChange,
}: {
  catalog: ProviderPresetCatalog | null;
  value: string;
  onChange: (preset: ProviderPreset) => void;
}) {
  const [category, setCategory] = useState("all");
  const [keyword, setKeyword] = useState("");

  const categories = catalog?.categories ?? [{ id: "all", label: "全部" }];

  const items = useMemo(() => {
    const all = catalog?.items ?? [];
    const byCategory = category === "all" ? all : all.filter((item) => item.category === category);
    const trimmed = keyword.trim().toLowerCase();
    if (!trimmed) return byCategory;
    return byCategory.filter(
      (item) =>
        item.label.toLowerCase().includes(trimmed) ||
        item.preset_type.toLowerCase().includes(trimmed) ||
        (item.default_base_url ?? "").toLowerCase().includes(trimmed),
    );
  }, [catalog, category, keyword]);

  return (
    <div className="cfg-preset-picker">
      <div className="cfg-preset-toolbar">
        <PageTabs
          variant="inline"
          label="预设分类"
          tabs={categories}
          active={category}
          onChange={setCategory}
        />
        <input
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="搜索预设"
          aria-label="搜索预设"
          autoComplete="off"
        />
      </div>
      <div className="cfg-preset-grid" role="radiogroup" aria-label="Provider 预设">
        {items.map((preset) => (
          <button
            key={preset.preset_type}
            type="button"
            role="radio"
            aria-checked={value === preset.preset_type}
            className={`cfg-preset${value === preset.preset_type ? " selected" : ""}`}
            onClick={() => onChange(preset)}
          >
            <Monogram
              text={preset.monogram ?? preset.preset_type.slice(0, 2)}
              tint={preset.tint}
              size={30}
            />
            <span className="cfg-preset-text">
              <b>{preset.label}</b>
              <small>{preset.default_base_url || "自定义端点"}</small>
            </span>
          </button>
        ))}
        {!items.length && <EmptyState title="没有匹配的预设" hint="可直接选择「自定义」并手填端点。" />}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 兜底预设：预设目录读取失败时仍能新建                                         */
/* -------------------------------------------------------------------------- */

const FALLBACK_PRESET: ProviderPreset = {
  preset_type: "openai-compatible",
  label: "OpenAI 兼容（自定义）",
  monogram: "自定义",
  tint: "slate",
  category: "gateway",
  default_api_type: "openai-compatible",
  supported_api_types: ["openai-compatible", "openai-responses", "anthropic", "gemini"],
  default_base_url: "",
  requires_api_key: true,
  api_key_url: null,
  supports_model_discovery: true,
};

function providerIdProblem(id: string): string {
  if (!id.trim()) return "ID 不能为空。";
  if (id.length > 50) return "ID 不能超过 50 个字符。";
  if (!/^[A-Za-z0-9._-]+$/.test(id)) return "ID 只允许字母、数字与 . _ - 。";
  return "";
}

function headersProblem(text: string): string {
  if (!text.trim()) return "";
  if (parsePairs(text) === null) return "自定义请求头每行需为 KEY=VALUE。";
  const parsed = parsePairs(text) ?? {};
  if (Object.keys(parsed).length > 20) return "自定义请求头最多 20 项。";
  if (Object.values(parsed).some((value) => value.length > 500)) return "请求头取值不能超过 500 个字符。";
  return "";
}

/* -------------------------------------------------------------------------- */
/* Provider 表单弹层                                                            */
/* -------------------------------------------------------------------------- */

type ProviderForm = {
  id: string;
  name: string;
  preset_type: string;
  api_type: string;
  base_url: string;
  api_key: string;
  headers: string;
  enabled: boolean;
};

function ProviderFormModal({
  catalog,
  editing,
  onClose,
  onSaved,
}: {
  catalog: ProviderPresetCatalog | null;
  editing: ProviderRegistry | null;
  onClose: () => void;
  onSaved: (message: string) => Promise<void>;
}) {
  const presets = catalog?.items ?? [];
  const presetOf = (type: string): ProviderPreset | null =>
    presets.find((item) => item.preset_type === type) ??
    (type === FALLBACK_PRESET.preset_type ? FALLBACK_PRESET : null);

  const seed = editing ? presetOf(editing.preset_type) : presets[0] ?? FALLBACK_PRESET;

  const [form, setForm] = useState<ProviderForm>(() => ({
    id: editing?.id ?? "",
    name: editing?.name ?? seed?.label ?? "",
    preset_type: editing?.preset_type ?? seed?.preset_type ?? FALLBACK_PRESET.preset_type,
    api_type: editing?.api_type ?? seed?.default_api_type ?? FALLBACK_PRESET.default_api_type,
    base_url: editing?.base_url ?? seed?.default_base_url ?? "",
    // 凭据只写入不回读：编辑时输入框恒为空，留空 = 不修改。
    api_key: "",
    headers: formatPairs(editing?.custom_headers),
    enabled: editing?.enabled ?? true,
  }));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  const preset = presetOf(form.preset_type) ?? FALLBACK_PRESET;
  const apiTypeOptions = preset.supported_api_types.length
    ? preset.supported_api_types
    : [preset.default_api_type];

  const problem =
    (editing ? "" : providerIdProblem(form.id)) ||
    (!form.name.trim() ? "名称不能为空。" : "") ||
    (form.name.length > 100 ? "名称不能超过 100 个字符。" : "") ||
    (form.base_url.length > 500 ? "地址不能超过 500 个字符。" : "") ||
    headersProblem(form.headers);

  /** `enabled` 由弹层外部的开关控制，不走这里。 */
  const edit = (key: Exclude<keyof ProviderForm, "enabled">) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      setForm((current) => ({ ...current, [key]: event.target.value }) as ProviderForm);

  /** 换预设即整套换默认值；仅在新建且用户还没自己改过时才覆盖名称与 ID。 */
  const choosePreset = (next: ProviderPreset) => {
    setForm((current) => ({
      ...current,
      preset_type: next.preset_type,
      api_type: next.default_api_type,
      base_url: next.default_base_url ?? "",
      name: !editing && (!current.name || current.name === preset.label) ? next.label : current.name,
      id:
        !editing && !current.id
          ? next.preset_type.replace(/[^A-Za-z0-9._-]/g, "-")
          : current.id,
    }));
    setNotice(null);
  };

  const submit = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    const headers = parsePairs(form.headers) ?? {};
    setSaving(true);
    try {
      if (editing) {
        // 只提交改过的字段：省略 = 不改动，`api_key` 空串 = 不修改（doc/api.md §5.9）。
        const patch: ProviderRegistryUpdate = {};
        if (form.name.trim() !== editing.name) patch.name = form.name.trim();
        if (form.preset_type !== editing.preset_type) patch.preset_type = form.preset_type;
        if (form.api_type !== editing.api_type) patch.api_type = form.api_type;
        const nextBase = form.base_url.trim() || null;
        if (nextBase !== editing.base_url) patch.base_url = nextBase;
        if (form.api_key.trim()) patch.api_key = form.api_key.trim();
        if (form.headers.trim() !== formatPairs(editing.custom_headers)) patch.custom_headers = headers;
        if (form.enabled !== editing.enabled) patch.enabled = form.enabled;
        if (!Object.keys(patch).length) {
          setNotice({ tone: "bad", text: "没有需要保存的改动。" });
          setSaving(false);
          return;
        }
        await api.patchProviderRegistry(editing.id, patch);
        await onSaved(`已更新 Provider ${editing.id}。`);
      } else {
        const payload: ProviderRegistryCreate = {
          id: form.id.trim(),
          name: form.name.trim(),
          preset_type: form.preset_type,
          api_type: form.api_type,
          base_url: form.base_url.trim() || null,
          api_key: form.api_key.trim() || null,
          custom_headers: headers,
          enabled: form.enabled,
        };
        const created = await api.createProviderRegistry(payload);
        await onSaved(`已登记 Provider ${created.id}。`);
      }
      onClose();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={editing ? `编辑 Provider · ${editing.id}` : "新建 Provider"}
      subtitle={editing ? undefined : "先选预设再改端点：预设决定协议族、默认地址与是否支持远端发现。"}
      wide
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            className="cfg-primary"
            onClick={() => void submit()}
            disabled={saving || Boolean(problem)}
          >
            {saving ? "保存中…" : editing ? "保存修改" : "登记 Provider"}
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
            取消
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      {!editing && <PresetPicker catalog={catalog} value={form.preset_type} onChange={choosePreset} />}

      <div className="cfg-provider-preview">
        <Monogram
          text={preset.monogram ?? preset.preset_type.slice(0, 2)}
          tint={preset.tint}
          size={38}
        />
        <div>
          <b>{preset.label}</b>
          <small>
            {preset.default_api_type} ·{" "}
            {preset.supports_model_discovery ? "支持远端发现" : "不支持远端发现"}
            {preset.requires_api_key ? " · 需要凭据" : " · 无需凭据"}
          </small>
        </div>
        {preset.api_key_url && (
          <a href={preset.api_key_url} target="_blank" rel="noreferrer">
            获取密钥
          </a>
        )}
      </div>

      <div className="cfg-form-grid">
        <Field
          label="ID"
          htmlFor="pf-id"
          hint={editing ? "创建后不可修改。" : "1–50 字符，仅 A-Za-z0-9._-"}
          tone={!editing && providerIdProblem(form.id) ? "bad" : undefined}
        >
          <input
            id="pf-id"
            value={form.id}
            onChange={edit("id")}
            disabled={Boolean(editing)}
            placeholder="gateway-main"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="名称" htmlFor="pf-name" hint="1–100 字符，仅用于界面展示。">
          <input id="pf-name" value={form.name} onChange={edit("name")} autoComplete="off" />
        </Field>
        <Field label="协议族" htmlFor="pf-preset" hint="决定默认端点与发现路径。">
          <select
            id="pf-preset"
            value={form.preset_type}
            onChange={(event) => {
              const next = presetOf(event.target.value) ?? FALLBACK_PRESET;
              choosePreset(next);
            }}
          >
            {presets.map((item) => (
              <option value={item.preset_type} key={item.preset_type}>
                {item.label}
              </option>
            ))}
            {!presets.some((item) => item.preset_type === FALLBACK_PRESET.preset_type) && (
              <option value={FALLBACK_PRESET.preset_type}>{FALLBACK_PRESET.label}</option>
            )}
          </select>
        </Field>
        <Field label="接口协议" htmlFor="pf-api-type" hint="同族下可切换（如 DeepSeek 支持 OpenAI / Anthropic）。">
          <select id="pf-api-type" value={form.api_type} onChange={edit("api_type")}>
            {apiTypeOptions.map((type) => (
              <option value={type} key={type}>
                {type}
              </option>
            ))}
            {!apiTypeOptions.includes(form.api_type) && (
              <option value={form.api_type}>{form.api_type}</option>
            )}
          </select>
        </Field>
        <Field label="接口地址" htmlFor="pf-base-url" hint="留空使用预设默认端点。" wide>
          <input
            id="pf-base-url"
            value={form.base_url}
            onChange={edit("base_url")}
            placeholder={preset.default_base_url || "https://api.example.com/v1"}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field
          label="API 密钥"
          htmlFor="pf-api-key"
          hint={
            editing
              ? "留空表示不修改；只写入、不回读。"
              : "只写入、不回读；接口与日志都不返回原值。"
          }
          wide
        >
          <input
            id="pf-api-key"
            type="password"
            value={form.api_key}
            onChange={edit("api_key")}
            placeholder={
              editing?.api_key_configured ? "已配置；留空表示不修改" : "服务端尚未配置凭据"
            }
            autoComplete="off"
          />
        </Field>
        <Field
          label="自定义请求头"
          htmlFor="pf-headers"
          hint="每行 KEY=VALUE，最多 20 项；# 开头为注释。"
          tone={headersProblem(form.headers) ? "bad" : undefined}
          wide
        >
          <textarea
            id="pf-headers"
            rows={3}
            value={form.headers}
            onChange={edit("headers")}
            spellCheck={false}
            placeholder="X-Org=research"
          />
        </Field>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 面板主体：左列表 + 右详情（列表与详情各自成块，不整页居中）                   */
/* -------------------------------------------------------------------------- */

export function ProviderPanel({ catalog }: { catalog: ProviderPresetCatalog | null }) {
  const [providers, setProviders] = useState<ProviderRegistry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProviderRegistryDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [editing, setEditing] = useState<ProviderRegistry | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  const [busy, setBusy] = useState(false);

  // 刷新时要在异步回调里读到「当前选中」，用 ref 避免把 selectedId 塞进依赖数组。
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;

  const presetOf = (type: string): ProviderPreset | null =>
    catalog?.items.find((item) => item.preset_type === type) ??
    (type === FALLBACK_PRESET.preset_type ? FALLBACK_PRESET : null);

  const loadDetail = useCallback(async (id: string) => {
    try {
      const value = await api.getProviderRegistry(id);
      setDetail(value);
      setDetailError("");
    } catch (cause) {
      setDetail(null);
      setDetailError(describeError(cause, "Provider 详情读取失败。"));
    }
  }, []);

  const refresh = useCallback(
    async (prefer?: string | null) => {
      setLoading(true);
      try {
        const response = await api.listProviderRegistry();
        setProviders(response.items);
        setLoadError("");
        const current = prefer === undefined ? selectedRef.current : prefer;
        const next =
          current && response.items.some((item) => item.id === current)
            ? current
            : response.items[0]?.id ?? null;
        setSelectedId(next);
        if (next) await loadDetail(next);
        else setDetail(null);
      } catch (cause) {
        setLoadError(describeError(cause, "Provider 注册表读取失败。"));
      } finally {
        setLoading(false);
      }
    },
    [loadDetail],
  );

  useEffect(() => {
    void refresh(undefined);
  }, [refresh]);

  const select = async (id: string) => {
    setSelectedId(id);
    setNotice(null);
    await loadDetail(id);
  };

  const toggleEnabled = async (provider: ProviderRegistry) => {
    setBusy(true);
    try {
      await api.patchProviderRegistry(provider.id, { enabled: !provider.enabled });
      await refresh(provider.id);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "切换启用状态失败。") });
    } finally {
      setBusy(false);
    }
  };

  /** 仍有启用模型时后端返回 409 PROVIDER_IN_USE，此时再问一次是否强制级联删除。 */
  const remove = async (provider: ProviderRegistry) => {
    if (
      !window.confirm(
        `删除 Provider「${provider.name}」？其下 ${provider.model_count} 条模型条目会一并删除。`,
      )
    )
      return;
    setBusy(true);
    try {
      await api.deleteProviderRegistry(provider.id);
      await refresh(null);
      setNotice({ tone: "ok", text: `已删除 Provider ${provider.id}。` });
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "PROVIDER_IN_USE") {
        if (window.confirm(`${cause.message}\n\n仍要强制级联删除吗？`)) {
          try {
            await api.deleteProviderRegistry(provider.id, true);
            await refresh(null);
            setNotice({ tone: "ok", text: `已强制删除 Provider ${provider.id}。` });
          } catch (inner) {
            setNotice({ tone: "bad", text: describeError(inner, "强制删除失败。") });
          }
        }
      } else {
        setNotice({ tone: "bad", text: describeError(cause, "删除失败。") });
      }
    } finally {
      setBusy(false);
    }
  };

  const afterSave = async (id: string | null, message: string) => {
    await refresh(id);
    setNotice({ tone: "ok", text: message });
  };

  return (
    <div className="cfg-registry">
      <aside className="cfg-registry-list" aria-label="Provider 列表">
        <div className="cfg-block-head">
          <div>
            <h3>Provider</h3>
            <p>{providers.length} 个端点</p>
          </div>
          <button type="button" className="cfg-primary" onClick={() => setCreateOpen(true)}>
            新建
          </button>
        </div>

        {loadError && (
          <p role="alert" className="cfg-alert">
            {loadError}
          </p>
        )}
        {loading && !providers.length && <p className="cfg-hint">加载中…</p>}
        {!loading && !providers.length && !loadError && (
          <EmptyState title="还没有 Provider" hint="新建一个端点后即可批量引入模型。" />
        )}

        <ul className="cfg-provider-list">
          {providers.map((provider) => {
            const preset = presetOf(provider.preset_type);
            return (
              <li key={provider.id}>
                <button
                  type="button"
                  className={`cfg-provider-item${selectedId === provider.id ? " active" : ""}`}
                  onClick={() => void select(provider.id)}
                  aria-current={selectedId === provider.id}
                >
                  <Monogram
                    text={preset?.monogram ?? provider.preset_type.slice(0, 2)}
                    tint={preset?.tint}
                    size={30}
                  />
                  <span className="cfg-provider-text">
                    <b>{provider.name}</b>
                    <small>{provider.base_url ?? "预设默认端点"}</small>
                  </span>
                  <span className="cfg-provider-side">
                    <span className="cfg-count">{provider.model_count} 模型</span>
                    {!provider.enabled && <Chip tone="slate">已停用</Chip>}
                    {provider.api_key_configured ? (
                      <Chip tone="green">有凭据</Chip>
                    ) : (
                      <Chip tone="amber">缺凭据</Chip>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </aside>

      <div className="cfg-registry-detail">
        {detailError && (
          <p role="alert" className="cfg-alert">
            {detailError}
          </p>
        )}
        {!detail && !detailError && (
          <EmptyState
            title="选择左侧的一个 Provider"
            hint="可查看其模型条目、批量引入与特化调参。"
          />
        )}

        {detail && (
          <>
            <section className="cfg-block">
              <div className="cfg-block-head">
                <div className="cfg-block-title">
                  <Monogram
                    text={presetOf(detail.preset_type)?.monogram ?? detail.preset_type.slice(0, 2)}
                    tint={presetOf(detail.preset_type)?.tint}
                    size={38}
                  />
                  <div>
                    <h3>{detail.name}</h3>
                    <p>
                      <code>{detail.id}</code> ·{" "}
                      {presetOf(detail.preset_type)?.label ?? detail.preset_type}
                    </p>
                  </div>
                </div>
                <div className="cfg-row-actions">
                  <Switch
                    checked={detail.enabled}
                    disabled={busy}
                    onChange={() => void toggleEnabled(detail)}
                    label={`${detail.enabled ? "停用" : "启用"} ${detail.name}`}
                  />
                  <button
                    type="button"
                    className="cfg-quiet"
                    onClick={() => setEditing(detail)}
                    disabled={busy}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className="cfg-quiet danger"
                    onClick={() => void remove(detail)}
                    disabled={busy}
                  >
                    删除
                  </button>
                </div>
              </div>

              <dl className="cfg-facts">
                <div>
                  <dt>接口协议</dt>
                  <dd>{detail.api_type}</dd>
                </div>
                <div>
                  <dt>接口地址</dt>
                  <dd>{detail.base_url ?? "预设默认端点"}</dd>
                </div>
                <div>
                  <dt>凭据</dt>
                  <dd>{detail.api_key_configured ? "已配置（不回读原值）" : "未配置"}</dd>
                </div>
                <div>
                  <dt>自定义请求头</dt>
                  <dd>{Object.keys(detail.custom_headers).length} 项</dd>
                </div>
                <div>
                  <dt>最近更新</dt>
                  <dd>
                    {formatTime(detail.updated_at)}
                    {detail.updated_by ? ` · ${detail.updated_by}` : ""}
                  </dd>
                </div>
              </dl>
            </section>

            <ModelSection
              provider={detail}
              preset={presetOf(detail.preset_type)}
              onChanged={() => loadDetail(detail.id)}
              onNotice={setNotice}
            />
          </>
        )}

        <NoticeBar notice={notice} />
      </div>

      {createOpen && (
        <ProviderFormModal
          catalog={catalog}
          editing={null}
          onClose={() => setCreateOpen(false)}
          onSaved={(message) => afterSave(null, message)}
        />
      )}
      {editing && (
        <ProviderFormModal
          catalog={catalog}
          editing={editing}
          onClose={() => setEditing(null)}
          onSaved={(message) => afterSave(editing.id, message)}
        />
      )}
    </div>
  );
}

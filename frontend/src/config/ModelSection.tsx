import { useCallback, useMemo, useState, type ChangeEvent, type FormEvent } from "react";
import { ChevronDown } from "lucide-react";
import { api } from "../api/client";
import { InlineConfirm } from "../components/InlineConfirm";
import {
  CUSTOM_PARAMETER_TYPES,
  REASONING_TYPES,
  type CustomParameter,
  type CustomParameterType,
  type ModelBatchImportRequest,
  type ModelBatchImportResult,
  type ModelDiscovery,
  type ModelRegistry,
  type ModelRegistryCreate,
  type ModelRegistryUpdate,
  type ProviderPreset,
  type ProviderRegistryDetail,
  type ReasoningType,
} from "../types/api";
import {
  Chip,
  EmptyState,
  Field,
  Modal,
  NoticeBar,
  Switch,
  assignIfChanged,
  describeError,
  describeIntervalError,
  formatTime,
  parseInteger,
  parseLines,
  sameParameters,
  type NoticeState,
} from "./shared";

const REASONING_LABELS: Record<string, string> = {
  none: "无推理",
  openai: "OpenAI 推理",
  gemini: "Gemini 推理",
  anthropic: "Anthropic 推理",
};

const REASONING_TINT: Record<string, string> = {
  none: "slate",
  openai: "green",
  gemini: "teal",
  anthropic: "amber",
};

/* -------------------------------------------------------------------------- */
/* 表单模型：数字一律用字符串承接输入框，提交时才转数值                          */
/* -------------------------------------------------------------------------- */

type ModelForm = {
  name: string;
  model: string;
  enabled: boolean;
  reasoning_type: string;
  temperature: string;
  top_p: string;
  max_context_tokens: string;
  max_output_tokens: string;
  custom_parameters: CustomParameter[];
};

/**
 * 可直接用同一个文本 setter 写入的字段。
 *
 * 由 `ModelForm` 自身推导，而不是手写 `Exclude<...>`：以后往表单里加字符串字段时
 * 不必回来改这里，加非字符串字段时也自动被排除、不会误接进 `onChange`。
 */
type TextField = {
  [K in keyof ModelForm]: ModelForm[K] extends string ? K : never;
}[keyof ModelForm];

const AS_TEXT = (value: number | null): string => (value === null ? "" : String(value));

function formFromModel(model: ModelRegistry): ModelForm {
  return {
    name: model.name ?? "",
    model: model.model,
    enabled: model.enabled,
    reasoning_type: model.reasoning_type,
    temperature: AS_TEXT(model.temperature),
    top_p: AS_TEXT(model.top_p),
    max_context_tokens: AS_TEXT(model.max_context_tokens),
    max_output_tokens: AS_TEXT(model.max_output_tokens),
    custom_parameters: model.custom_parameters.map((item) => ({ ...item })),
  };
}

function parameterProblem(parameters: CustomParameter[]): string {
  const seen = new Set<string>();
  for (const item of parameters) {
    const key = item.key.trim();
    if (!key) return "特化参数的键名不能为空。";
    if (key.length > 100) return "特化参数的键名不能超过 100 个字符。";
    if (seen.has(key)) return `特化参数的键名重复：${key}。`;
    seen.add(key);
  }
  if (parameters.length > 32) return "特化参数最多 32 项。";
  return "";
}

function formProblem(form: ModelForm): string {
  if (!form.model.trim()) return "模型名不能为空。";
  if (form.model.trim().length > 200) return "模型名不能超过 200 个字符。";
  return (
    describeIntervalError(form.temperature, "Temperature", 0, 2) ||
    describeIntervalError(form.top_p, "Top P", 0, 1) ||
    (form.max_context_tokens.trim() && parseInteger(form.max_context_tokens) === undefined
      ? "上下文上限需要是 1 以上的整数。"
      : "") ||
    (form.max_output_tokens.trim() && parseInteger(form.max_output_tokens) === undefined
      ? "输出上限需要是 1 以上的整数。"
      : "") ||
    parameterProblem(form.custom_parameters)
  );
}

/** 只把变化过的字段写进 patch：省略 = 不改动，显式 `null` = 清除（doc/api.md §5.10）。 */
function buildModelPatch(model: ModelRegistry, form: ModelForm): ModelRegistryUpdate {
  // 用 Record<string, unknown> 承接 `assignIfChanged` 的按字段比较，
  // 收尾再断言成 Update 类型；否则可选字段的联合类型会把 null 挡住。
  const patch: Record<string, unknown> = {};
  const trimmedName = form.name.trim();
  assignIfChanged(patch, "name", trimmedName || null, model.name);
  assignIfChanged(patch, "model", form.model.trim(), model.model);
  assignIfChanged(patch, "enabled", form.enabled, model.enabled);
  assignIfChanged(patch, "reasoning_type", form.reasoning_type, model.reasoning_type);
  assignIfChanged(
    patch,
    "temperature",
    form.temperature.trim() ? Number(form.temperature.trim()) : null,
    model.temperature,
  );
  assignIfChanged(patch, "top_p", form.top_p.trim() ? Number(form.top_p.trim()) : null, model.top_p);
  assignIfChanged(
    patch,
    "max_context_tokens",
    form.max_context_tokens.trim() ? Number(form.max_context_tokens.trim()) : null,
    model.max_context_tokens,
  );
  assignIfChanged(
    patch,
    "max_output_tokens",
    form.max_output_tokens.trim() ? Number(form.max_output_tokens.trim()) : null,
    model.max_output_tokens,
  );
  if (!sameParameters(form.custom_parameters, model.custom_parameters)) {
    patch.custom_parameters = form.custom_parameters.map((item) => ({ ...item, key: item.key.trim() }));
  }
  return patch as ModelRegistryUpdate;
}

/* -------------------------------------------------------------------------- */
/* 特化参数编辑器                                                              */
/* -------------------------------------------------------------------------- */

function CustomParameterEditor({
  value,
  onChange,
}: {
  value: CustomParameter[];
  onChange: (next: CustomParameter[]) => void;
}) {
  const update = (index: number, patch: Partial<CustomParameter>) => {
    onChange(value.map((item, at) => (at === index ? { ...item, ...patch } : item)));
  };

  return (
    <div className="cfg-params">
      <div className="cfg-params-head">
        <span>特化参数</span>
        <button
          type="button"
          className="cfg-quiet"
          onClick={() => onChange([...value, { key: "", value: "", type: "text" }])}
          disabled={value.length >= 32}
        >
          ＋ 添加参数
        </button>
      </div>
      {!value.length && (
        <p className="cfg-hint">
          没有额外参数。用于透传厂商专属字段（如 thinking_budget）。
        </p>
      )}
      {value.map((item, index) => (
        <div className="cfg-param-row" key={index}>
          <input
            className="cfg-param-key"
            value={item.key}
            onChange={(event) => update(index, { key: event.target.value })}
            placeholder="键名，例如 thinking_budget"
            autoComplete="off"
            spellCheck={false}
            aria-label={`特化参数 ${index + 1} 键名`}
          />
          <select
            value={item.type}
            onChange={(event) => update(index, { type: event.target.value as CustomParameterType })}
            aria-label={`特化参数 ${index + 1} 类型`}
          >
            {CUSTOM_PARAMETER_TYPES.map((type) => (
              <option value={type} key={type}>
                {type}
              </option>
            ))}
          </select>
          <input
            className="cfg-param-value"
            value={item.value}
            onChange={(event) => update(index, { value: event.target.value })}
            placeholder="值"
            autoComplete="off"
            spellCheck={false}
            aria-label={`特化参数 ${index + 1} 取值`}
          />
          <button
            type="button"
            className="cfg-icon-button"
            onClick={() => onChange(value.filter((_, at) => at !== index))}
            aria-label={`删除特化参数 ${index + 1}`}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 单条模型的特化调参（L2 卡片展开区）                                          */
/* -------------------------------------------------------------------------- */

function ModelTuningForm({
  model,
  onDone,
  onCancel,
}: {
  model: ModelRegistry;
  onDone: (message: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<ModelForm>(() => formFromModel(model));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  const problem = formProblem(form);

  /** 文本/数字类字段的统一 setter；`enabled` 与 `custom_parameters` 另走各自入口。 */
  const edit = (key: TextField) => (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const value = event.target.value;
    setForm((current) => ({ ...current, [key]: value }) as ModelForm);
    setNotice(null);
  };

  const submit = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    const patch = buildModelPatch(model, form);
    if (!Object.keys(patch).length) {
      setNotice({ tone: "bad", text: "没有需要保存的改动。" });
      return;
    }
    setSaving(true);
    try {
      await api.patchModelRegistry(model.id, patch);
      await onDone(`已保存 ${model.model} 的特化参数。`);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="cfg-tuning"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void submit();
      }}
      noValidate
    >
      <div className="cfg-tuning-head">
        <span>
          特化调参 <code>{model.model}</code>
        </span>
        <span className="cfg-model-meta">更新于 {formatTime(model.updated_at)}</span>
      </div>

      <div className="cfg-tuning-grid">
        <Field label="显示名" htmlFor={`m-name-${model.id}`} hint="留空则回退为模型名。">
          <input
            id={`m-name-${model.id}`}
            value={form.name}
            onChange={edit("name")}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="模型名" htmlFor={`m-model-${model.id}`} hint="发送给上游的 model 字段。">
          <input
            id={`m-model-${model.id}`}
            value={form.model}
            onChange={edit("model")}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="推理类型" htmlFor={`m-reasoning-${model.id}`} hint="决定推理参数族的组装方式。">
          <select
            id={`m-reasoning-${model.id}`}
            value={form.reasoning_type}
            onChange={edit("reasoning_type")}
          >
            {REASONING_TYPES.map((type) => (
              <option value={type} key={type}>
                {REASONING_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Temperature"
          htmlFor={`m-temp-${model.id}`}
          hint="0–2；留空表示未设置。"
          tone={describeIntervalError(form.temperature, "Temperature", 0, 2) ? "bad" : undefined}
        >
          <input
            id={`m-temp-${model.id}`}
            type="number"
            min={0}
            max={2}
            step={0.01}
            value={form.temperature}
            onChange={edit("temperature")}
          />
        </Field>
        <Field
          label="Top P"
          htmlFor={`m-topp-${model.id}`}
          hint="0–1；留空表示未设置。"
          tone={describeIntervalError(form.top_p, "Top P", 0, 1) ? "bad" : undefined}
        >
          <input
            id={`m-topp-${model.id}`}
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={form.top_p}
            onChange={edit("top_p")}
          />
        </Field>
        <Field
          label="上下文上限"
          htmlFor={`m-ctx-${model.id}`}
          hint="整数，≥1；留空表示未设置。"
          tone={form.max_context_tokens.trim() && parseInteger(form.max_context_tokens) === undefined ? "bad" : undefined}
        >
          <input
            id={`m-ctx-${model.id}`}
            type="number"
            min={1}
            step={1}
            value={form.max_context_tokens}
            onChange={edit("max_context_tokens")}
          />
        </Field>
        <Field
          label="输出上限"
          htmlFor={`m-out-${model.id}`}
          hint="整数，≥1；留空表示未设置。"
          tone={form.max_output_tokens.trim() && parseInteger(form.max_output_tokens) === undefined ? "bad" : undefined}
        >
          <input
            id={`m-out-${model.id}`}
            type="number"
            min={1}
            step={1}
            value={form.max_output_tokens}
            onChange={edit("max_output_tokens")}
          />
        </Field>
      </div>

      <CustomParameterEditor
        value={form.custom_parameters}
        onChange={(next) => {
          setForm((current) => ({ ...current, custom_parameters: next }));
          setNotice(null);
        }}
      />

      <div className="cfg-row-actions">
        <button type="submit" className="cfg-primary" disabled={saving || Boolean(problem)}>
          {saving ? "保存中…" : "保存特化参数"}
        </button>
        <button type="button" className="cfg-quiet" onClick={onCancel} disabled={saving}>
          收起
        </button>
        <NoticeBar notice={notice} />
      </div>
    </form>
  );
}

/* -------------------------------------------------------------------------- */
/* 手动登记模型                                                                */
/* -------------------------------------------------------------------------- */

function ModelCreateModal({
  providerId,
  onClose,
  onSaved,
}: {
  providerId: string;
  onClose: () => void;
  onSaved: (message: string) => Promise<void>;
}) {
  const [form, setForm] = useState<ModelForm>(() => ({
    name: "",
    model: "",
    enabled: true,
    reasoning_type: "none",
    temperature: "",
    top_p: "",
    max_context_tokens: "",
    max_output_tokens: "",
    custom_parameters: [],
  }));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  const problem = formProblem(form);

  const edit = (key: TextField) => (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((current) => ({ ...current, [key]: event.target.value }) as ModelForm);

  const submit = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    setSaving(true);
    try {
      const payload: ModelRegistryCreate = {
        provider_id: providerId,
        model: form.model.trim(),
        name: form.name.trim() || null,
        enabled: form.enabled,
        reasoning_type: form.reasoning_type as ReasoningType,
        temperature: form.temperature.trim() ? Number(form.temperature.trim()) : null,
        top_p: form.top_p.trim() ? Number(form.top_p.trim()) : null,
        max_context_tokens: form.max_context_tokens.trim() ? Number(form.max_context_tokens.trim()) : null,
        max_output_tokens: form.max_output_tokens.trim() ? Number(form.max_output_tokens.trim()) : null,
        custom_parameters: form.custom_parameters.map((item) => ({ ...item, key: item.key.trim() })),
      };
      const created = await api.createModelRegistry(payload);
      await onSaved(`已登记模型 ${created.model}。`);
      onClose();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "新增失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="手动登记模型"
      subtitle={`Provider：${providerId}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="cfg-primary" onClick={() => void submit()} disabled={saving || Boolean(problem)}>
            {saving ? "提交中…" : "登记模型"}
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
            取消
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      <div className="cfg-form-grid">
        <Field label="模型名" htmlFor="mc-model" hint="上游 model 字段；同一 Provider 内唯一。">
          <input
            id="mc-model"
            value={form.model}
            onChange={edit("model")}
            placeholder="例如 gpt-4o-mini"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="显示名" htmlFor="mc-name" hint="可选，仅用于界面展示。">
          <input id="mc-name" value={form.name} onChange={edit("name")} autoComplete="off" spellCheck={false} />
        </Field>
        <Field label="推理类型" htmlFor="mc-reasoning">
          <select id="mc-reasoning" value={form.reasoning_type} onChange={edit("reasoning_type")}>
            {REASONING_TYPES.map((type) => (
              <option value={type} key={type}>
                {REASONING_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Temperature" htmlFor="mc-temp" hint="0–2，留空表示未设置。">
          <input
            id="mc-temp"
            type="number"
            min={0}
            max={2}
            step={0.01}
            value={form.temperature}
            onChange={edit("temperature")}
          />
        </Field>
        <Field label="Top P" htmlFor="mc-topp" hint="0–1，留空表示未设置。">
          <input id="mc-topp" type="number" min={0} max={1} step={0.01} value={form.top_p} onChange={edit("top_p")} />
        </Field>
        <Field label="输出上限" htmlFor="mc-out" hint="整数 ≥1。">
          <input
            id="mc-out"
            type="number"
            min={1}
            step={1}
            value={form.max_output_tokens}
            onChange={edit("max_output_tokens")}
          />
        </Field>
        <Field label="上下文上限" htmlFor="mc-ctx" hint="整数 ≥1。">
          <input
            id="mc-ctx"
            type="number"
            min={1}
            step={1}
            value={form.max_context_tokens}
            onChange={edit("max_context_tokens")}
          />
        </Field>
      </div>
      <CustomParameterEditor
        value={form.custom_parameters}
        onChange={(next) => setForm((current) => ({ ...current, custom_parameters: next }))}
      />
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 批量引入                                                                    */
/* -------------------------------------------------------------------------- */

type ImportDefaults = {
  temperature: string;
  top_p: string;
  max_context_tokens: string;
  max_output_tokens: string;
  reasoning_type: string;
};

function BatchImportModal({
  provider,
  supportsDiscovery,
  preset,
  onClose,
  onImported,
}: {
  provider: ProviderRegistryDetail;
  supportsDiscovery: boolean;
  preset: ProviderPreset | null;
  onClose: () => void;
  onImported: (message: string) => Promise<void>;
}) {
  const [phase, setPhase] = useState<"idle" | "discovering" | "importing">("idle");
  const [remote, setRemote] = useState<ModelDiscovery | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [manualText, setManualText] = useState("");
  const [result, setResult] = useState<ModelBatchImportResult | null>(null);
  const [notice, setNotice] = useState<NoticeState>(null);
  const [defaults, setDefaults] = useState<ImportDefaults>({
    temperature: "",
    top_p: "",
    max_context_tokens: "",
    max_output_tokens: "",
    reasoning_type: "",
  });

  // 远端给的是「导入前」的快照；导入成功后父级会刷新 provider.models，
  // 这里并上它，使刚刚引入的条目立刻变成「已登记」并退出选择。
  const existing = useMemo(
    () => new Set([...(remote?.existing ?? []), ...provider.models.map((item) => item.model)]),
    [remote, provider.models],
  );

  const visible = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    const items = remote?.items ?? [];
    if (!keyword) return items;
    return items.filter(
      (item) => item.id.toLowerCase().includes(keyword) || item.name.toLowerCase().includes(keyword),
    );
  }, [remote, search]);

  const pendingSelection = useMemo(
    () => selected.filter((id) => !existing.has(id)),
    [selected, existing],
  );
  const manualPending = useMemo(
    () => parseLines(manualText).filter((name) => !existing.has(name)),
    [manualText, existing],
  );
  const pendingCount = new Set([...pendingSelection, ...manualPending]).size;

  const discover = async () => {
    setPhase("discovering");
    setNotice(null);
    try {
      const found = await api.discoverProviderModels(provider.id);
      const registered = new Set(found.existing);
      setRemote(found);
      setSelected(found.items.filter((item) => !registered.has(item.id)).map((item) => item.id));
      setNotice({
        tone: "info",
        text: `远端返回 ${found.total} 个模型，其中 ${found.existing.length} 个已登记。默认只勾选未登记的。`,
      });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "远端发现失败，可改用下方手动输入。") });
    } finally {
      setPhase("idle");
    }
  };

  const toggle = (id: string) => {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  };
  const selectAllNew = () =>
    setSelected((remote?.items ?? []).filter((item) => !existing.has(item.id)).map((item) => item.id));

  /**
   * 一个默认值都没填时返回 `undefined`（即整个 `defaults` 键缺席），而不是 `null`：
   * 这里「没有默认值」的语义是「不下发这一项」，缺省才是正确表达；
   * `null` 在本项目里另有含义（显式清除覆盖），不能混用。
   */
  const buildDefaults = (): ModelBatchImportRequest["defaults"] => {
    const payload: NonNullable<ModelBatchImportRequest["defaults"]> = {};
    if (defaults.temperature.trim()) payload.temperature = Number(defaults.temperature.trim());
    if (defaults.top_p.trim()) payload.top_p = Number(defaults.top_p.trim());
    if (defaults.max_context_tokens.trim()) payload.max_context_tokens = Number(defaults.max_context_tokens.trim());
    if (defaults.max_output_tokens.trim()) payload.max_output_tokens = Number(defaults.max_output_tokens.trim());
    if (defaults.reasoning_type) payload.reasoning_type = defaults.reasoning_type as ReasoningType;
    return Object.keys(payload).length ? payload : undefined;
  };

  const submit = async () => {
    const manual = parseLines(manualText).filter((name) => !existing.has(name));
    const models = Array.from(new Set([...pendingSelection, ...manual]));
    if (!models.length) {
      setNotice({ tone: "bad", text: "请至少选择一个模型，或手动输入模型名。" });
      return;
    }
    if (models.length > 200) {
      setNotice({ tone: "bad", text: "单次最多引入 200 条，请分批提交。" });
      return;
    }
    setPhase("importing");
    try {
      const response = await api.batchImportModels({
        provider_id: provider.id,
        models,
        name_prefix: "",
        enabled: true,
        defaults: buildDefaults(),
      });
      setResult(response);
      await onImported(
        `批量引入完成：新增 ${response.created.length} 条，跳过 ${response.skipped.length} 条。`,
      );
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "批量引入失败，请稍后重试。") });
    } finally {
      setPhase("idle");
    }
  };

  return (
    <Modal
      title="批量引入模型"
      subtitle={
        <>
          Provider <code>{provider.id}</code> · {preset?.label ?? provider.preset_type}
          {!supportsDiscovery && " · 该协议族不支持远端发现"}
        </>
      }
      wide
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            className="cfg-primary"
            onClick={() => void submit()}
            disabled={phase !== "idle" || pendingCount === 0}
          >
            {phase === "importing" ? "引入中…" : `引入所选（${pendingCount}）`}
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={phase !== "idle"}>
            {result ? "完成" : "取消"}
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      <section className="cfg-import-block">
        <div className="cfg-import-head">
          <span>1 · 取回远端清单</span>
          <button
            type="button"
            className="cfg-quiet"
            onClick={() => void discover()}
            disabled={phase !== "idle" || !supportsDiscovery}
            title={supportsDiscovery ? "向该 Provider 发起一次出站请求" : "该协议族不支持远端发现"}
          >
            {phase === "discovering" ? "拉取中…" : "从远端发现模型"}
          </button>
        </div>
        <p className="cfg-hint">
          发现请求由服务端发起（避免浏览器直连的 CORS 与凭据外泄），只在点击时触发，不会随页面加载自动调用。
        </p>
        {remote && (
          <>
            <div className="cfg-import-toolbar">
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="筛选模型名"
                aria-label="筛选远端模型"
                autoComplete="off"
              />
              <button type="button" className="cfg-quiet" onClick={selectAllNew}>
                全选未登记
              </button>
              <button type="button" className="cfg-quiet" onClick={() => setSelected([])}>
                清空选择
              </button>
              <span className="cfg-count">
                {visible.length} / {remote.total} 个 · 已选 {pendingCount}
              </span>
            </div>
            <div className="cfg-discover-list">
              {visible.map((item) => {
                const registered = existing.has(item.id);
                return (
                  <label className={`cfg-discover-item${registered ? " registered" : ""}`} key={item.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(item.id)}
                      disabled={registered}
                      onChange={() => toggle(item.id)}
                    />
                    <span className="cfg-discover-name">
                      <b>{item.name || item.id}</b>
                      {item.name && item.name !== item.id && <code>{item.id}</code>}
                    </span>
                    {item.owned_by && <span className="cfg-discover-owner">{item.owned_by}</span>}
                    {registered && <Chip tone="slate">已登记</Chip>}
                  </label>
                );
              })}
              {!visible.length && (
                <EmptyState title="远端没有可引入的模型" hint="可改用下方手动输入。" />
              )}
            </div>
          </>
        )}
      </section>

      <section className="cfg-import-block">
        <div className="cfg-import-head">
          <span>2 · 手动补充（每行一个模型名）</span>
        </div>
        <textarea
          rows={3}
          value={manualText}
          onChange={(event) => setManualText(event.target.value)}
          placeholder={"deepseek-chat\ndeepseek-reasoner"}
          spellCheck={false}
        />
      </section>

      <section className="cfg-import-block">
        <div className="cfg-import-head">
          <span>3 · 这一批新条目的默认值</span>
        </div>
        <p className="cfg-hint">
          留空表示不写入该字段；已存在的条目只计入「跳过」，不会被覆盖，因此重复提交是安全的。
        </p>
        <div className="cfg-form-grid">
          <Field label="Temperature" htmlFor="bi-temp" hint="0–2">
            <input
              id="bi-temp"
              type="number"
              min={0}
              max={2}
              step={0.01}
              value={defaults.temperature}
              onChange={(event) => setDefaults((current) => ({ ...current, temperature: event.target.value }))}
            />
          </Field>
          <Field label="Top P" htmlFor="bi-topp" hint="0–1">
            <input
              id="bi-topp"
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={defaults.top_p}
              onChange={(event) => setDefaults((current) => ({ ...current, top_p: event.target.value }))}
            />
          </Field>
          <Field label="上下文上限" htmlFor="bi-ctx" hint="整数 ≥1">
            <input
              id="bi-ctx"
              type="number"
              min={1}
              step={1}
              value={defaults.max_context_tokens}
              onChange={(event) =>
                setDefaults((current) => ({ ...current, max_context_tokens: event.target.value }))
              }
            />
          </Field>
          <Field label="输出上限" htmlFor="bi-out" hint="整数 ≥1">
            <input
              id="bi-out"
              type="number"
              min={1}
              step={1}
              value={defaults.max_output_tokens}
              onChange={(event) =>
                setDefaults((current) => ({ ...current, max_output_tokens: event.target.value }))
              }
            />
          </Field>
          <Field label="推理类型" htmlFor="bi-reasoning" hint="留空则不写入">
            <select
              id="bi-reasoning"
              value={defaults.reasoning_type}
              onChange={(event) => setDefaults((current) => ({ ...current, reasoning_type: event.target.value }))}
            >
              <option value="">不指定</option>
              {REASONING_TYPES.map((type) => (
                <option value={type} key={type}>
                  {REASONING_LABELS[type] ?? type}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </section>

      {result && (
        <section className="cfg-import-block">
          <div className="cfg-import-head">
            <span>结果</span>
          </div>
          <p className="cfg-hint">
            新增 {result.created.length} 条 · 跳过 {result.skipped.length} 条 · 请求 {result.total_requested} 条
          </p>
          {result.created.length > 0 && (
            <ul className="cfg-result-list">
              {result.created.slice(0, 12).map((id) => (
                <li key={id}>
                  <Chip tone="green">新增</Chip> <code>{id}</code>
                </li>
              ))}
              {result.created.length > 12 && <li>…以及另外 {result.created.length - 12} 条</li>}
            </ul>
          )}
          {result.skipped.length > 0 && (
            <ul className="cfg-result-list">
              {result.skipped.slice(0, 12).map((item) => (
                <li key={item.model}>
                  <Chip tone="amber">跳过</Chip> <code>{item.model}</code> <span>{item.reason}</span>
                </li>
              ))}
              {result.skipped.length > 12 && <li>…以及另外 {result.skipped.length - 12} 条</li>}
            </ul>
          )}
        </section>
      )}
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 模型区主体（嵌在 Provider 详情里）                                            */
/* -------------------------------------------------------------------------- */

export function ModelSection({
  provider,
  preset,
  onChanged,
  onNotice,
}: {
  provider: ProviderRegistryDetail;
  preset: ProviderPreset | null;
  onChanged: () => Promise<void>;
  onNotice: (notice: NoticeState) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const supportsDiscovery = preset ? preset.supports_model_discovery : true;

  const models = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    if (!keyword) return provider.models;
    return provider.models.filter(
      (item) => item.model.toLowerCase().includes(keyword) || (item.name ?? "").toLowerCase().includes(keyword),
    );
  }, [provider.models, filter]);

  const toggleEnabled = async (model: ModelRegistry) => {
    setBusyId(model.id);
    try {
      await api.patchModelRegistry(model.id, { enabled: !model.enabled });
      await onChanged();
    } catch (cause) {
      onNotice({ tone: "bad", text: describeError(cause, "切换启用状态失败。") });
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (model: ModelRegistry) => {
    setBusyId(model.id);
    try {
      await api.deleteModelRegistry(model.id);
      await onChanged();
      onNotice({ tone: "ok", text: `已删除 ${model.model}。` });
    } catch (cause) {
      onNotice({ tone: "bad", text: describeError(cause, "删除失败。") });
    } finally {
      setBusyId(null);
    }
  };

  const afterSave = useCallback(
    async (message: string) => {
      await onChanged();
      onNotice({ tone: "ok", text: message });
    },
    [onChanged, onNotice],
  );

  return (
    <section className="cfg-block" aria-label="模型注册表">
      <div className="cfg-block-head">
        <div>
          <h3>模型条目</h3>
          <p>
            共 {provider.models.length} 条，启用 {provider.models.filter((item) => item.enabled).length} 条
          </p>
        </div>
        <div className="cfg-row-actions">
          <button type="button" className="cfg-primary" onClick={() => setImportOpen(true)}>
            批量引入
          </button>
          <button type="button" className="cfg-quiet" onClick={() => setCreateOpen(true)}>
            手动登记
          </button>
        </div>
      </div>

      {provider.models.length > 0 && (
        <div className="cfg-import-toolbar">
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="筛选已登记模型"
            aria-label="筛选已登记模型"
            autoComplete="off"
          />
          <span className="cfg-count">{models.length} 条</span>
        </div>
      )}

      {!provider.models.length && (
        <EmptyState
          title="还没有登记模型"
          hint={<>点「批量引入」从远端一次性拉取，或「手动登记」逐条添加。</>}
        />
      )}

      <div className="cfg-model-list">
        {models.map((model) => (
          <article
            className={`cfg-model-row${model.enabled ? "" : " off"}${expanded === model.id ? " open" : ""}`}
            key={model.id}
          >
            {/* 整卡即展开/收起的手柄（用户 2026-09-16：去掉「特化调参」按钮）。
                内部的开关与删除按钮各自阻断冒泡，避免误触发展开。 */}
            <div
              className="cfg-model-main"
              role="button"
              tabIndex={0}
              aria-expanded={expanded === model.id}
              onClick={() => setExpanded((current) => (current === model.id ? null : model.id))}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setExpanded((current) => (current === model.id ? null : model.id));
                }
              }}
            >
              <span
                className="cfg-model-switch"
                onClick={(event) => event.stopPropagation()}
                onKeyDown={(event) => event.stopPropagation()}
              >
                <Switch
                  checked={model.enabled}
                  disabled={busyId === model.id}
                  onChange={() => void toggleEnabled(model)}
                  label={`${model.enabled ? "停用" : "启用"} ${model.model}`}
                />
              </span>
              <div className="cfg-model-id">
                <b>{model.name || model.model}</b>
                <code>{model.model}</code>
              </div>
              <div className="cfg-model-badges">
                {model.reasoning_type !== "none" && (
                  <Chip tone={REASONING_TINT[model.reasoning_type] ?? "slate"}>
                    {REASONING_LABELS[model.reasoning_type] ?? model.reasoning_type}
                  </Chip>
                )}
                {model.temperature !== null && <Chip tone="blue">T {model.temperature}</Chip>}
                {model.top_p !== null && <Chip tone="blue">P {model.top_p}</Chip>}
                {model.max_output_tokens !== null && <Chip tone="indigo">输出 {model.max_output_tokens}</Chip>}
                {model.max_context_tokens !== null && <Chip tone="indigo">上下文 {model.max_context_tokens}</Chip>}
                {model.custom_parameters.length > 0 && (
                  <Chip tone="purple" title={model.custom_parameters.map((item) => item.key).join("、")}>
                    特化 {model.custom_parameters.length}
                  </Chip>
                )}
              </div>
              <div className="cfg-row-actions">
                <InlineConfirm
                  label={`删除模型条目「${model.model}」`}
                  confirmLabel="确认删除"
                  triggerClassName="cfg-quiet danger"
                  triggerLabel={`删除模型条目：${model.model}`}
                  triggerTitle="删除后，引用它的角色会退化为未绑定"
                  disabled={busyId === model.id}
                  onConfirm={() => void remove(model)}
                >
                  删除
                </InlineConfirm>
                <ChevronDown size={15} className="cfg-model-chevron" aria-hidden="true" />
              </div>
            </div>
            {expanded === model.id && (
              <ModelTuningForm
                model={model}
                onDone={async (message) => {
                  await afterSave(message);
                }}
                onCancel={() => setExpanded(null)}
              />
            )}
          </article>
        ))}
      </div>

      {importOpen && (
        <BatchImportModal
          provider={provider}
          preset={preset}
          supportsDiscovery={supportsDiscovery}
          onClose={() => setImportOpen(false)}
          onImported={afterSave}
        />
      )}
      {createOpen && (
        <ModelCreateModal
          providerId={provider.id}
          onClose={() => setCreateOpen(false)}
          onSaved={afterSave}
        />
      )}
    </section>
  );
}

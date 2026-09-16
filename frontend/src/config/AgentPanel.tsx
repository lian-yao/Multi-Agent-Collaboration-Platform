import { useCallback, useEffect, useMemo, useState } from "react";
import { Cpu, Plus, SlidersHorizontal, Thermometer, Trash2 } from "lucide-react";
import { api } from "../api/client";
import { REASONING_TYPES, type Agent, type AgentConfigList, type AvailableModel, type ReasoningType } from "../types/api";
import {
  Chip,
  EmptyState,
  Field,
  Modal,
  NoticeBar,
  assignIfChanged,
  describeError,
  describeIntervalError,
  parseInteger,
  type NoticeState,
} from "./shared";
import { InlineConfirm } from "../components/InlineConfirm";

const REASONING_LABELS: Record<string, string> = {
  none: "无推理",
  openai: "OpenAI 推理",
  gemini: "Gemini 推理",
  anthropic: "Anthropic 推理",
};

const STATUS_LABELS: Record<string, string> = {
  idle: "空闲",
  running: "执行中",
  error: "异常",
  disabled: "已停用",
};

interface AgentForm {
  llm_model_id: string;
  model: string;
  temperature: string;
  top_p: string;
  max_output_tokens: string;
  reasoning_type: ReasoningType;
}

/** 六个可覆盖字段里，除 `reasoning_type` 外都是文本/数字输入。 */
type TextField = Exclude<keyof AgentForm, "reasoning_type">;

const asText = (value: number | null): string => (value === null ? "" : String(value));

function formFromAgent(agent: Agent): AgentForm {
  return {
    llm_model_id: agent.llm_model_id ?? "",
    model: agent.model,
    temperature: asText(agent.temperature),
    top_p: asText(agent.top_p),
    max_output_tokens: asText(agent.max_output_tokens),
    reasoning_type: agent.reasoning_type as ReasoningType,
  };
}

function formProblem(form: AgentForm): string {
  if (form.model.trim().length > 200) return "模型名不能超过 200 个字符。";
  return (
    describeIntervalError(form.temperature, "Temperature", 0, 2) ||
    describeIntervalError(form.top_p, "Top P", 0, 1) ||
    (form.max_output_tokens.trim() && parseInteger(form.max_output_tokens) === undefined
      ? "输出上限需要是 1 以上的整数。"
      : "")
  );
}

/**
 * 只提交被改动的字段；清空即显式 `null` = 清除该层覆盖、回退下一层配置（doc/api.md §5.7）。
 */
function buildAgentPatch(agent: Agent, form: AgentForm): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  assignIfChanged(patch, "llm_model_id", form.llm_model_id || null, agent.llm_model_id);
  assignIfChanged(patch, "model", form.model.trim() || null, agent.model);
  assignIfChanged(
    patch,
    "temperature",
    form.temperature.trim() ? Number(form.temperature.trim()) : null,
    agent.temperature,
  );
  assignIfChanged(patch, "top_p", form.top_p.trim() ? Number(form.top_p.trim()) : null, agent.top_p);
  assignIfChanged(
    patch,
    "max_output_tokens",
    form.max_output_tokens.trim() ? Number(form.max_output_tokens.trim()) : null,
    agent.max_output_tokens,
  );
  assignIfChanged(patch, "reasoning_type", form.reasoning_type, agent.reasoning_type);
  return patch;
}

const monogramOf = (agent: Agent): string => agent.name.slice(0, 1);

/**
 * 单个角色卡片（网格里一行多个）。
 *
 * 卡片主体点击打开配置弹窗；自定义角色（`builtin=false`）右上角提供删除按钮，
 * 内置流水线角色不显示删除入口（删除会破坏固定三步流水线）。
 *
 * 导出是为了让 `frontend/rendercheck/config-smoke.tsx` 能用显式 props 驱动它。
 */
export function AgentRoleCard({
  agent,
  active,
  onOpen,
  onDelete,
}: {
  agent: Agent;
  active: boolean;
  onOpen: () => void;
  onDelete?: () => void;
}) {
  const overrides = agent.override_keys.length;

  return (
    <div className={`cfg-agent-card${active ? " active" : ""}${!agent.enabled ? " disabled" : ""}`}>
      <button
        type="button"
        className="cfg-agent-open"
        onClick={onOpen}
        title={`配置「${agent.name}」的模型绑定与参数`}
      >
        <span className="cfg-agent-top">
          <span className={`cfg-monogram cfg-agent-avatar tint-${active ? "green" : "blue"}`} aria-hidden="true">
            {monogramOf(agent)}
          </span>
          <span className="cfg-agent-title">
            <span className="cfg-agent-name">
              <b>{agent.name}</b>
              {active && <em className="cfg-agent-live">当前阶段</em>}
              {!agent.enabled && <em className="cfg-agent-off">已停用</em>}
            </span>
            <small>
              {agent.role} · {STATUS_LABELS[agent.status] ?? agent.status}
            </small>
          </span>
          <SlidersHorizontal size={14} className="cfg-agent-more" aria-hidden="true" />
        </span>

        <span className="cfg-agent-meta">
          <span className="cfg-agent-meta-item">
            <Cpu size={12} aria-hidden="true" />
            {agent.model}
            {agent.provider_name ? ` · ${agent.provider_name}` : ` · ${agent.provider}`}
          </span>
          <span className="cfg-agent-meta-item">
            <Thermometer size={12} aria-hidden="true" />T {agent.temperature}
          </span>
          {overrides > 0 ? (
            <Chip tone="amber">覆盖 {overrides} 项</Chip>
          ) : (
            <Chip tone="slate">无覆盖</Chip>
          )}
          {agent.builtin && <Chip tone="blue">内置</Chip>}
        </span>
      </button>

      {onDelete && (
        <InlineConfirm
          label={`删除自定义角色「${agent.name}」`}
          confirmLabel="删除"
          triggerClassName="cfg-agent-delete"
          triggerLabel={`删除角色 ${agent.name}`}
          triggerTitle="删除该自定义角色，其模型覆盖配置会一并清除"
          slotClassName="cfg-agent-delete-slot"
          size="sm"
          onConfirm={() => onDelete?.()}
        >
          <Trash2 size={14} aria-hidden="true" />
        </InlineConfirm>
      )}
    </div>
  );
}

/**
 * 角色配置弹窗（点卡片打开）。
 *
 * 六个覆盖字段 + 「层次来源」标记：高亮 = 本层覆盖，未高亮 = 回退到
 * 「环境配置 → 默认路由模型 → legacy 五列」。清空某个数字/文本输入并保存 = 显式 `null`
 * = 清除该字段的覆盖；「清除全部覆盖」一次性发 6 个 `null`。
 *
 * 同样是 props 驱动的导出，供无浏览器渲染冒烟直接挂载。
 */
export function AgentTuningPanel({
  agent,
  availableModels,
  active,
  onSaved,
  onClose,
}: {
  agent: Agent;
  availableModels: AvailableModel[];
  active: boolean;
  /** 保存成功后调用：父组件据此重新拉取角色清单并提示。 */
  onSaved: (message: string) => Promise<void>;
  onClose: () => void;
}) {
  const [form, setForm] = useState<AgentForm>(() => formFromAgent(agent));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  useEffect(() => {
    setForm(formFromAgent(agent));
    setNotice(null);
  }, [agent]);

  const overridden = useMemo(() => new Set(agent.override_keys), [agent.override_keys]);
  const problem = formProblem(form);
  const patch = buildAgentPatch(agent, form);
  const dirty = Object.keys(patch).length > 0;
  const boundModel = availableModels.find((item) => item.id === form.llm_model_id);
  const boundMissing = form.llm_model_id !== "" && !boundModel;

  const edit = (key: TextField, value: string) => {
    setForm((current) => ({ ...current, [key]: value }) as AgentForm);
    setNotice(null);
  };

  const save = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    if (!dirty) {
      setNotice({ tone: "bad", text: "没有需要保存的改动。" });
      return;
    }
    setSaving(true);
    try {
      await api.patchAgentConfig(agent.id, patch);
      await onSaved(`已保存角色「${agent.name}」的覆盖值，下一次阶段执行即生效。`);
      onClose();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  /** 清除该角色的全部覆盖；二次确认由按钮上的 InlineConfirm 给出，这里只负责执行。 */
  const clearAll = async () => {
    if (!agent.override_keys.length) {
      setNotice({ tone: "bad", text: "该角色当前没有覆盖值。" });
      return;
    }
    setSaving(true);
    try {
      await api.patchAgentConfig(agent.id, {
        llm_model_id: null,
        model: null,
        temperature: null,
        top_p: null,
        max_output_tokens: null,
        reasoning_type: null,
      });
      await onSaved(`已清除「${agent.name}」的全部覆盖。`);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "清除失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  const flag = (key: string, label: string) => (
    <span
      className={`cfg-flag${overridden.has(key) ? " on" : ""}`}
      title={overridden.has(key) ? "该字段已被显式覆盖" : "该值来自下一层配置"}
    >
      {label}
    </span>
  );

  return (
    <Modal
      title={agent.name}
      subtitle={
        <>
          生效 <code>{agent.model}</code>
          {agent.provider_name ? ` · ${agent.provider_name}` : ` · ${agent.provider}`}
          {agent.llm_model_id ? ` · 绑定 ${agent.llm_model_id}` : ""}
          {agent.builtin ? " · 内置角色" : " · 自定义角色"}
        </>
      }
      wide
      onClose={onClose}
      footer={
        <>
          <button type="submit" form="agent-tuning-form" className="cfg-primary" disabled={saving || Boolean(problem)}>
            {saving ? "保存中…" : "保存覆盖"}
          </button>
          <InlineConfirm
            label={`清除「${agent.name}」的全部覆盖`}
            confirmLabel="确认清除"
            question="回退到环境配置与默认路由"
            triggerClassName="cfg-quiet"
            triggerLabel={`清除「${agent.name}」的全部覆盖`}
            triggerTitle="清除该角色的全部覆盖值，回退到环境配置与默认路由"
            disabled={saving}
            onConfirm={() => void clearAll()}
          >
            清除全部覆盖
          </InlineConfirm>
          <button
            type="button"
            className="cfg-quiet"
            onClick={() => {
              setForm(formFromAgent(agent));
              setNotice(null);
            }}
            disabled={saving}
          >
            还原表单
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
            取消
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      <div className="cfg-agent-modal-head">
        <span className={`cfg-monogram cfg-agent-avatar tint-${active ? "green" : "blue"}`} aria-hidden="true">
          {monogramOf(agent)}
        </span>
        <div>
          <Chip tone="blue">{agent.role}</Chip>
          <Chip tone={agent.status === "running" ? "green" : "slate"}>
            {STATUS_LABELS[agent.status] ?? agent.status}
          </Chip>
          {active && <Chip tone="green">当前阶段</Chip>}
          {agent.builtin ? <Chip tone="blue">内置</Chip> : <Chip tone="slate">自定义</Chip>}
          {!agent.enabled && <Chip tone="amber">已停用</Chip>}
        </div>
      </div>

      <form
        id="agent-tuning-form"
        className="cfg-form-grid"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
        noValidate
      >
        <Field
          label="绑定注册表模型"
          htmlFor={`a-model-${agent.id}`}
          hint={
            boundMissing
              ? "当前绑定的条目已不存在（退化为未绑定），重新选择或留空即可。"
              : "选定后由该条目的 Provider 决定端点、凭据与特化参数。"
          }
          tone={boundMissing ? "bad" : undefined}
          wide
        >
          <select
            id={`a-model-${agent.id}`}
            value={form.llm_model_id}
            onChange={(event) => edit("llm_model_id", event.target.value)}
          >
            <option value="">不绑定（走默认路由 / 环境配置）</option>
            {boundMissing && (
              <option value={form.llm_model_id}>{form.llm_model_id}（条目已删除）</option>
            )}
            {availableModels.map((model) => (
              <option key={model.id} value={model.id}>
                {model.provider_id} · {model.name || model.model}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="模型名"
          htmlFor={`a-name-${agent.id}`}
          hint={overridden.has("model") ? "已覆盖。留空并保存 = 清除覆盖。" : "回退值；编辑后成为覆盖。"}
        >
          <input
            id={`a-name-${agent.id}`}
            value={form.model}
            onChange={(event) => edit("model", event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>

        <Field
          label="推理类型"
          htmlFor={`a-reasoning-${agent.id}`}
          hint={overridden.has("reasoning_type") ? "已覆盖。" : "回退值。"}
        >
          <select
            id={`a-reasoning-${agent.id}`}
            value={form.reasoning_type}
            onChange={(event) => {
              setForm((current) => ({ ...current, reasoning_type: event.target.value as ReasoningType }));
              setNotice(null);
            }}
          >
            {REASONING_TYPES.map((type) => (
              <option key={type} value={type}>
                {REASONING_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Temperature"
          htmlFor={`a-temp-${agent.id}`}
          hint={overridden.has("temperature") ? "已覆盖（0–2）。" : "回退值；0–2。"}
          tone={describeIntervalError(form.temperature, "Temperature", 0, 2) ? "bad" : undefined}
        >
          <input
            id={`a-temp-${agent.id}`}
            type="number"
            min={0}
            max={2}
            step={0.01}
            value={form.temperature}
            onChange={(event) => edit("temperature", event.target.value)}
          />
        </Field>

        <Field
          label="Top P"
          htmlFor={`a-topp-${agent.id}`}
          hint={overridden.has("top_p") ? "已覆盖（0–1）。" : "回退值；0–1。"}
          tone={describeIntervalError(form.top_p, "Top P", 0, 1) ? "bad" : undefined}
        >
          <input
            id={`a-topp-${agent.id}`}
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={form.top_p}
            onChange={(event) => edit("top_p", event.target.value)}
          />
        </Field>

        <Field
          label="输出上限"
          htmlFor={`a-out-${agent.id}`}
          hint={overridden.has("max_output_tokens") ? "已覆盖（整数 ≥1）。" : "回退值；整数 ≥1。"}
          tone={form.max_output_tokens.trim() && parseInteger(form.max_output_tokens) === undefined ? "bad" : undefined}
        >
          <input
            id={`a-out-${agent.id}`}
            type="number"
            min={1}
            step={1}
            value={form.max_output_tokens}
            onChange={(event) => edit("max_output_tokens", event.target.value)}
          />
        </Field>

        <div className="cfg-field cfg-field-wide">
          <span className="cfg-label">层次来源</span>
          <div className="cfg-flags">
            {flag("llm_model_id", "注册表绑定")}
            {flag("model", "模型名")}
            {flag("temperature", "Temperature")}
            {flag("top_p", "Top P")}
            {flag("max_output_tokens", "输出上限")}
            {flag("reasoning_type", "推理类型")}
          </div>
          <small>高亮 = 本层覆盖；未高亮 = 回退到「环境配置 → 默认路由模型 → legacy 五列」。</small>
        </div>
      </form>
    </Modal>
  );
}

/**
 * 「Agent 团队」页的角色路由分区（`doc/api.md` §5.7）。
 *
 * 一次 `GET /api/v1/config/agents` 取回全部角色与可选模型；点开方块后在网格下方编辑，
 * 同一时刻只编辑一个角色（`selectedId`），保存后重新拉取，让「生效值」与覆盖标记同步。
 */
/**
 * 新建角色弹窗。
 *
 * 只登记角色目录条目（id/name/role/description/system_prompt），模型绑定与参数
 * 在创建后再点开卡片单独调。内置角色 id（collector/analyst/reporter）不可复用。
 */
function AgentCreateModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (message: string) => Promise<void>;
}) {
  const [form, setForm] = useState({ id: "", name: "", role: "", description: "" });
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  const problem =
    (!form.id.trim() ? "ID 不能为空。" : "") ||
    (form.id.length > 50 ? "ID 不能超过 50 个字符。" : "") ||
    (!/^[A-Za-z0-9._-]+$/.test(form.id) ? "ID 只允许字母、数字与 . _ - 。" : "") ||
    (!form.name.trim() ? "名称不能为空。" : "") ||
    (!form.role.trim() ? "角色键不能为空。" : "");

  const edit = (key: keyof typeof form, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
    setNotice(null);
  };

  const submit = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    setSaving(true);
    try {
      await api.createAgentRegistry({
        id: form.id.trim(),
        name: form.name.trim(),
        role: form.role.trim(),
        description: form.description.trim() || null,
      });
      await onCreated(`已新建角色「${form.name.trim()}」，点开卡片即可绑定模型与调参。`);
      onClose();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "新建失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="新建角色"
      subtitle="登记一个自定义角色条目；创建后再点开卡片绑定模型与参数。"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="cfg-primary" onClick={() => void submit()} disabled={saving || Boolean(problem)}>
            {saving ? "登记中…" : "登记角色"}
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
            取消
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      <div className="cfg-form-grid">
        <Field label="ID" htmlFor="ag-id" hint="1–50 字符，仅 A-Za-z0-9._-；内置角色 id 不可复用。" tone={form.id.trim() && problem ? "bad" : undefined}>
          <input
            id="ag-id"
            value={form.id}
            onChange={(event) => edit("id", event.target.value)}
            placeholder="summarizer"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="名称" htmlFor="ag-name" hint="界面展示名，如「摘要 Agent」。">
          <input id="ag-name" value={form.name} onChange={(event) => edit("name", event.target.value)} autoComplete="off" />
        </Field>
        <Field label="角色键" htmlFor="ag-role" hint="角色语义标识（自由文本），如 summarizer。">
          <input id="ag-role" value={form.role} onChange={(event) => edit("role", event.target.value)} autoComplete="off" spellCheck={false} />
        </Field>
        <Field label="职责说明" htmlFor="ag-desc" hint="可选；卡片与详情里的说明文字。" wide>
          <textarea
            id="ag-desc"
            rows={3}
            value={form.description}
            onChange={(event) => edit("description", event.target.value)}
            placeholder="收集、归纳并输出摘要。"
          />
        </Field>
      </div>
    </Modal>
  );
}

export function AgentPanel({ activeAgentId }: { activeAgentId?: string }) {
  const [data, setData] = useState<AgentConfigList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api.listAgentConfigs();
      setData(value);
      setError("");
    } catch (cause) {
      setError(describeError(cause, "Agent 配置读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const afterSave = async (message: string) => {
    await load();
    setNotice({ tone: "ok", text: message });
  };

  const remove = async (agent: Agent) => {
    setBusy(true);
    try {
      await api.deleteAgentRegistry(agent.id);
      await load();
      setNotice({ tone: "ok", text: `已删除角色 ${agent.id}。` });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "删除失败，请稍后重试。") });
    } finally {
      setBusy(false);
    }
  };

  const editing = data?.items.find((agent) => agent.id === editingId) ?? null;

  return (
    <div className="cfg-stack">
      <section className="cfg-block">
        <div className="cfg-block-head">
          <div>
            <h3>角色目录</h3>
            <p>
              {data
                ? `${data.items.length} 个角色 · 可绑定 ${data.available_models.length} 个注册表模型`
                : "正在读取角色目录"}
            </p>
          </div>
          <div className="cfg-row-actions">
            <button type="button" className="cfg-primary" onClick={() => setCreateOpen(true)} disabled={busy}>
              <Plus size={13} aria-hidden="true" />
              新建角色
            </button>
            <button type="button" className="cfg-quiet" onClick={() => void load()} disabled={loading}>
              重新读取
            </button>
          </div>
        </div>
        <p className="cfg-hint">
          点开一张角色卡片在弹窗里配置它的模型绑定与参数；内置三角色（信息收集 / 数据分析 / 报告生成）
          不可删除，自定义角色可自由增删。覆盖值按角色粒度保存，下次阶段执行即生效。
        </p>
        {error && (
          <p role="alert" className="cfg-alert">
            {error}
          </p>
        )}
        {loading && !data && <p className="cfg-hint">加载中…</p>}
        {data && !data.items.length && (
          <EmptyState title="没有可配置的角色" hint="点击「新建角色」登记一个，或确认角色目录已加载。" />
        )}
        {data && !data.available_models.length && data.items.length > 0 && (
          <p className="cfg-hint">
            模型注册表里还没有启用的条目；先在「工具与配置 → Provider」里引入模型，这里才能绑定。
          </p>
        )}
      </section>

      {data && data.items.length > 0 && (
        <div className="cfg-agent-grid">
          {data.items.map((agent) => (
            <AgentRoleCard
              key={agent.id}
              agent={agent}
              active={agent.id === activeAgentId}
              onOpen={() => {
                setEditingId(agent.id);
                setNotice(null);
              }}
              onDelete={agent.builtin ? undefined : () => void remove(agent)}
            />
          ))}
        </div>
      )}

      <NoticeBar notice={notice} />

      {editing && (
        <AgentTuningPanel
          agent={editing}
          availableModels={data?.available_models ?? []}
          active={editing.id === activeAgentId}
          onSaved={afterSave}
          onClose={() => setEditingId(null)}
        />
      )}

      {createOpen && (
        <AgentCreateModal onClose={() => setCreateOpen(false)} onCreated={afterSave} />
      )}
    </div>
  );
}

export default AgentPanel;

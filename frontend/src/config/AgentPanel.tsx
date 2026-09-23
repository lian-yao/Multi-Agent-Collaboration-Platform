import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cpu, Plus, SlidersHorizontal, Sparkles, Thermometer, Trash2 } from "lucide-react";
import { api } from "../api/client";
import {
  REASONING_TYPES,
  type Agent,
  type AgentConfigList,
  type AgentProfileUpdate,
  type AvailableModel,
  type ReasoningType,
  type Tool,
} from "../types/api";
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
import {
  AGENT_ICON_GLYPHS,
  AGENT_ICON_KEYS,
  AgentGlyph,
  type AgentIconKey,
} from "../components/AgentGlyph";
import {
  AgentToolsPanel,
  allToolNames,
  buildToolCatalogGroups,
  type ToolCatalogGroup,
} from "./AgentTools";

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

/** 弹窗副路由的三个分区（ADR-036 的界面落点）：人设 / 模型参数 / 工具授权。 */
type AgentModalTab = "profile" | "tuning" | "tools";

const AGENT_MODAL_TABS: readonly (readonly [AgentModalTab, string, string])[] = [
  ["profile", "设定", "名称、职责说明、系统提示词与启停（图标点头像改）"],
  ["tuning", "调度", "模型绑定与调参覆盖"],
  ["tools", "工具", "这个角色能用的工具"],
];

interface AgentProfileForm {
  name: string;
  /** "" = 自动（按角色键 / 名称推断）；其余值是 `AGENT_ICON_KEYS` 里的键。 */
  icon: string;
  description: string;
  system_prompt: string;
  enabled: boolean;
}

function profileFormFromAgent(agent: Agent): AgentProfileForm {
  return {
    name: agent.name,
    icon: agent.icon ?? "",
    description: agent.description ?? "",
    system_prompt: agent.system_prompt ?? "",
    enabled: agent.enabled,
  };
}

/**
 * 目录字段 patch：只提交被改动的键，空串归一成 `null`（= 清空该列）。
 * 走的是 `/profile` 端点（ADR-036），与模型参数覆盖的 `/agents/{id}` 分开——
 * 两组字段对 `null` 的后果不同，混在一份 patch 里没法解释。
 */
function buildProfilePatch(
  agent: Agent,
  form: AgentProfileForm,
): AgentProfileUpdate {
  const patch: AgentProfileUpdate = {};
  const name = form.name.trim();
  if (name && name !== agent.name) patch.name = name;
  if ((form.icon || null) !== (agent.icon ?? null)) patch.icon = form.icon || null;
  const description = form.description.trim() || null;
  if (description !== (agent.description ?? null)) patch.description = description;
  const systemPrompt = form.system_prompt.trim() || null;
  if (systemPrompt !== (agent.system_prompt ?? null)) patch.system_prompt = systemPrompt;
  if (form.enabled !== agent.enabled) patch.enabled = form.enabled;
  return patch;
}

/**
 * 白名单的比较**不看顺序**。
 *
 * 这里不能用 `shared.tsx` 的 `sameStrings`（逐元素比）：后端存的是它自己的顺序，
 * 用户在弹窗里勾选的顺序是另一套，两者表达的是同一份授权。逐元素比会让「打开弹窗、
 * 什么都没动、点保存」也发出一条 `tool_names` —— 用户没改配置却看到「已保存」。
 */
function sameToolNameSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const known = new Set(left);
  return right.every((name) => known.has(name));
}

/** 按目录顺序重排白名单；目录里没有的名字（孤儿）保持原有先后接在后面。 */
function orderToolNames(
  selected: readonly string[],
  groups: readonly ToolCatalogGroup[],
): string[] {
  const chosen = new Set(selected);
  const known = new Set<string>();
  const ordered: string[] = [];
  for (const name of allToolNames(groups)) {
    known.add(name);
    if (chosen.has(name)) ordered.push(name);
  }
  for (const name of selected) {
    if (!known.has(name)) ordered.push(name);
  }
  return ordered;
}

/**
 * 把「不受限 / 按名单」两态翻译成 PATCH 里的 `tool_names`（doc/api.md §5.7）。
 *
 * 三态语义在这里收口：`null` = 清除白名单、回到不受限，`[]` = 显式什么都不给，
 * 非空数组 = 白名单。没有改动时返回空对象，让后端继续按「字段未提供 = 不改动」处理。
 */
function buildToolPatch(
  original: string[] | null,
  restricted: boolean,
  selected: readonly string[],
  groups: readonly ToolCatalogGroup[],
): Record<string, unknown> {
  if (!restricted) {
    return original === null ? {} : { tool_names: null };
  }
  const next = orderToolNames(selected, groups);
  if (original !== null && sameToolNameSet(original, next)) return {};
  return { tool_names: next };
}

// 头像位原先在这里取显示名首字当 monogram（`agent.name.slice(0, 1)`）。现在改由
// `AgentGlyph` 按 role 解析角色图标 —— 单字中文方块认不出是谁，而且同一个 Agent
// 在协作画布上画的是机器人，两处对不上。解析口径集中在 `components/AgentGlyph.tsx`。

/**
 * 卡片上的工具授权摘要。
 *
 * 必须画在卡片上，不能只留在弹窗里：工具授权是「这个角色能做什么」的一部分，
 * 与模型、温度同级。只在弹窗里显示，等于用户不逐个点开就不知道哪个角色被收紧了。
 */
/**
 * 把白名单读成可判别的三态。
 *
 * `?? null` 不是顺手加的防御写法，而是**必要的**：`tool_names` 是后加的字段，
 * 旧响应、`rendercheck` 的预览种子、滚动发布期间仍在跑的老进程，都不会给这个键。
 * 字段缺失与 `null` 是同一件事（未配置），但直接读会变成 `undefined.length` ——
 * 渲染期 TypeError，整块角色网格一起白。
 */
const toolScope = (agent: Agent): string[] | null => agent.tool_names ?? null;

function ToolScopeChip({ agent }: { agent: Agent }) {
  const tools = toolScope(agent);
  if (tools === null) {
    return (
      <Chip tone="slate" title="未配置白名单：该角色可以用工具目录里的全部工具">
        工具不受限
      </Chip>
    );
  }
  if (tools.length === 0) {
    return (
      <Chip tone="rose" title="白名单为空：该角色没有任何工具可用（会话附件工具除外）">
        工具 0 个
      </Chip>
    );
  }
  return (
    <Chip tone="amber" title={`只放行这 ${tools.length} 个工具：${tools.join("、")}`}>
      工具 {tools.length} 个
    </Chip>
  );
}

/**
 * 单个角色卡片（网格里一行多个）。
 *
 * 卡片主体点击打开配置弹窗；自定义角色（`builtin=false`）右上角提供删除按钮，
 * 内置流水线角色不显示删除入口（删除会破坏固定链）。
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
          <span className={`cfg-monogram cfg-agent-avatar tint-${active ? "green" : "blue"}`}>
            <AgentGlyph
              role={agent.role}
              name={agent.name}
              icon={agent.icon ?? undefined}
              size={17}
            />
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
          {agent.system_prompt ? <Chip tone="green">有提示词</Chip> : null}
          <ToolScopeChip agent={agent} />
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
  toolGroups,
  toolsError,
  onReloadTools,
  onSaved,
  onClose,
}: {
  agent: Agent;
  availableModels: AvailableModel[];
  active: boolean;
  /** 工具目录（§5.3 的目录 + §5.11 的紧凑目录合成）；父组件取一次后复用。 */
  toolGroups: ToolCatalogGroup[];
  /** 工具目录读取失败的原因；非空时这一块整体只读。 */
  toolsError: string;
  /** 重新读取工具目录（失败后原地重试，不必关掉弹窗再打开）。 */
  onReloadTools: () => void;
  /** 保存成功后调用：父组件据此重新拉取角色清单并提示。 */
  onSaved: (message: string) => Promise<void>;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<AgentModalTab>("profile");
  const [form, setForm] = useState<AgentForm>(() => formFromAgent(agent));
  const [profileForm, setProfileForm] = useState<AgentProfileForm>(() =>
    profileFormFromAgent(agent),
  );
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  /** 头像浮层里的图标选择器（不常驻在「设定」里，见 `.cfg-agent-icon-popover`）。 */
  const [iconPickerOpen, setIconPickerOpen] = useState(false);
  const iconPickerRef = useRef<HTMLSpanElement>(null);
  const [toolsRestricted, setToolsRestricted] = useState(toolScope(agent) !== null);
  const [toolsSelected, setToolsSelected] = useState<string[]>(() => [
    ...(toolScope(agent) ?? []),
  ]);

  useEffect(() => {
    setForm(formFromAgent(agent));
    setProfileForm(profileFormFromAgent(agent));
    setToolsRestricted(toolScope(agent) !== null);
    setToolsSelected([...(toolScope(agent) ?? [])]);
    setNotice(null);
    // 切换角色时回到「设定」：三个分区共用一个弹窗，上一角色停在工具页，
    // 下一角色也跟着停在工具页会让「为什么参数没显示」变成悬案。
    setTab("profile");
  }, [agent]);

  // 图标浮层：点外面或按 Esc 收起。
  // Esc 必须在**捕获阶段**拦下并 `stopPropagation` —— `Modal` 自己在 window 上挂了
  // 冒泡期的 Esc 关闭整个弹窗；不拦就会「只想收起浮层，结果连弹窗带草稿一起关了」。
  useEffect(() => {
    if (!iconPickerOpen) return;
    const onMouseDown = (event: MouseEvent) => {
      if (!iconPickerRef.current?.contains(event.target as Node)) setIconPickerOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setIconPickerOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [iconPickerOpen]);

  /** 选完即收起，并把「没有需要保存的改动」这类上一次的提示清掉。 */
  const pickIcon = (icon: string) => {
    setProfileForm((current) => ({ ...current, icon }));
    setIconPickerOpen(false);
    setNotice(null);
  };

  const overridden = useMemo(() => new Set(agent.override_keys), [agent.override_keys]);
  const profilePatch = useMemo(
    () => buildProfilePatch(agent, profileForm),
    [agent, profileForm],
  );
  const profileProblem =
    (!profileForm.name.trim() ? "名称不能为空。" : "") ||
    (profileForm.name.length > 100 ? "名称不能超过 100 个字符。" : "") ||
    (profileForm.system_prompt.length > 8000
      ? "系统提示词不能超过 8000 个字符。"
      : "");
  const problem = formProblem(form) || profileProblem;
  const patch = buildAgentPatch(agent, form);
  const toolPatch = useMemo(
    () => buildToolPatch(toolScope(agent), toolsRestricted, toolsSelected, toolGroups),
    [agent, toolsRestricted, toolsSelected, toolGroups],
  );
  // 「有没有改动」必须把三份 patch 一起看：只比模型字段，会让「只动了人设或工具」
  // 被判成「没有需要保存的改动」。
  const dirty =
    Object.keys(profilePatch).length > 0 ||
    Object.keys(patch).length > 0 ||
    Object.keys(toolPatch).length > 0;
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
      // 目录字段与覆盖值分属两个端点（ADR-036）：改了哪份提交哪份，
      // 都改了就各提交一次——一次保存不混两种 `null` 语义。
      if (Object.keys(profilePatch).length > 0) {
        await api.patchAgentProfile(agent.id, profilePatch);
      }
      if (Object.keys(patch).length > 0 || Object.keys(toolPatch).length > 0) {
        await api.patchAgentConfig(agent.id, { ...patch, ...toolPatch });
      }
      await onSaved(`已保存角色「${agent.name}」的配置，下一次阶段执行即生效。`);
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
        // 白名单也在 `override_keys` 里（后端 `OVERRIDE_FIELDS` 含它），所以
        // 「清除全部覆盖」不能漏掉它 —— 漏了会留下「覆盖标记已清空、工具却仍被名单
        // 卡着」的矛盾状态，而且用户没有任何入口能看出来。
        tool_names: null,
      });
      setToolsRestricted(false);
      setToolsSelected([]);
      await onSaved(`已清除「${agent.name}」的全部覆盖。`);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "清除失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  const toggleTool = (name: string, next: boolean) => {
    setToolsSelected((current) =>
      next ? [...current, name] : current.filter((item) => item !== name),
    );
    setNotice(null);
  };

  const changeToolMode = (next: boolean) => {
    setToolsRestricted(next);
    // 从「不受限」切到「按名单」时用当前目录预填。反过来（预填空名单）会让一次切换
    // 就悄悄收掉全部工具，而用户当时的意图通常只是「想收紧一点」。
    setToolsSelected((current) =>
      next && current.length === 0 ? allToolNames(toolGroups) : current,
    );
    setNotice(null);
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
        {/* 头像即图标入口：点它开浮层挑图标。画的是**待保存**的 `profileForm.icon`
            而不是 `agent.icon`，否则挑完看不见变化。 */}
        <span className="cfg-agent-avatar-slot" ref={iconPickerRef}>
          <button
            type="button"
            className={`cfg-monogram cfg-agent-avatar tint-${active ? "green" : "blue"}`}
            aria-haspopup="dialog"
            aria-expanded={iconPickerOpen}
            aria-label={`更换「${agent.name}」的图标`}
            title="更换图标"
            onClick={() => setIconPickerOpen((open) => !open)}
          >
            <AgentGlyph
              role={agent.role}
              name={agent.name}
              icon={profileForm.icon || null}
              size={17}
            />
          </button>
          {iconPickerOpen && (
            <div
              className="cfg-agent-icon-popover"
              role="dialog"
              aria-label="选择角色图标"
            >
              <span className="cfg-label">图标</span>
              <div className="cfg-agent-icons">
                <button
                  type="button"
                  title="自动：按角色键与名称推断"
                  aria-label="自动选择图标"
                  className={`cfg-agent-icon${profileForm.icon === "" ? " active" : ""}`}
                  onClick={() => pickIcon("")}
                >
                  <Sparkles size={15} aria-hidden="true" />
                </button>
                {AGENT_ICON_KEYS.map((key: AgentIconKey) => {
                  const Icon = AGENT_ICON_GLYPHS[key];
                  return (
                    <button
                      key={key}
                      type="button"
                      title={key}
                      aria-label={`图标 ${key}`}
                      className={`cfg-agent-icon${profileForm.icon === key ? " active" : ""}`}
                      onClick={() => pickIcon(key)}
                    >
                      <Icon size={15} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
              <small>
                选「自动」时按角色键 / 名称推断；显式选定的图标在卡片、画布与记录页同样生效。
              </small>
            </div>
          )}
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

      <div className="cfg-agent-tabs" role="tablist" aria-label="角色配置分区">
        {AGENT_MODAL_TABS.map(([key, label, title]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            title={title}
            className={`cfg-agent-tab${tab === key ? " active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {/* —— 设定：角色目录字段（/profile 端点，ADR-036）。用 hidden 而不是条件挂载：
          三个分区共享一份保存动作，而保存按钮在 footer、靠 form="agent-tuning-form"
          关联到「调度」那份 <form> —— 条件卸载会让「在设定页点保存」直接失效。
          ⚠️ hidden 要靠 config.css 的 `[hidden]{display:none!important}` 才生效：
          `.cfg-form-grid` 的 `display:grid` 会压过 UA 样式表的 `[hidden]`，那条规则别删。 —— */}
      <section hidden={tab !== "profile"} className="cfg-form-grid" aria-label="角色设定">
        <Field
          label="名称"
          htmlFor={`a-title-${agent.id}`}
          hint="画布与卡片上的展示名。"
        >
          <input
            id={`a-title-${agent.id}`}
            value={profileForm.name}
            onChange={(event) =>
              setProfileForm((current) => ({ ...current, name: event.target.value }))
            }
            autoComplete="off"
          />
        </Field>

        <Field
          label="参与动态调度"
          htmlFor={`a-en-${agent.id}`}
          hint="停用后主 Agent 不再把任务自动派给它；固定链不受影响。"
        >
          <select
            id={`a-en-${agent.id}`}
            value={profileForm.enabled ? "on" : "off"}
            onChange={(event) =>
              setProfileForm((current) => ({
                ...current,
                enabled: event.target.value === "on",
              }))
            }
          >
            <option value="on">启用</option>
            <option value="off">停用</option>
          </select>
        </Field>

        <Field
          label="职责说明"
          htmlFor={`a-desc-${agent.id}`}
          hint="卡片与详情里的说明文字；同时是 planner 候选清单里对它的介绍。"
          wide
        >
          <textarea
            id={`a-desc-${agent.id}`}
            rows={2}
            value={profileForm.description}
            onChange={(event) =>
              setProfileForm((current) => ({
                ...current,
                description: event.target.value,
              }))
            }
          />
        </Field>

        <Field
          label="系统提示词（system prompt）"
          htmlFor={`a-prompt-${agent.id}`}
          hint="主 Agent 调度这个角色时喂给它的人设；留空 = 内置角色用默认人设，自定义角色用通用兜底。≤ 8000 字符。"
          wide
        >
          <textarea
            id={`a-prompt-${agent.id}`}
            rows={6}
            value={profileForm.system_prompt}
            onChange={(event) =>
              setProfileForm((current) => ({
                ...current,
                system_prompt: event.target.value,
              }))
            }
            spellCheck={false}
          />
        </Field>
      </section>

      <form
        id="agent-tuning-form"
        hidden={tab !== "tuning"}
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
            {flag("tool_names", "工具白名单")}
          </div>
          <small>
            高亮 = 本层覆盖；未高亮 = 回退到「环境配置 → 默认路由模型 → legacy 五列」。
            工具白名单只有「配置 / 未配置」两态，不参与这层回退。
          </small>
        </div>
      </form>

      <section hidden={tab !== "tools"} aria-label="角色工具授权">
      <AgentToolsPanel
        groups={toolGroups}
        groupsError={toolsError}
        restricted={toolsRestricted}
        selected={toolsSelected}
        onToggle={toggleTool}
        onRestrictedChange={changeToolMode}
        onSelectAll={() => {
          setToolsSelected(allToolNames(toolGroups));
          setNotice(null);
        }}
        onSelectNone={() => {
          setToolsSelected([]);
          setNotice(null);
        }}
        onReload={onReloadTools}
      />
      </section>
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

/** `/api/v1/tools` 的 `page_size` 上限。 */
const TOOL_PAGE_SIZE = 100;
const TOOL_PAGE_LIMIT = 20;

/**
 * 取回**完整**工具目录。
 *
 * 不用接口默认的 20 条：目录被截断的后果不是「少显示几行」，而是界面上的「全选」
 * 并不是全部 —— 保存出来的白名单会缺工具，而缺口只在执行期才暴露（工具没被调用）。
 * 翻页到底仍装不下就出声，不静默截断。
 */
async function collectToolCatalog(): Promise<Tool[]> {
  const all: Tool[] = [];
  for (let page = 1; page <= TOOL_PAGE_LIMIT; page += 1) {
    const chunk = await api.getTools(page, TOOL_PAGE_SIZE);
    all.push(...chunk.items);
    if (all.length >= chunk.total) return all;
  }
  throw new Error(
    `工具目录超过 ${TOOL_PAGE_LIMIT * TOOL_PAGE_SIZE} 条，请先在工具页收敛登记数量。`,
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
  const [toolGroups, setToolGroups] = useState<ToolCatalogGroup[]>([]);
  const [toolsError, setToolsError] = useState("");
  const [toolsLoaded, setToolsLoaded] = useState(false);

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

  const loadTools = useCallback(async () => {
    try {
      // MCP 紧凑目录拿不到时降级成「只有内置工具」：它只影响分组标题，
      // 不该因为它失败就让整块工具授权变成只读。
      const [catalog, mcp] = await Promise.all([
        collectToolCatalog(),
        api.listMcpCompactTools().catch(() => null),
      ]);
      setToolGroups(buildToolCatalogGroups(catalog, mcp));
      setToolsError("");
    } catch (cause) {
      setToolGroups([]);
      setToolsError(describeError(cause, "工具目录读取失败。"));
    }
  }, []);

  // 惰性加载：只在第一次打开角色弹窗时取目录，之后复用。
  useEffect(() => {
    if (editingId !== null && !toolsLoaded) {
      setToolsLoaded(true);
      void loadTools();
    }
  }, [editingId, toolsLoaded, loadTools]);

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
          点开一张角色卡片，在弹窗的「设定 / 调度 / 工具」三个分区里分别配置人设与图标、模型绑定与参数、
          工具授权。设定里停用后，主 Agent 不再把任务自动派给它（固定链不受影响）；
          内置三角色不可删除。所有改动按角色粒度保存，下次阶段执行即生效。
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
          toolGroups={toolGroups}
          toolsError={toolsError}
          onReloadTools={() => void loadTools()}
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

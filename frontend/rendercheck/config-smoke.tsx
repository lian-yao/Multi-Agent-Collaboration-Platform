/**
 * 配置页渲染冒烟检查（ADR-017，`frontend/src/config/`）。
 *
 * 前端没有测试框架，「类型检查 + 构建」抓不到渲染期崩溃、解构错字段、空态分支写错
 * 这类问题。本文件用 `react-dom/server` 把配置页整棵树在没有浏览器的前提下渲染一遍，
 * 不引入任何新依赖（只用项目已有的 react / esbuild）。
 *
 * 运行（**必须在 `frontend/` 下执行**，版式断言会按 cwd 读 `src/**.css`）：
 *
 * ```bash
 * node_modules/.bin/esbuild rendercheck/config-smoke.tsx --bundle --platform=node \
 *   --format=cjs --jsx=automatic --loader:.css=empty --outfile="$TEMP/config-smoke.cjs" \
 *   && node "$TEMP/config-smoke.cjs"
 * ```
 *
 * 退出码 0 = 全通过。**覆盖边界**：只跑不依赖 effects 的渲染路径（组件树挂载、JSX、
 * hooks 顺序、空态、以及用显式 props 驱动的模型区块），外加四组读文件的静态断言
 * （样式表版式、副路由语义、全站禁用原生对话框、自定义 Provider 加号图标与模型上下文查表）。
 * 真正需要点击与 effects 的交互路径仍要人工在浏览器里过一遍——见 `doc/testing.md` §3.3。
 *
 * 注意：角色路由面板挂在「Agent 团队」页、采样面板挂在「任务记录」页，都不在
 * `RuntimeConfig` 里，所以这里单独挂载一次，避免换页后新位置无人验证。
 */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentPanel, AgentRoleCard, AgentTuningPanel } from "../src/config/AgentPanel";
import { RuntimeConfig } from "../src/config/ConfigPage";
import { ModelSection } from "../src/config/ModelSection";
import { RuntimeSampling } from "../src/records/Inspection";
import { RecordsPage } from "../src/records/RecordsPage";
import { PageTabs } from "../src/components/PageTabs";
import { InlineConfirm, InlineConfirmBar } from "../src/components/InlineConfirm";
import { ProviderMark } from "../src/config/providerIcons";
import { resolveKnownContextTokens } from "../src/config/modelCapabilities";
import { draftProblem, draftToPayload, parseMcpImport, type McpImportDraft } from "../src/config/mcpConfig";
import { entriesToRecord, recordToEntries, samePairs } from "../src/config/KeyValueFields";
import { McpImportModal } from "../src/config/McpImportModal";
import { SandboxBoundary } from "../src/config/SandboxPanel";
import type { Agent, ProviderRegistryDetail, SandboxStatus } from "../src/types/api";

(globalThis as Record<string, unknown>).fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ({ items: [], total: 0, categories: [] }),
});

const results: [boolean, string][] = [];
const check = (label: string, condition: boolean, detail = "") =>
  results.push([condition, condition ? label : `${label}  →  ${detail}`]);

/* ---- 整页挂载（数据未到达的初始态） ---- */
let page = "";
try {
  page = renderToStaticMarkup(<RuntimeConfig />);
  check("配置页整体可静态渲染", page.length > 200, `长度 ${page.length}`);
} catch (cause) {
  check("配置页整体可静态渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("渲染出 4 个分区入口", ["Provider", "默认路由", "MCP 工具", "执行边界"].every((label) => page.includes(label)), page.slice(0, 300));
check(
  "配置页不再携带已迁走的分区",
  !page.includes("运行采样") && !page.includes("Agent 角色"),
  "「运行采样」应属任务记录页、「Agent 角色」应属 Agent 团队页",
);
check("默认分区为 Provider", page.includes("Provider") && page.includes("新建"));
check("页面标题与副标题就位", page.includes("工具与配置"));
check("标题带 CONFIGURATION 眉标", page.includes("CONFIGURATION"));
check(
  "副路由使用统一的 ui-tabs（不再是 cfg-tabs）",
  page.includes('class="ui-tabs"') && page.includes('role="tablist"') && !page.includes('class="cfg-tabs'),
  "配置页与任务记录页必须共用 components/PageTabs",
);
check(
  "副路由带 aria-selected 与 tab 角色",
  (page.match(/role="tab"/g) ?? []).length === 4 && page.includes('aria-selected="true"'),
  String((page.match(/role="tab"/g) ?? []).length),
);
check(
  "分区释义随选中项切换（不再是写死的副标题）",
  page.includes("多端点登记"),
  page.slice(0, 400),
);

/* ---- 两个已迁走的面板：仍要能挂载（防止换页后一渲染就崩） ---- */

let teamPage = "";
try {
  teamPage = renderToStaticMarkup(<AgentPanel activeAgentId="planner" />);
  check("Agent 团队页的角色面板可渲染", teamPage.length > 200, `长度 ${teamPage.length}`);
} catch (cause) {
  check("Agent 团队页的角色面板可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("角色面板用「角色目录」措辞", teamPage.includes("角色目录"));

/* ---- 单个角色方块（props 驱动）：摘要要齐，表单不在卡片里 ---- */

const roleAgent: Agent = {
  id: "analyst",
  name: "数据分析 Agent",
  role: "analyst",
  model: "gpt-5.5",
  provider: "openai",
  provider_name: null,
  llm_model_id: "main:gpt-5.5",
  temperature: 0.2,
  top_p: null,
  max_output_tokens: null,
  reasoning_type: "none",
  status: "running",
  override_keys: ["temperature"],
  builtin: true,
  description: null,
  enabled: true,
};

let card = "";
try {
  card = renderToStaticMarkup(
    <AgentRoleCard agent={roleAgent} active onOpen={() => undefined} />,
  );
  check("角色方块可渲染", card.length > 200, `长度 ${card.length}`);
} catch (cause) {
  check("角色方块可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("方块显示角色名与生效模型", card.includes("数据分析 Agent") && card.includes("gpt-5.5"));
check("方块标出当前阶段", card.includes("当前阶段"));
check("方块标出覆盖项数", card.includes("覆盖 1 项"));
check("方块是整块可点的按钮", card.includes("<button") && card.includes("配置「数据分析 Agent」"));
check(
  "表单不在方块里",
  !card.includes("清除全部覆盖") && !card.includes("层次来源") && !card.includes("绑定与调参"),
  "表单应放进弹窗，不在卡片网格里",
);

/* ---- 角色配置面板（props 驱动）：表单在这里 ---- */

let tuning = "";
try {
  tuning = renderToStaticMarkup(
    <AgentTuningPanel
      agent={roleAgent}
      availableModels={[
        { id: "main:gpt-5.5", provider_id: "main", model: "gpt-5.5", name: "GPT 5.5", enabled: true },
      ]}
      active
      onSaved={async () => undefined}
      onClose={() => undefined}
    />,
  );
  check("角色配置面板可渲染", tuning.length > 200, `长度 ${tuning.length}`);
} catch (cause) {
  check("角色配置面板可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check(
  "面板带出六个覆盖字段与层次来源",
  ["绑定注册表模型", "模型名", "推理类型", "Temperature", "Top P", "输出上限", "层次来源"].every((label) =>
    tuning.includes(label),
  ),
  tuning.slice(0, 200),
);
check("面板标出已绑定模型与已覆盖字段", tuning.includes("main · GPT 5.5") && tuning.includes("cfg-flag on"));
check("面板有保存与清除入口", tuning.includes("保存覆盖") && tuning.includes("清除全部覆盖"));

let historyPage = "";
try {
  historyPage = renderToStaticMarkup(<RuntimeSampling workflow={null} />);
  check("任务记录页的采样面板可渲染", historyPage.includes("全局指标采样"), historyPage.slice(0, 200));
} catch (cause) {
  check("任务记录页的采样面板可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

/* ---- 任务记录页：三分区副路由 + 运行记录空态 ---- */

let records = "";
try {
  records = renderToStaticMarkup(
    <RecordsPage
      tab="runs"
      onTabChange={() => undefined}
      workflow={null}
      messageCount={0}
      currentTaskTitle=""
      sessionId={null}
      onOpenRun={() => undefined}
      onOpenSession={() => undefined}
      onDeleteSession={async () => undefined}
    />,
  );
  check("任务记录页可静态渲染", records.length > 200, `长度 ${records.length}`);
} catch (cause) {
  check("任务记录页可静态渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check(
  "任务记录页渲染出四个分区入口",
  ["运行记录", "工具调用", "指标采样", "历史会话"].every((label) => records.includes(label)),
  records.slice(0, 300),
);
check(
  "任务记录页副路由与配置页同构",
  records.includes('class="ui-tabs"') && records.includes('role="tablist"') && records.includes('role="tab"'),
);
check("运行记录标题与分区释义就位", records.includes("RUN RECORDS") && records.includes("当前任务的整体执行与终态"));
check(
  "页头有当前任务徽标与历史入口",
  records.includes("当前任务") && records.includes("历史会话"),
  records.slice(0, 400),
);
check(
  "没有任务时给出空态而不是空白",
  records.includes("还没有运行记录"),
  records.slice(0, 400),
);

/* ---- 副路由组件本身：左对齐容器 + 键盘可达 ---- */

let tabsHtml = "";
try {
  tabsHtml = renderToStaticMarkup(
    <PageTabs
      tabs={[
        { id: "a", label: "甲" },
        { id: "b", label: "乙" },
      ]}
      active="a"
      onChange={() => undefined}
    />,
  );
  check("PageTabs 可渲染", tabsHtml.includes('class="ui-tabs"'), tabsHtml.slice(0, 200));
} catch (cause) {
  check("PageTabs 可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("PageTabs 用 tablist/tab 语义", tabsHtml.includes('role="tablist"') && (tabsHtml.match(/role="tab"/g) ?? []).length === 2);
check("PageTabs 只有选中项可被 Tab 键停留", (tabsHtml.match(/tabindex="0"/g) ?? []).length === 1);
check("PageTabs inline 变体可切换样式类", renderToStaticMarkup(
  <PageTabs variant="inline" tabs={[{ id: "x", label: "X" }]} active="x" onChange={() => undefined} />,
).includes("ui-tabs-inline"));

/* ---- 版式回归：页面级容器不再整页居中（用户明确提出的问题） ---- */

try {
  const styles = readFileSync(join(process.cwd(), "src", "styles.css"), "utf8");
  const config = readFileSync(join(process.cwd(), "src", "config", "config.css"), "utf8");
  const centered = (css: string) => /\.config-page\s*\{[^}]*margin:\s*0\s+auto/.test(css);
  check(
    "配置页与记录页不再整页居中",
    !centered(styles) && !centered(config),
    "`.config-page` 自身不应有 `max-width` + `margin:0 auto`；限宽居中只落在直接子元素上",
  );
  check(
    "副路由容器只占内容宽度",
    /\.ui-tabs\s*\{[^}]*width:\s*fit-content/.test(readFileSync(join(process.cwd(), "src", "components", "page-tabs.css"), "utf8")),
    "`.ui-tabs` 需要 width:fit-content，否则会撑满一行",
  );
  check(
    "整页共用一条限宽居中的内容轴（标题、副路由与内容对齐）",
    /\.config-page\s*>\s*\*\s*\{[^}]*max-width:\s*1180px[^}]*margin-inline:\s*auto/.test(styles) &&
      /\.config-page\s*>\s*\.ui-tabs\s*\{[^}]*width:\s*fit-content/.test(styles),
    "`.config-page > *` 需要 max-width:1180px + margin-inline:auto，且 `.config-page > .ui-tabs` 保持 fit-content",
  );
} catch (cause) {
  check("读得到样式表以校验版式", false, cause instanceof Error ? cause.message : String(cause));
}

/* ---- 有数据的模型区块（props 驱动，不依赖 effects） ---- */
const detail: ProviderRegistryDetail = {
  id: "p1",
  name: "桩端点",
  preset_type: "openai-compatible",
  api_type: "openai-compatible",
  base_url: "http://host.docker.internal:49411/v1",
  api_key_configured: true,
  custom_headers: { "X-Org": "research" },
  additional_settings: {},
  enabled: true,
  model_count: 1,
  created_at: "2026-09-15T08:00:00Z",
  updated_at: "2026-09-15T08:00:00Z",
  updated_by: null,
  models: [
    {
      id: "p1:stub-alpha",
      provider_id: "p1",
      model: "stub-alpha",
      name: "Stub Alpha",
      enabled: true,
      reasoning_type: "openai",
      temperature: 0.2,
      top_p: 0.9,
      max_context_tokens: 128000,
      max_output_tokens: 2048,
      custom_parameters: [{ key: "thinking_budget", value: "2048", type: "number" }],
      created_at: "2026-09-15T08:00:00Z",
      updated_at: "2026-09-15T08:00:00Z",
      updated_by: null,
    },
    {
      id: "p1:stub-beta",
      provider_id: "p1",
      model: "stub-beta",
      name: null,
      enabled: false,
      reasoning_type: "none",
      temperature: null,
      top_p: null,
      max_context_tokens: null,
      max_output_tokens: null,
      custom_parameters: [],
      created_at: null,
      updated_at: null,
      updated_by: null,
    },
  ],
};

let section = "";
try {
  section = renderToStaticMarkup(
    <ModelSection
      provider={detail}
      preset={{
        preset_type: "openai-compatible",
        label: "OpenAI 兼容（自定义）",
        monogram: "自定义",
        tint: "slate",
        category: "gateway",
        default_api_type: "openai-compatible",
        supported_api_types: ["openai-compatible"],
        default_base_url: "",
        requires_api_key: true,
        api_key_url: null,
        supports_model_discovery: true,
      }}
      onChanged={async () => undefined}
      onNotice={() => undefined}
    />,
  );
  check("模型区块可渲染", section.length > 200, `长度 ${section.length}`);
} catch (cause) {
  check("模型区块可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("渲染出两条模型", section.includes("Stub Alpha") && section.includes("stub-beta"));
check("渲染出特化徽标", section.includes("OpenAI 推理") && section.includes("特化 1") && section.includes("上下文 128000"));
check("渲染出启用/停用开关", (section.match(/role="switch"/g) ?? []).length >= 2, String((section.match(/role="switch"/g) ?? []).length));
check("渲染出批量引入入口", section.includes("批量引入") && section.includes("手动登记"));
check("模型行是整卡展开手柄且不再有独立调参按钮", section.includes("aria-expanded") && !section.includes("特化调参") && section.includes("cfg-model-chevron"));
check("模型行不再渲染无效的能力徽标", !section.includes("能力") && !section.includes("图像"));

/* ---- 危险操作的行内二次确认 + 全站禁用原生对话框 ---- */

const trigger = renderToStaticMarkup(
  <InlineConfirm
    label="删除会话「演示」"
    triggerClassName="cfg-quiet danger"
    triggerLabel="删除会话：演示"
    slotClassName="record-run-delete-slot"
    size="sm"
    onConfirm={() => undefined}
  >
    删除
  </InlineConfirm>,
);
check(
  "未点击时只渲染触发按钮，不渲染确认条",
  trigger.includes("cfg-quiet danger") && !trigger.includes("ui-confirm-yes") && !trigger.includes("取消"),
  trigger,
);
check(
  "宿主 slot 类名落在外层（各区域靠它复用绝对定位）",
  trigger.includes("ui-confirm-slot") && trigger.includes("record-run-delete-slot"),
  trigger,
);

const bar = renderToStaticMarkup(
  <InlineConfirmBar
    question="仍要强制级联删除？"
    confirmLabel="强制删除"
    onConfirm={() => undefined}
    onCancel={() => undefined}
  />,
);
check(
  "确认条带问句 + 确认 + 取消，且确认按钮不是原生对话框",
  bar.includes("仍要强制级联删除？") && bar.includes("强制删除") && bar.includes("取消"),
  bar,
);
check(
  "确认条语义正确（role=group + 两个 button，无 confirm/alert）",
  bar.includes('role="group"') && bar.includes('class="ui-confirm-yes"') && bar.includes('class="ui-confirm-no"'),
  bar,
);

// 读文件断言：确认条容器要和触发按钮**同形**——圆角 6px（`.sidebar-user-item-delete`
// 与 `.cfg-quiet` 同款）、不画自己的描边（否则按钮自带的边会和容器边叠成「框套框」，
// 用户 2026-09-16 反馈过），间距压到 2px。
const confirmCss = readFileSync(
  join(process.cwd(), "src", "components", "inline-confirm.css"),
  "utf8",
);
const confirmRule = /\.ui-confirm\s*\{([^}]*)\}/.exec(confirmCss)?.[1] ?? "";
check(
  "确认条容器与触发按钮同形（圆角 6px、无描边、间距 2px）",
  /border-radius:\s*6px/.test(confirmRule) &&
    /border:\s*0/.test(confirmRule) &&
    /padding:\s*2px/.test(confirmRule) &&
    /gap:\s*2px/.test(confirmRule),
  confirmRule.trim() || "没读到 .ui-confirm 规则",
);

// 读文件断言：原生对话框由宿主提供，沙箱 iframe（无 allow-modals）、浏览器
// 「阻止此页面创建更多对话框」、Electron/CEF 外壳都会屏蔽它；被屏蔽时 confirm()
// 不弹窗、直接返回 false，挂在返回值上的删除逻辑就会静默失效（点了没反应）。
const banned = /window\.(confirm|alert|prompt)\s*\(/;
const srcRoot = join(process.cwd(), "src");
const offenders: string[] = [];
for (const rel of readdirSync(srcRoot, { recursive: true }) as string[]) {
  if (!/\.(ts|tsx)$/.test(rel)) continue;
  if (banned.test(readFileSync(join(srcRoot, rel), "utf8"))) offenders.push(rel);
}
check(
  "全站不再使用 window.confirm / alert / prompt",
  offenders.length === 0,
  `仍在使用：${offenders.join(", ")}`,
);

// 「自定义」Provider 的图标位必须是加号，而不是把中文「自定义」当 monogram 塞进方块
// （三字会缩到很小、且和品牌白底方块不同形）。品牌预设仍走品牌标。
const customMark = renderToStaticMarkup(
  <ProviderMark presetType="openai-compatible" label="自定义" size={30} />,
);
const brandMark = renderToStaticMarkup(
  <ProviderMark presetType="openai" label="OA" size={30} />,
);
check(
  "自定义 Provider 用加号占位（不渲染「自定义」文字）",
  customMark.includes("cfg-mark-add") && !customMark.includes("自定义"),
  customMark.slice(0, 160),
);
check(
  "品牌预设仍渲染品牌标而不是加号",
  brandMark.includes("cfg-mark") && !brandMark.includes("cfg-mark-add"),
  brandMark.slice(0, 120),
);

// 常见模型的上下文窗口自动识别：查表在 modelCapabilities.ts，接线在 ModelSection 的两个入口
// （「手动登记模型」与「特化调参」）。文案一致性由 describeContextHint 统一保证。
const capabilities = readFileSync(
  join(process.cwd(), "src", "config", "modelCapabilities.ts"),
  "utf8",
);
check(
  "常见模型上下文表与查询函数就位",
  /KNOWN_CONTEXT_TOKENS/.test(capabilities) && /export function resolveKnownContextTokens/.test(capabilities),
  capabilities.slice(0, 120),
);
check(
  "上下文窗口按模型名归一化命中（含日期后缀回退）",
  resolveKnownContextTokens("openai/gpt-4o") === 128000 &&
    resolveKnownContextTokens("claude-sonnet-4-5-20250929") === 200000 &&
    resolveKnownContextTokens("gemini-2.5-pro") === 1048576 &&
    resolveKnownContextTokens("某个没听过的模型") === null,
  `gpt-4o=${resolveKnownContextTokens("openai/gpt-4o")} sonnet=${resolveKnownContextTokens("claude-sonnet-4-5-20250929")} 未知=${resolveKnownContextTokens("某个没听过的模型")}`,
);
const modelSource = readFileSync(
  join(process.cwd(), "src", "config", "ModelSection.tsx"),
  "utf8",
);
check(
  "两个模型入口都接了自动识别且手改后停手",
  (modelSource.match(/resolveKnownContextTokens\(/g) ?? []).length >= 2 &&
    (modelSource.match(/contextTouched/g) ?? []).length >= 4,
  `resolveKnownContextTokens x${(modelSource.match(/resolveKnownContextTokens\(/g) ?? []).length} / contextTouched x${(modelSource.match(/contextTouched/g) ?? []).length}`,
);

/* ---- MCP：粘贴导入的解析、归一化与校验（mcpConfig.ts / KeyValueFields.tsx） ---- */
// MCP 生态里每个客户端都有自己一份 JSON 约定，导入前必须先归一化；下面按四种输入形态各测
// 一遍，并锁住两条约定：表单与导入共用同一份校验，请求体只带该传输用得上的字段。
const baseDraft: McpImportDraft = {
  id: "x",
  name: "x",
  transport: "stdio",
  command: "npx",
  args: [],
  env: {},
  cwd: "",
  url: "",
  headers: {},
};
const firstDraft = (text: string): McpImportDraft => parseMcpImport(text).drafts[0];

const claudeStyle = parseMcpImport(
  JSON.stringify({
    mcpServers: {
      filesystem: { command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"] },
      notion: { type: "http", url: "https://mcp.example.com/mcp", headers: { Authorization: "Bearer x" } },
    },
  }),
);
check(
  "mcpServers 映射：逐条解析，并按 command/url 推断传输",
  claudeStyle.shape === "mcpServers-map" &&
    claudeStyle.issues.length === 0 &&
    claudeStyle.drafts.length === 2 &&
    claudeStyle.drafts[0].id === "filesystem" &&
    claudeStyle.drafts[0].transport === "stdio" &&
    claudeStyle.drafts[0].args.length === 3 &&
    claudeStyle.drafts[1].id === "notion" &&
    claudeStyle.drafts[1].transport === "http" &&
    claudeStyle.drafts[1].headers.Authorization === "Bearer x",
  JSON.stringify(claudeStyle.drafts.map((draft) => [draft.id, draft.transport])),
);

const listStyle = parseMcpImport(
  JSON.stringify({ mcpServers: [{ id: "notion", type: "streamable-http", url: "https://mcp.example.com/mcp" }] }),
);
const missingId = parseMcpImport(JSON.stringify({ mcpServers: [{ url: "https://x.example.com/mcp" }] }));
check(
  "数组形态可解析；streamable-http 归一成 http，缺 ID 的条目给出原因",
  listStyle.shape === "mcpServers-list" &&
    listStyle.drafts.length === 1 &&
    listStyle.drafts[0].transport === "http" &&
    missingId.drafts.length === 0 &&
    missingId.issues.length === 1,
  `${JSON.stringify(listStyle.drafts.map((draft) => [draft.id, draft.transport]))} / ${missingId.issues.length} issues`,
);

const bareStyle = parseMcpImport(JSON.stringify({ time: { command: "uvx", args: ["mcp-server-time"] } }));
check(
  "裸映射（顶层键即 ID）可用，非参数对象不被当成 Server",
  bareStyle.shape === "server-map" &&
    bareStyle.drafts.length === 1 &&
    bareStyle.drafts[0].id === "time" &&
    bareStyle.drafts[0].transport === "stdio",
  `${bareStyle.shape}/${bareStyle.drafts.length}`,
);

const messy = parseMcpImport(
  JSON.stringify({
    mcpServers: {
      "bad id!": { command: "npx" },
      noUrl: { type: "sse" },
      wrongProto: { url: "ftp://example.com/mcp" },
      ok: { command: "npx", args: ["-y", "pkg"] },
    },
  }),
);
check(
  "非法条目逐条给出原因，而不是静默丢弃",
  messy.drafts.length === 1 && messy.drafts[0].id === "ok" && messy.issues.length === 3,
  JSON.stringify(messy.issues),
);

check(
  "JSON 语法错误向上抛出，由弹层转成可读提示",
  (() => {
    try {
      parseMcpImport("{ nope");
      return false;
    } catch (cause) {
      return cause instanceof SyntaxError;
    }
  })(),
);

const stdioPayload = draftToPayload(firstDraft(JSON.stringify({ mcpServers: { fs: { command: "npx" } } })));
const httpPayload = draftToPayload(
  firstDraft(JSON.stringify({ mcpServers: { gh: { type: "http", url: "https://x.example.com/mcp" } } })),
);
check(
  "草稿转请求体按传输裁剪字段（本地不带 url，远程不带 command/args/env）",
  !("url" in stdioPayload) &&
    stdioPayload.command === "npx" &&
    !("command" in httpPayload) &&
    httpPayload.url === "https://x.example.com/mcp",
  JSON.stringify([stdioPayload, httpPayload]),
);

check(
  "校验口径与后端一致：ID 字符集 / 本地缺命令 / 远程缺地址 / 协议不匹配",
  draftProblem({ ...baseDraft, id: "a b" }).includes("ID 只允许") &&
    draftProblem({ ...baseDraft, command: "" }).includes("必须填写启动命令") &&
    draftProblem({ ...baseDraft, transport: "sse", command: "", url: "" }).includes("必须填写地址") &&
    draftProblem({ ...baseDraft, transport: "ws", command: "", url: "https://x.example.com" }).includes("ws://") &&
    draftProblem(baseDraft) === "",
  [
    draftProblem({ ...baseDraft, id: "a b" }),
    draftProblem({ ...baseDraft, command: "" }),
    draftProblem({ ...baseDraft, transport: "sse", command: "", url: "" }),
  ].join(" | "),
);

check(
  "键值对取值：空行忽略、空键报错、重复键报错、比较按项不看引用",
  entriesToRecord([
    { id: "1", key: "A", value: "1" },
    { id: "2", key: "", value: "" },
  ])?.A === "1" &&
    entriesToRecord([{ id: "1", key: "", value: "v" }]) === null &&
    entriesToRecord([
      { id: "1", key: "A", value: "1" },
      { id: "2", key: "A", value: "2" },
    ]) === null &&
    samePairs({ A: "1" }, { A: "1" }) &&
    !samePairs({ A: "1" }, { A: "1", B: "2" }) &&
    recordToEntries({ A: "1" }, "p").length === 1,
  "见断言条件",
);

// 静态约定：面板不再自带一套校验（旧实现直接调 parsePairs/formatPairs 解析文本域），
// 且三个交互入口（粘贴导入、分组下拉、键值对编辑器）确实接在渲染树里。
const mcpPanelSource = readFileSync(join(process.cwd(), "src", "config", "McpPanel.tsx"), "utf8");
check(
  "MCP 面板：校验单一来源，且三个入口都接上",
  /draftProblem\(/.test(mcpPanelSource) &&
    !/parsePairs\(/.test(mcpPanelSource) &&
    !/formatPairs\(/.test(mcpPanelSource) &&
    /<KeyValueFields/.test(mcpPanelSource) &&
    /TRANSPORT_GROUPS\.map/.test(mcpPanelSource) &&
    /<McpImportModal/.test(mcpPanelSource),
  `draftProblem=${/draftProblem\(/.test(mcpPanelSource)} KeyValueFields=${/<KeyValueFields/.test(mcpPanelSource)}`,
);

const mcpImportSource = readFileSync(join(process.cwd(), "src", "config", "McpImportModal.tsx"), "utf8");
const mcpCss = readFileSync(join(process.cwd(), "src", "config", "config.css"), "utf8");
check(
  "导入弹层样式前缀与「模型批量引入」不撞名（后者占用 cfg-import-*）",
  /cfg-mcp-import-/.test(mcpImportSource) &&
    !/className="cfg-import-/.test(mcpImportSource) &&
    /\.cfg-mcp-import-item\s*\{/.test(mcpCss) &&
    /\.cfg-kv-row\s*\{/.test(mcpCss) &&
    /\.cfg-kv\s*\{[^}]*grid-column/.test(mcpCss),
  `mcp-import-* = ${(mcpCss.match(/\.cfg-mcp-import-/g) ?? []).length} 条规则`,
);

// 弹层平时只在点击后才挂载，静态渲染够不到，所以按显式 props 单独挂一次
// （同 `AgentRoleCard` 的既有做法）。`initialText` 让它能覆盖「有内容」的分支。
const importModalMarkup = renderToStaticMarkup(
  <McpImportModal
    existingIds={["filesystem"]}
    initialText={JSON.stringify({
      mcpServers: {
        filesystem: { command: "npx", args: ["-y", "pkg"] },
        notion: { type: "http", url: "https://mcp.example.com/mcp" },
      },
    })}
    onClose={() => {}}
    onSaved={async () => {}}
  />,
);
check(
  "粘贴导入弹层：有内容时逐条预览，并标出会撞车的 ID",
  importModalMarkup.includes("cfg-mcp-import-item") &&
    importModalMarkup.includes("可登记") &&
    importModalMarkup.includes("notion") &&
    importModalMarkup.includes("cfg-mcp-import-item dup") &&
    importModalMarkup.includes("ID 已存在"),
  importModalMarkup.slice(0, 160),
);

const emptyImportMarkup = renderToStaticMarkup(
  <McpImportModal existingIds={[]} onClose={() => {}} onSaved={async () => {}} />,
);
check(
  "粘贴导入弹层：空态可渲染，且不渲染条目行",
  emptyImportMarkup.length > 200 &&
    emptyImportMarkup.includes("cfg-mcp-import-head") &&
    !emptyImportMarkup.includes("cfg-mcp-import-item"),
  `长度 ${emptyImportMarkup.length}`,
);

// 「执行边界」是只读分区：限额属部署期安全边界，后端没有写接口，前端也不能有编辑控件。
const sandboxUnavailable: SandboxStatus = {
  backend: "docker",
  image: "python:3.12-slim",
  available: false,
  reason: "Docker 守护进程不可达；常见原因是 backend 容器未挂载 /var/run/docker.sock。",
  limits: {
    timeout_seconds: 15,
    memory_limit: "256m",
    cpu_limit: 0.5,
    pids_limit: 64,
    network_enabled: false,
    output_limit_chars: 4000,
    max_code_chars: 20000,
  },
};
const boundaryMarkup = renderToStaticMarkup(<SandboxBoundary status={sandboxUnavailable} />);
check(
  "执行边界：不可用时给出原因与逐项限额",
  boundaryMarkup.includes("不可用") &&
    boundaryMarkup.includes("Docker 守护进程不可达") &&
    boundaryMarkup.includes("python:3.12-slim") &&
    boundaryMarkup.includes("15 秒") &&
    boundaryMarkup.includes("0.5 核") &&
    boundaryMarkup.includes("256m") &&
    boundaryMarkup.includes("禁用") &&
    boundaryMarkup.includes("4000 字符"),
  boundaryMarkup.slice(0, 160),
);

check(
  "执行边界：只读——没有任何可编辑控件",
  !/<input|<select|<textarea/.test(boundaryMarkup),
  boundaryMarkup.slice(0, 160),
);

const sandboxAvailable = renderToStaticMarkup(
  <SandboxBoundary status={{ ...sandboxUnavailable, available: true, reason: null }} />,
);
check(
  "执行边界：可用时不渲染原因行",
  sandboxAvailable.includes("可用") &&
    !sandboxAvailable.includes("Docker 守护进程不可达") &&
    !sandboxAvailable.includes("cfg-alert"),
  sandboxAvailable.slice(0, 160),
);

const configPageSource = readFileSync(join(process.cwd(), "src", "config", "ConfigPage.tsx"), "utf8");
check(
  "工具与配置：执行边界成为第四个分区",
  page.includes("执行边界") &&
    /id:\s*"sandbox"/.test(configPageSource) &&
    /<SandboxPanel\s*\/>/.test(configPageSource),
  "",
);

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

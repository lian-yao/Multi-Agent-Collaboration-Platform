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
 * node node_modules/esbuild/bin/esbuild rendercheck/config-smoke.tsx --bundle \
 *   --platform=node --format=esm --jsx=automatic --loader:.css=empty \
 *   --packages=external --outfile=_smoke_config.mjs && node _smoke_config.mjs
 * ```
 *
 * 跑法按 2026-09-22 实测改过：`--format=cjs` 会让 react-dom 的服务端渲染抛
 * `Element type is invalid`（必须 `esm`），而 `node_modules/.bin/` 在本仓库不存在、
 * `$TEMP` 在 Git Bash 里不展开 → 直接调 `node node_modules/esbuild/bin/esbuild`，
 * 产物写显式相对路径，用完即删。
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
import {
  MetricSeries,
  RuntimeSampling,
  groupMetrics,
} from "../src/records/Inspection";
import { RecordsPage } from "../src/records/RecordsPage";
import {
  AgentToolsPanel,
  buildToolCatalogGroups,
  orphanToolNames,
} from "../src/config/AgentTools";
import { PageTabs } from "../src/components/PageTabs";
import { InlineConfirm, InlineConfirmBar } from "../src/components/InlineConfirm";
import { ProviderMark } from "../src/config/providerIcons";
import { AgentGlyph, agentIconKey, AGENT_ICON_GLYPHS, FALLBACK_AGENT_ICON, type AgentIconKey } from "../src/components/AgentGlyph";
import { resolveKnownContextTokens } from "../src/config/modelCapabilities";
import { draftProblem, draftToPayload, parseMcpImport, type McpImportDraft } from "../src/config/mcpConfig";
import { entriesToRecord, recordToEntries, samePairs } from "../src/config/KeyValueFields";
import { McpImportModal } from "../src/config/McpImportModal";
import { SandboxBoundary } from "../src/config/SandboxPanel";
import { EgressBoundary } from "../src/config/EgressPanel";
import { MemoryBoundary } from "../src/config/MemoryPanel";
import type {
  Agent,
  LongTermMemoryEntry,
  Metric,
  ProviderRegistryDetail,
  SandboxStatus,
  Workflow,
} from "../src/types/api";

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

check(
  "渲染出 6 个分区入口",
  ["Provider", "默认路由", "内部工具", "MCP 工具", "记忆", "执行边界"].every((label) =>
    page.includes(label),
  ),
  page.slice(0, 300),
);
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
  "副路由带 aria-selected 与 tab 角色（工作区入口已搬到工作台；ADR-036 的内部工具与长期记忆各占一个分区）",
  // 六个分区：Provider / 默认路由 / 内部工具 / MCP 工具 / 记忆 / 执行边界。
  (page.match(/role="tab"/g) ?? []).length === 6 && page.includes('aria-selected="true"'),
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
  // 不配白名单 = 不受限，是默认态；「按名单」那条路另有断言。
  tool_names: null,
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

/* ---- 角色头像：按 role 解析图标（components/AgentGlyph.tsx），配置页与协作画布共用 ---- */

const avatarMarkup =
  card.match(/class="cfg-monogram cfg-agent-avatar[^"]*"[^>]*>([\s\S]*?)<\/span>/)?.[1] ?? "";
check(
  "角色头像位画图标，不再塞显示名首字",
  avatarMarkup.includes("<svg") && !/[\u4e00-\u9fff]/.test(avatarMarkup),
  `头像位内容：${avatarMarkup || "（没切到样式块）"}`,
);
check(
  "头像图标与 role 对上（analyst → 折线图）",
  avatarMarkup.includes("lucide-chart-line"),
  avatarMarkup.slice(0, 160),
);

const iconCases: [string, string, AgentIconKey][] = [
  ["collector", "信息收集 Agent", "search"],
  ["analyst", "数据分析 Agent", "chart"],
  ["reporter", "报告生成 Agent", "report"],
  ["summarizer", "摘要 Agent", "summary"],
  ["translator", "翻译 Agent", "translate"],
  ["reviewer", "审核 Agent", "review"],
];
check(
  "role 语义键决定图标（内置三角色 + 常见自定义角色）",
  iconCases.every(([role, name, expected]) => agentIconKey(role, name) === expected),
  iconCases.map(([r, n, e]) => `${r}→${agentIconKey(r, n)}（应为 ${e}）`).join("；"),
);
check(
  "role 优先于显示名，改显示名不会换图标",
  agentIconKey("summarizer", "信息收集 Agent") === "summary",
  `实际 ${agentIconKey("summarizer", "信息收集 Agent")}`,
);
check(
  "role 匹配不到就看显示名，都匹配不到回退机器人",
  agentIconKey("agent-7", "报告生成 Agent") === "report" && agentIconKey("agent-7") === FALLBACK_AGENT_ICON,
  `实际 ${agentIconKey("agent-7")} / ${agentIconKey("agent-7", "报告生成 Agent")}`,
);

const iconKeys = Object.keys(AGENT_ICON_GLYPHS) as AgentIconKey[];
const iconClasses = iconKeys.map((key) => {
  const Glyph = AGENT_ICON_GLYPHS[key];
  return renderToStaticMarkup(<Glyph size={16} />).match(/lucide-[a-z0-9-]+/g)?.[0] ?? "";
});
check(
  "每张角色图标都能渲染出图形",
  iconClasses.every((name) => name.startsWith("lucide-")),
  iconClasses.filter((name) => !name.startsWith("lucide-")).join("、"),
);
check(
  "图标键与图形一一对应，没有两键共用一张图",
  new Set(iconClasses).size === iconKeys.length,
  `键 ${iconKeys.length} 个、图形 ${new Set(iconClasses).size} 张`,
);
check(
  "三种内置角色渲染出三张不同图形",
  renderToStaticMarkup(<AgentGlyph role="collector" size={17} />) !==
    renderToStaticMarkup(<AgentGlyph role="analyst" size={17} />) &&
    renderToStaticMarkup(<AgentGlyph role="analyst" size={17} />) !==
      renderToStaticMarkup(<AgentGlyph role="reporter" size={17} />),
);

/* ---- 工具目录夹具：面板与三态断言共用，必须声明在使用点之前 ---- */

const toolGroups = buildToolCatalogGroups(
  [
    { name: "knowledge_search", description: "检索知识库" },
    { name: "code_exec", description: "在沙箱里执行代码" },
    { name: "web_search", description: "联网检索" },
  ],
  {
    items: [
      {
        name: "web_search",
        description: "联网检索",
        tool_enabled: false,
        enabled: true,
        server_id: "srv-1",
      },
    ],
    servers: [{ id: "srv-1", name: "检索服务" }],
  },
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
      toolGroups={toolGroups}
      toolsError=""
      onReloadTools={() => undefined}
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
check("角色面板带出工具授权分区与白名单标记", tuning.includes("工具授权") && tuning.includes("工具白名单"));

/* ---- 弹窗副路由真的分区 + 图标改由头像浮层承担（2026-09-23 用户反馈） ---- */

const tagOf = (pattern: RegExp) => (pattern.exec(tuning) ?? [""])[0];
const profileTag = tagOf(/<section[^>]*aria-label="角色设定"[^>]*>/);
const toolsTag = tagOf(/<section[^>]*aria-label="角色工具授权"[^>]*>/);
const tuningTag = tagOf(/<form[^>]*id="agent-tuning-form"[^>]*>/);
check(
  "弹窗副路由真的分区：只有当前分区不带 hidden",
  profileTag.length > 0 &&
    tuningTag.length > 0 &&
    toolsTag.length > 0 &&
    !/\shidden/.test(profileTag) &&
    /\shidden/.test(tuningTag) &&
    /\shidden/.test(toolsTag),
  `设定=${profileTag} / 调度=${tuningTag} / 工具=${toolsTag}` +
    "。缺 hidden 就是 .cfg-form-grid 的 display:grid 压过了 UA 的 [hidden]，两个分区会一直同时可见",
);
check(
  "图标选择器不再常驻「设定」，改由头部头像的浮层承担",
  tuning.includes("cfg-agent-avatar-slot") &&
    tuning.includes('aria-expanded="false"') &&
    !tuning.includes("cfg-agent-icons") &&
    !tuning.includes("cfg-agent-icon-popover"),
  "设定分区里不该再有一整行图标格；没点头像时浮层不该出现在 DOM 里",
);

/* ---- 角色工具授权（ADR-035）：三态语义与目录分组 ---- */

check(
  "工具目录按来源分组，MCP 工具从内置里摘出去",
  toolGroups.length === 2 &&
    toolGroups[0].id === "builtin" &&
    toolGroups[0].tools.length === 2 &&
    toolGroups[1].label === "MCP · 检索服务",
  toolGroups.map((group) => `${group.label}:${group.tools.length}`).join(" / "),
);
check(
  "全局停用的工具说明具体原因，不换成「不可用」三个字",
  toolGroups[1].tools[0].available === false &&
    toolGroups[1].tools[0].unavailableReason ===
      "已在 MCP 配置里停用该工具（tool_options.disabled）",
  String(toolGroups[1].tools[0].unavailableReason),
);
check(
  "名单里目录没有的名字才是孤儿",
  orphanToolNames(["code_exec", "gone_tool"], toolGroups).join(",") === "gone_tool",
);

const toolsPanel = (restricted: boolean, selected: string[], groupsError = "") =>
  renderToStaticMarkup(
    <AgentToolsPanel
      groups={toolGroups}
      groupsError={groupsError}
      restricted={restricted}
      selected={selected}
      onToggle={() => undefined}
      onRestrictedChange={() => undefined}
      onSelectAll={() => undefined}
      onSelectNone={() => undefined}
      onReload={() => undefined}
    />,
  );
const inputsTotal = (html: string) => (html.match(/<input/g) ?? []).length;
const inputsLocked = (html: string) => (html.match(/<input[^>]*disabled/g) ?? []).length;

const unrestrictedTools = toolsPanel(false, []);
check(
  "不受限态：整份清单照画，但复选框全部只读",
  unrestrictedTools.includes("当前不受限") &&
    unrestrictedTools.includes("3 / 3 个生效") &&
    inputsTotal(unrestrictedTools) === 3 &&
    inputsLocked(unrestrictedTools) === 3,
  `${inputsLocked(unrestrictedTools)}/${inputsTotal(unrestrictedTools)}`,
);

const scopedTools = toolsPanel(true, ["code_exec"]);
check(
  "受限态：只放行勾选项，另说清名单是快照",
  scopedTools.includes("1 / 3 个生效") && scopedTools.includes("名单是快照"),
  scopedTools.slice(0, 200),
);
check(
  "受限态：只有「全局停用且未勾选」的项锁住，其余可编辑",
  inputsTotal(scopedTools) === 3 && inputsLocked(scopedTools) === 1,
  `${inputsLocked(scopedTools)}/${inputsTotal(scopedTools)}`,
);

const emptyTools = toolsPanel(true, []);
check(
  "空名单是危险态，明说保存后没有工具可用",
  emptyTools.includes("0 / 3 个生效") && emptyTools.includes("名单是空的"),
  emptyTools.slice(0, 200),
);
const orphanTools = toolsPanel(true, ["gone_tool"]);
check(
  "孤儿名单单独成组，并说清保存时原样保留",
  orphanTools.includes("名单里有、目录里没有") && orphanTools.includes("原样保留"),
  orphanTools.slice(0, 200),
);
const brokenTools = toolsPanel(true, [], "工具目录读取失败。");
check(
  "目录读不到时禁用模式开关并给出重试入口",
  brokenTools.includes("重新读取工具目录") &&
    (brokenTools.match(/<button[^>]*disabled/g) ?? []).length === 1,
  brokenTools.slice(0, 260),
);

const legacyAgent = { ...roleAgent };
delete (legacyAgent as { tool_names?: unknown }).tool_names;
check(
  "tool_names 缺失（旧响应 / 预览种子）按「不受限」渲染，不是渲染期报错",
  renderToStaticMarkup(
    <AgentRoleCard agent={legacyAgent} active={false} onOpen={() => undefined} />,
  ).includes("工具不受限"),
  "缺字段时应当等价于未配置",
);
check(
  "角色方块标出工具授权状态",
  card.includes("工具不受限") &&
    renderToStaticMarkup(
      <AgentRoleCard
        agent={{ ...roleAgent, tool_names: ["code_exec"] }}
        active={false}
        onOpen={() => undefined}
      />,
    ).includes("工具 1 个") &&
    renderToStaticMarkup(
      <AgentRoleCard
        agent={{ ...roleAgent, tool_names: [] }}
        active={false}
        onOpen={() => undefined}
      />,
    ).includes("工具 0 个"),
  card.slice(0, 200),
);

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

/* ---- 运行记录：就地展开日志，失败原因在折叠区之外 ---- */

const failedWorkflow: Workflow = {
  id: "wf-1234567890",
  session_id: "s-1",
  agent_run_id: null,
  status: "failed",
  current_step: "analyst",
  checkpoint: {
    status: "failed",
    current_step: "analyst",
    completed_steps: ["collector"],
    mode: "static",
  },
  error: "阶段 analyst 执行失败：Provider 返回 401（凭据无效）",
  created_at: "2026-09-22T10:00:00Z",
  updated_at: "2026-09-22T10:02:00Z",
  completed_at: "2026-09-22T10:02:00Z",
};

let failedRun = "";
try {
  failedRun = renderToStaticMarkup(
    <RecordsPage
      tab="runs"
      onTabChange={() => undefined}
      workflow={failedWorkflow}
      messageCount={3}
      currentTaskTitle="分析这份财报"
      sessionId="s-1"
      onOpenRun={() => undefined}
      onOpenSession={() => undefined}
      onDeleteSession={async () => undefined}
    />,
  );
} catch (cause) {
  check("失败态运行记录可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check(
  "运行记录就地展开，不再把整卡做成跳转入口",
  failedRun.includes("展开阶段日志与失败原因") && failedRun.includes('aria-expanded="false"'),
  failedRun.slice(0, 240),
);
check(
  "失败原因不等展开就先给出来",
  failedRun.includes("失败原因") && failedRun.includes("Provider 返回 401（凭据无效）"),
  failedRun.slice(0, 400),
);
check(
  "未展开时不请求也不渲染阶段日志",
  !failedRun.includes("阶段日志") || !failedRun.includes("读取阶段日志"),
  failedRun.slice(0, 240),
);

/* ---- 指标采样：按「指标名 + 标签组」归集成序列 ---- */

const metricFixtures: Metric[] = [
  { id: 1, metric_name: "stage_duration_ms", value: 120, labels: { stage: "collector" }, recorded_at: "2026-09-22T10:00:01Z" },
  { id: 2, metric_name: "stage_duration_ms", value: 200, labels: { stage: "analyst" }, recorded_at: "2026-09-22T10:00:02Z" },
  { id: 3, metric_name: "stage_duration_ms", value: 180, labels: { stage: "collector" }, recorded_at: "2026-09-22T10:00:03Z" },
  { id: 4, metric_name: "input_tokens", value: 900, labels: {}, recorded_at: "2026-09-22T10:00:04Z" },
];
const seriesGroups = groupMetrics(metricFixtures);
check(
  "同指标不同标签分成两条序列，不混成一条",
  seriesGroups.length === 3 &&
    seriesGroups.filter((group) => group.metric_name === "stage_duration_ms").length === 2,
  seriesGroups.map((group) => group.key).join(" | "),
);
const collectorSeries = seriesGroups.find((group) => group.labels.stage === "collector");
check(
  "同序列按时间升序排，趋势才读得出来",
  collectorSeries?.samples.map((sample) => sample.value).join(",") === "120,180",
  collectorSeries?.samples.map((sample) => `${sample.value}@${sample.recorded_at}`).join(" / "),
);
check(
  "序列之间按最近一次采样倒序",
  seriesGroups[0].metric_name === "input_tokens",
  seriesGroups[0].metric_name,
);
const seriesHtml = collectorSeries ? renderToStaticMarkup(<MetricSeries group={collectorSeries} />) : "";
check(
  "指标序列行画出趋势、统计与单位",
  seriesHtml.includes("polyline") &&
    seriesHtml.includes("最小") &&
    seriesHtml.includes("均值") &&
    seriesHtml.includes("2 次采样") &&
    seriesHtml.includes("耗时"),
  seriesHtml.slice(0, 260),
);
check(
  "逐次原值仍可展开，归集没有丢掉原始数据",
  seriesHtml.includes("逐次原值（2 条）"),
  seriesHtml.slice(0, 260),
);
const singleSample = renderToStaticMarkup(
  <MetricSeries
    group={{ key: "k", metric_name: "input_tokens", labels: {}, samples: [metricFixtures[3]] }}
  />,
);
check(
  "只有一次采样时说明画不出趋势，而不是画一条假线",
  singleSample.includes("仅 1 次采样") && !singleSample.includes("polyline"),
  singleSample.slice(0, 200),
);
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
  check(
    "[hidden] 压得住 .cfg-form-grid 的 display（副路由互斥的隐性依赖）",
    /\[hidden\]\s*\{[^}]*display:\s*none\s*!important/.test(config),
    "config.css 需要 [hidden]{display:none!important}：作者样式的 display 优先于 UA 的 [hidden]，少了它「设定」与「调度」会一直同时可见",
  );
  check(
    "config.css 不引用从未定义的设计令牌",
    !/--cfg-(?:border|text|text-dim)\)/.test(config),
    "--cfg-border / --cfg-text / --cfg-text-dim 从未定义（真令牌是 --cfg-line / --cfg-ink / --cfg-ink-3），引用它们等于整条声明失效",
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
  "工具与配置：执行边界排在最后（记忆排在它前面）",
  page.includes("执行边界") &&
    /id:\s*"sandbox"/.test(configPageSource) &&
    /<SandboxPanel\s*\/>/.test(configPageSource) &&
    // 记忆是全局数据（不属于某个会话），所以归配置页；顺序上排在执行边界之前。
    /id:\s*"memory"/.test(configPageSource) &&
    configPageSource.indexOf('id: "memory"') < configPageSource.indexOf('id: "sandbox"'),
  "",
);

/* -------------------------------------------------------------------------- */
/* 长期记忆面板（§5.22、ADR-036）                                              */
/* -------------------------------------------------------------------------- */

const memoryEntries: LongTermMemoryEntry[] = [
  {
    key: "称呼",
    content: "叫我张三",
    updated_at: "2026-09-23T10:54:04Z",
  },
  {
    key: "回答长度",
    content: "尽量简短",
    updated_at: "2026-09-23T02:00:00Z",
  },
];
const memoryMarkup = renderToStaticMarkup(
  <MemoryBoundary items={memoryEntries} onDelete={() => {}} onReload={() => {}} />,
);
check(
  "记忆面板：逐条列出 key、正文与更新时间，并给出「忘记」入口",
  memoryMarkup.includes("称呼") &&
    memoryMarkup.includes("叫我张三") &&
    memoryMarkup.includes("回答长度") &&
    memoryMarkup.includes("忘记") &&
    memoryMarkup.includes("2 条"),
  memoryMarkup.slice(0, 200),
);
const memoryEmptyMarkup = renderToStaticMarkup(
  <MemoryBoundary items={[]} onDelete={() => {}} onReload={() => {}} />,
);
check(
  "记忆面板：空态说清「怎么产生」（对话里说「记住：…」），且没有任何写入控件",
  memoryEmptyMarkup.includes("记住：") &&
    // 只查**控件**，不查文案：面板正文里正当解释"新增只能在对话里说「记住：…」"，
    // 用 `/新增/` 去卡会把自己的说明一起卡掉（这类"被自己的注释绊倒"本轮踩过多次）。
    !/<form|<input|<textarea|<select/.test(memoryEmptyMarkup) &&
    !/<button[^>]*>\s*(新增|添加|保存)/.test(memoryEmptyMarkup),
  "",
);
check(
  "记忆面板：失败态可重试，读取中不显示成「没有记忆」",
  renderToStaticMarkup(
    <MemoryBoundary items={[]} error="长期记忆读取失败" onReload={() => {}} />,
  ).includes("重试") &&
    renderToStaticMarkup(<MemoryBoundary items={[]} loading />).includes("读取中") &&
    !renderToStaticMarkup(<MemoryBoundary items={[]} loading />).includes("还没有长期记忆"),
  "",
);
const memoryPanelSource = readFileSync(
  join(process.cwd(), "src", "config", "MemoryPanel.tsx"),
  "utf8",
);
check(
  "记忆面板：只列与删——写入只走对话里的「记住：…」",
  /api\.listLongTermMemory\(/.test(memoryPanelSource) &&
    /api\.deleteLongTermMemory\(/.test(memoryPanelSource) &&
    // 客户端根本没有写入方法，面板也就不可能凭空造一条记忆
    !/remember\(|save_entry|createLongTermMemory/.test(memoryPanelSource) &&
    !/POST|put\(/.test(memoryPanelSource),
  "",
);
check(
  "记忆面板：按错误码处理「已经不在了」，不把它渲染成故障",
  /MEMORY_ENTRY_NOT_FOUND/.test(memoryPanelSource) &&
    /已经是|已经不在了|已为你刷新/.test(memoryPanelSource),
  "",
);

/* -------------------------------------------------------------------------- */
/* 出网策略（§5.21）                                                          */
/* -------------------------------------------------------------------------- */

check(
  "工作区不再是配置页分区（已搬到工作台，§7.1），执行边界仍同时挂沙箱与出网",
  !/id:\s*"workspaces"/.test(configPageSource) &&
    // 只看**渲染**：注释里说明「这块搬到哪去了」是允许的（与其它断言同一口径）。
    !/<WorkspacePanel\s*\/>/.test(configPageSource) &&
    /<EgressPanel\s*\/>/.test(configPageSource),
  "",
);

const egressMarkup = renderToStaticMarkup(
  <EgressBoundary
    status={{
      mode: "public_only",
      allow_hosts: ["*.example.com"],
      deny_hosts: [],
      internal_hosts: ["redis", "search-gateway"],
      allowed_ports: [443, 8800],
      model_exempt: true,
      max_redirects: 5,
      blocked: {},
    }}
  />,
);
check(
  "出网策略：只读展示模式、内部服务与端口",
  egressMarkup.includes("只放公网") &&
    egressMarkup.includes("search-gateway") &&
    egressMarkup.includes("443、8800") &&
    !/<input|<select|<textarea/.test(egressMarkup),
  egressMarkup.slice(0, 200),
);
check(
  "出网策略：计数为 0 时说明这是进程内计数，不说成「没有被拦过」",
  egressMarkup.includes("本进程还没有拒绝记录") &&
    egressMarkup.includes("macp_egress_blocked_total"),
  egressMarkup.slice(0, 200),
);

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

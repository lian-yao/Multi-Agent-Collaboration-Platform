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
 * hooks 顺序、空态、以及用显式 props 驱动的模型区块），外加两组读文件的静态断言
 * （样式表版式、副路由语义）。真正需要点击与 effects 的交互路径仍要人工在浏览器里
 * 过一遍——见 `doc/testing.md` §3.3。
 *
 * 注意：角色路由面板挂在「Agent 团队」页、采样面板挂在「任务记录」页，都不在
 * `RuntimeConfig` 里，所以这里单独挂载一次，避免换页后新位置无人验证。
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentPanel, AgentRoleCard, AgentTuningPanel } from "../src/config/AgentPanel";
import { RuntimeConfig } from "../src/config/ConfigPage";
import { ModelSection } from "../src/config/ModelSection";
import { RuntimeSampling } from "../src/records/Inspection";
import { RecordsPage } from "../src/records/RecordsPage";
import { PageTabs } from "../src/components/PageTabs";
import type { Agent, ProviderRegistryDetail } from "../src/types/api";

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

check("渲染出 3 个分区入口", ["Provider", "默认路由", "MCP 工具"].every((label) => page.includes(label)), page.slice(0, 300));
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
  (page.match(/role="tab"/g) ?? []).length === 3 && page.includes('aria-selected="true"'),
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
      onDeleteSession={() => undefined}
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

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

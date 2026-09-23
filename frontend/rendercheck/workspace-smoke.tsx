/**
 * 工作台协作视图渲染冒烟检查（ADR-018，`frontend/src/workspace/`）。
 *
 * 与 `rendercheck/config-smoke.tsx` 同一套路：用 `react-dom/server` 在无浏览器前提下
 * 把新组件整棵树渲染一遍，抓渲染期崩溃、解构错字段、空态分支写错这类问题；
 * 再加两组读文件的静态断言，固定「假选择已删除」与「职责边界写在注释里」。
 *
 * 运行（**必须在 `frontend/` 下执行**，静态断言会按 cwd 读 `src/`）：
 *
 * ```bash
 * node node_modules/esbuild/bin/esbuild rendercheck/workspace-smoke.tsx --bundle \
 *   --platform=node --format=esm --jsx=automatic --loader:.css=empty \
 *   --packages=external --outfile=_smoke.mjs && node _smoke.mjs
 * ```
 *
 * 这两条都按 2026-09-22 实测改过，别照旧写法：`--format=cjs` 会让 react-dom 的服务端渲染
 * 抛 `Element type is invalid`，必须用 `esm`；`node_modules/.bin/` 在本仓库不存在，
 * `$TEMP` 在 Git Bash 里也不展开 → 直接调 `node node_modules/esbuild/bin/esbuild`，
 * 产物写显式相对路径，用完即删。
 *
 * 冒烟本身只跑代码，**不做类型检查**（esbuild 只转译），而 `tsconfig.json` 的
 * `include` 只有 `src`，覆盖不到本目录。改了本文件或 `preview.tsx` 后要单独过一遍：
 *
 * ```bash
 * node node_modules/typescript/bin/tsc --noEmit --jsx react-jsx --module esnext \
 *   --moduleResolution bundler --target es2022 --lib es2022,dom,dom.iterable \
 *   --strict --skipLibCheck --esModuleInterop --isolatedModules rendercheck/preview.tsx
 * ```
 *
 * 退出码 0 = 全通过。只跑不依赖 effects 的渲染路径——`TaskUsage` / `Inspector` 这类
 * 靠 effect 拉数据的容器不在覆盖范围内，仍要人工在浏览器里过一遍。
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { CollaborationGraph } from "../src/workspace/CollaborationGraph";
import { CollabCanvas, type CollabConversation } from "../src/workspace/CollabCanvas";
import { layoutCollaboration } from "../src/workspace/GraphCanvas";
import {
  buildCollaboration,
  planSourceKey,
  type CollabGraph,
  type StageMeta,
} from "../src/workspace/collaboration";
import {
  AgentStageModal,
  AgentTraceView,
  type AgentStageDetail,
} from "../src/workspace/AgentStageModal";
import { formatStamp } from "../src/config/shared";
import { Markdown } from "../src/components/Markdown";
import { ApprovalCard, APPROVAL_STATUS_TEXT } from "../src/workspace/ApprovalCard";
import type { Approval, ApprovalStatus } from "../src/types/api";
import { Disclosure } from "../src/components/Disclosure";
import { RunActivity } from "../src/workspace/RunActivity";
import { ToolCallBlock } from "../src/workspace/TraceParts";
import { TaskUsagePanel, groupUsage, usageFor } from "../src/workspace/TaskUsage";
import { MessageAttachmentList, PendingFileChips } from "../src/workspace/AttachmentList";
import {
  HostLocationBrowser,
  WorkspaceBoundary,
  WorkspaceLocationPicker,
  folderTargets,
} from "../src/workspace/WorkspacePanel";
import {
  ATTACHMENT_EXTENSIONS,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_COUNT,
  classifyLocal,
  describeAttachment,
  formatBytes,
  rejectionReason,
  shortenName,
  type PendingAttachment,
} from "../src/workspace/attachments";
import type {
  Agent,
  Attachment,
  WorkspaceHostTree,
  Metric,
  StageTraceItem,
  Workspace,
  WorkspaceTree,
  Workflow,
  WorkflowStageTrace,
} from "../src/types/api";

const results: [boolean, string][] = [];
const check = (label: string, condition: boolean, detail = "") =>
  results.push([condition, condition ? label : `${label}  →  ${detail}`]);

/* -------------------------------------------------------------------------- */
/* 协作工作流模型                                                              */
/* -------------------------------------------------------------------------- */

const collabStages: StageMeta[] = [
  { id: "collect", label: "信息收集", agent: "collector", responsibility: "整理任务要求与输入资料。" },
  { id: "analyze", label: "数据分析", agent: "analyst", responsibility: "基于收集结果梳理关键结论。" },
  { id: "report", label: "报告生成", agent: "reporter", responsibility: "整合分析结果，组织结构化报告。" },
];

const collabAgent = (id: string, name: string, override: string[]): Agent => ({
  id,
  name,
  role: id,
  model: "gpt-5.5",
  provider: "openai",
  provider_name: "OpenAI",
  llm_model_id: null,
  temperature: 0.2,
  top_p: null,
  max_output_tokens: 1024,
  reasoning_type: "none",
  status: "ready",
  override_keys: override,
  builtin: true,
  description: null,
  enabled: true,
  tool_names: null,
});

const collabAgents: Agent[] = [
  collabAgent("collector", "信息收集 Agent", ["temperature"]),
  collabAgent("analyst", "数据分析 Agent", []),
  collabAgent("reporter", "报告生成 Agent", ["max_output_tokens"]),
];

const collabWorkflow: Workflow = {
  id: "w-collab",
  session_id: "s-1",
  agent_run_id: "r-1",
  status: "completed",
  current_step: null,
  checkpoint: {
    status: "completed",
    current_step: null,
    completed_steps: ["collect", "analyze", "report"],
  },
  created_at: "2026-09-21T10:00:00Z",
  updated_at: "2026-09-21T10:00:30Z",
  completed_at: "2026-09-21T10:00:30Z",
  error: null,
};

const collabTraces: WorkflowStageTrace = {
  workflow_id: "w-collab",
  mode: "static",
  task: "统计 128/2680 的占比并说明含义",
  availability: "available",
  reason: null,
  items: [
    {
      stage: "collect",
      role: "collector",
      input: null,
      input_from: null,
      output: "已算出占比 4.78%。",
      tool_calls: [
        {
          call_id: "c-1",
          tool_name: "calculator",
          status: "succeeded",
          input: { expression: "128/2680" },
          output: { value: 0.0477612 },
          error: null,
        },
      ],
      truncated: false,
      reason: null,
    },
    {
      stage: "analyze",
      role: "analyst",
      input: "已算出占比 4.78%。",
      input_from: "collect",
      output: "该比例说明退货在总量中占比偏低。",
      tool_calls: [],
      truncated: false,
      reason: null,
    },
    {
      stage: "report",
      role: "reporter",
      input: "该比例说明退货在总量中占比偏低。",
      input_from: "analyze",
      output: "# 占比分析报告",
      tool_calls: [],
      truncated: true,
      reason: null,
    },
  ],
};

/**
 * 采样里**没有 `agent_id`，只有 `role`**——这正是「Token 有没有落到 Agent 头上」的考题。
 * 用 `agent_id` 的旧实现会把三条采样全退到 `model` 那一层、并成一个分组。
 */
const collabMetrics: Metric[] = [
  { metric_name: "input_tokens", value: 817, labels: { role: "collector", stage: "collect" }, recorded_at: "" },
  { metric_name: "output_tokens", value: 66, labels: { role: "collector", stage: "collect" }, recorded_at: "" },
  { metric_name: "total_tokens", value: 883, labels: { role: "collector", stage: "collect" }, recorded_at: "" },
  { metric_name: "total_tokens", value: 1556, labels: { role: "reporter", stage: "report" }, recorded_at: "" },
];

const collabGraph: CollabGraph = buildCollaboration({
  stages: collabStages,
  agents: collabAgents,
  workflow: collabWorkflow,
  completed: new Set(["collect", "analyze", "report"]),
  traces: collabTraces,
  metrics: collabMetrics,
});

check("模型：三个静态阶段各成一个节点", collabGraph.nodes.length === 3, `实际 ${collabGraph.nodes.length}`);
check("模型：串行链路每步依赖上一步", collabGraph.edges.length === 2, `实际 ${collabGraph.edges.length}`);
check("模型：每波一个节点即串行流水线", collabGraph.waves.length === 3 && !collabGraph.nodes.some((n) => n.parallel));
check("模型：连线的工具链路挂在上游", collabGraph.edges[0].toolSummary === "calculator ×1", collabGraph.edges[0].toolSummary);
check(
  "模型：采样按 role 归到各 Agent",
  collabGraph.nodes[0].usage.some((row) => row.label === "总 Token" && row.values[0] === "883"),
  JSON.stringify(collabGraph.nodes[0].usage),
);
check("模型：没采样的 Agent 是空数组而不是 0", collabGraph.nodes[1].usage.length === 0, JSON.stringify(collabGraph.nodes[1].usage));
check("模型：参数带显式覆盖标记", collabGraph.nodes[0].params.some((p) => p.key === "temperature" && p.overridden));
check("模型：未覆盖的参数不算显式覆盖", collabGraph.nodes[1].params.every((p) => !p.overridden));
check("模型：根节点回落成整条任务原文", collabGraph.nodes[0].prompt === "统计 128/2680 的占比并说明含义");
check("模型：下游节点标注输入来自哪一步", collabGraph.nodes[1].promptFrom === "collect");
check("模型：截断标记带出来", collabGraph.nodes[2].truncated);
check("模型：usageFor 按角色取到该 Agent 的采样", usageFor(collabMetrics, "reporter").length === 1);

/* -------------------------------------------------------------------------- */
/* 规划窗口：计划没落盘时不许拿固定三步冒充事实（ADR-034）                        */
/*                                                                             */
/* 刚提交的运次里 `checkpoint` 是 null（`create_workflow_run` 只建行、不写摘要）， */
/* 于是「没有计划」同时对应静态链路的常态与动态链路的规划窗口。判据靠提交方声明的   */
/* `requestedMode`——服务端在这段时间里没有任何字段能说明这件事。                 */
/* -------------------------------------------------------------------------- */

const draftWorkflow: Workflow = {
  id: "w-draft",
  session_id: "s-1",
  agent_run_id: null,
  status: "running",
  current_step: null,
  checkpoint: null,
  created_at: "2026-09-22T10:00:00Z",
  updated_at: "2026-09-22T10:00:03Z",
  completed_at: null,
  error: null,
};

const draftGraph = (requestedMode?: string) =>
  buildCollaboration({
    stages: collabStages,
    agents: collabAgents,
    workflow: draftWorkflow,
    completed: new Set(),
    traces: null,
    requestedMode,
  });

check(
  "模型：动态编排计划未落盘时不出节点，只报在规划",
  draftGraph("dynamic").planning && draftGraph("dynamic").nodes.length === 0,
  `planning=${draftGraph("dynamic").planning} nodes=${draftGraph("dynamic").nodes.length}`,
);
check(
  "模型：同一份数据按静态提交时照画固定三步",
  !draftGraph("static").planning && draftGraph("static").nodes.length === 3,
  // 静态链路的固定三步是常量，没有规划环节——把它也判成「在规划」是把真话藏起来
  `planning=${draftGraph("static").planning} nodes=${draftGraph("static").nodes.length}`,
);
check(
  "模型：不知道模式时不猜「在规划」",
  !draftGraph(undefined).planning,
  // 历史工作流不带开关：宁可照旧画三步，也不要把静态老任务读成「正在规划」
  `planning=${draftGraph(undefined).planning}`,
);

/* -------------------------------------------------------------------------- */
/* 画布几何：纯函数，先把坐标算对再看渲染                                        */
/* -------------------------------------------------------------------------- */

const layout = layoutCollaboration(collabGraph, 720);
check("画布：首尾端子 + 三个 Agent 共五个节点", layout.nodes.length === 5, `实际 ${layout.nodes.length}`);
check(
  "画布：首尾端子是任务与交付",
  layout.nodes[0].kind === "start" && layout.nodes[4].kind === "end",
  `${layout.nodes[0].kind} / ${layout.nodes[4].kind}`,
);
check("画布：串行链路连成四条边", layout.edges.length === 4, `实际 ${layout.edges.length}`);
check(
  "画布：整条链路跑完就整条点亮",
  layout.edges.filter((edge) => edge.active).length === 4,
  `${layout.edges.filter((edge) => edge.active).length} 条点亮`,
);
check(
  "画布：边是三次贝塞尔而不是直线",
  layout.edges.every((edge) => edge.d.startsWith("M ") && edge.d.includes(" C ")),
  layout.edges[0].d,
);
check(
  "画布：路径两端退到圆周上",
  // 圆心在 y=46、半径 23，起点必须离开圆心：起点 y 不应等于 46
  !layout.edges[0].d.startsWith(`M 360 46`),
  layout.edges[0].d,
);
check("画布：连线记得带上游工具链路", layout.edges[1].toolSummary === "calculator ×1", layout.edges[1].toolSummary);
const yOfNode = (id: string) => layout.nodes.find((node) => node.id === id)?.y ?? -1;
check(
  "画布：胶囊落在「上游标签之下、下游圆之上」的空带里",
  // 上游圆下沿 + 标签高度 < 胶囊 < 下游圆上沿：直接取 t=0.5 会被标签的底色压住
  layout.edges[1].mid.y > yOfNode("collect") + 21 + 34 &&
    layout.edges[1].mid.y < yOfNode("analyze") - 21 - 4,
  `mid=${layout.edges[1].mid.y} 空带 ${yOfNode("collect") + 55}~${yOfNode("analyze") - 25}`,
);
check("画布：没有并行波次就不画圈定框", layout.groups.length === 0, `实际 ${layout.groups.length}`);
check(
  "画布：高度由行数算出，不随容器拉伸",
  layout.height === 44 + 74 + 4 * 96,
  `实际 ${layout.height}`,
);
check(
  "画布：给了更高的可用高度就把行距撑开",
  layoutCollaboration(collabGraph, 720, false, 900).height === 900,
  `实际 ${layoutCollaboration(collabGraph, 720, false, 900).height}`,
);
check(
  "画布：可用高度不够时不压缩行距",
  layoutCollaboration(collabGraph, 720, false, 200).height === 44 + 74 + 4 * 96,
  `实际 ${layoutCollaboration(collabGraph, 720, false, 200).height}`,
);
check("画布：量不到宽度时退回默认宽度", layoutCollaboration(collabGraph, 0).width === 720);
check("画布：侧栏窄档退回更小的默认宽度", layoutCollaboration(collabGraph, 0, true).width === 248);

/* -------------------------------------------------------------------------- */
/* 画布：规划节点（「任务分配」，只有动态链路有）                                 */
/* -------------------------------------------------------------------------- */

/**
 * 分配**结果**已经画在角色节点上了（谁参与、谁依赖谁），所以「画布渲染了任务分配」这件事
 * 不能只靠角色节点证明——真正缺的是「依据是什么」。三组断言各自对应一件事：
 * 模型里带了 `planner`、布局里多占了**一行**、浮层里真的写出了理由与逐条分工。
 */
const plannerWorkflow: Workflow = {
  id: "w-plan",
  session_id: "s-1",
  agent_run_id: "r-2",
  status: "running",
  current_step: "s2",
  checkpoint: {
    status: "running",
    mode: "dynamic",
    current_step: "s2",
    plan_source: "llm",
    plan_rationale: "任务要先取证再核算，最后由专人成文。",
    completed_steps: ["s1"],
    plan: [
      { id: "s1", role: "collector", instruction: "找到权威数据源", depends_on: [], status: "completed" },
      { id: "s2", role: "analyst", instruction: "算出占比并解释", depends_on: ["s1"], status: "pending" },
      { id: "s3", role: "reporter", instruction: "写成结论", depends_on: ["s2"], status: "pending" },
    ],
  },
  created_at: "",
  updated_at: "",
  completed_at: null,
  error: null,
};

const plannerGraph = buildCollaboration({
  stages: collabStages,
  agents: collabAgents,
  workflow: plannerWorkflow,
  completed: new Set(["s1"]),
  traces: null,
});

check(
  "模型：动态链路带出分配理由与逐条分工",
  plannerGraph.planner?.rationale === "任务要先取证再核算，最后由专人成文。" &&
    plannerGraph.planner?.assignments.length === 3 &&
    plannerGraph.planner?.assignments[1].label === "数据分析" &&
    plannerGraph.planner?.assignments[2].instruction === "写成结论",
  JSON.stringify(plannerGraph.planner),
);
check(
  "模型：静态链路没有规划决策（是 null，不是空壳对象）",
  collabGraph.planner === null,
  JSON.stringify(collabGraph.planner),
);

const plannerLayout = layoutCollaboration(plannerGraph, 720);
check(
  "画布：规划节点插在任务端子与首个角色节点之间",
  plannerLayout.nodes[1].kind === "planner" &&
    plannerLayout.nodes[1].label === "任务分配" &&
    plannerLayout.nodes[1].y > plannerLayout.nodes[0].y &&
    plannerLayout.nodes[1].y < plannerLayout.nodes[2].y,
  plannerLayout.nodes.map((node) => `${node.kind}@${node.y}`).join(" / "),
);
check(
  "画布：规划节点只多占一行，不改变波次",
  plannerLayout.height === 44 + 74 + 5 * 96 &&
    plannerGraph.waves.length === 3 &&
    plannerLayout.nodes.filter((node) => node.kind === "planner").length === 1,
  `高度 ${plannerLayout.height} / 波数 ${plannerGraph.waves.length}`,
);
check(
  "画布：连线变成 任务→分配→s1→s2→s3→交付",
  plannerLayout.edges.length === 5 &&
    plannerLayout.edges.some((edge) => edge.key === "__start->__planner") &&
    plannerLayout.edges.some((edge) => edge.key === "__planner->s1"),
  plannerLayout.edges.map((edge) => edge.key).join(" / "),
);

let plannerSerial = "";
try {
  plannerSerial = renderToStaticMarkup(
    <CollabCanvas open graph={plannerGraph} onClose={() => undefined} />,
  );
  check("全屏画布可渲染（含规划节点）", plannerSerial.length > 400, `长度 ${plannerSerial.length}`);
} catch (cause) {
  check("全屏画布可渲染（含规划节点）", false, cause instanceof Error ? cause.message : String(cause));
}
check("画布画出规划节点", plannerSerial.includes("cv-node kind-planner"), plannerSerial.slice(0, 200));
check(
  "规划节点用的是「规划」图标（与配置页同一套解析，不是清一色机器人）",
  plannerSerial.includes("lucide-list-checks"),
  "AgentGlyph 的 plan 键 = ListChecks；图标与键错配是静默的，只能这样发现",
);
check(
  "规划节点的浮层写出分配理由与逐条分工",
  plannerSerial.includes("分配理由") &&
    plannerSerial.includes("任务要先取证再核算，最后由专人成文。") &&
    plannerSerial.includes("找到权威数据源"),
  plannerSerial.slice(plannerSerial.indexOf("cv-pop"), plannerSerial.indexOf("cv-pop") + 200),
);

/* -------------------------------------------------------------------------- */
/* 侧栏：紧凑画布                                                              */
/* -------------------------------------------------------------------------- */

let serial = "";
try {
  serial = renderToStaticMarkup(
    <CollaborationGraph graph={collabGraph} onExpand={() => undefined} />,
  );
  check("侧栏画布可渲染", serial.length > 400, `长度 ${serial.length}`);
} catch (cause) {
  check("侧栏画布可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("侧栏画布是 SVG + 绝对定位节点的画法", serial.includes("cv-canvas dense") && serial.includes("cv-svg"));
check("侧栏画布画出了圆节点", serial.includes("cv-node kind-agent"));
check(
  "侧栏画出三个 Agent 节点",
  ["信息收集 Agent", "数据分析 Agent", "报告生成 Agent"].every((name) => serial.includes(name)),
);
// 圆面图形与配置页共用 components/AgentGlyph.tsx 的解析：三个角色各拿自己的图标，
// 而不是清一色机器人。断言 icon 的 kebab 类名——键与图形错配是静默的，只有这样才能发现。
check(
  "侧栏节点画的是角色图标（收集 / 分析 / 报告各一）",
  serial.includes("lucide-file-search") &&
    serial.includes("lucide-chart-line") &&
    serial.includes("lucide-file-text"),
  "圆面图形应与配置页角色卡一致，见 components/AgentGlyph.tsx",
);
check("侧栏标出首尾端子", serial.includes(">任务<") && serial.includes(">交付<"));
check(
  "侧栏节点记下模型与 Token",
  serial.includes("gpt-5.5 · 883 tok"),
  serial.slice(serial.indexOf("cv-meta"), serial.indexOf("cv-meta") + 60),
);
check("侧栏未采样的节点只写模型", serial.includes(">gpt-5.5<"));
check(
  "侧栏不挂悬停浮层",
  !serial.includes("cv-pop") && !serial.includes("cv-pop-slot"),
  "悬停浮层只在全屏画布给；侧栏的答案就是圆下方那行「模型 · Token」",
);
check(
  "侧栏不重复执行轨迹",
  !serial.includes("生效参数") && !serial.includes("分配到的任务") && !serial.includes("阶段产出"),
  "侧栏只回答「用什么跑的」，轨迹归执行台",
);
check(
  "侧栏不再铺一段功能说明",
  !serial.includes("串行流水线") && !serial.includes("节点是 Agent 卡片") && !serial.includes("collab-legend"),
);
check("侧栏给出全屏画布入口", serial.includes("全屏画布"));
check("侧栏不弹执行轨迹", !serial.includes("查看信息收集 Agent的"), serial.slice(0, 200));

/* 状态优先于角色：正在跑的那一格画转圈，不让角色图标把状态盖掉 */
const runningGraph = buildCollaboration({
  stages: collabStages,
  agents: collabAgents,
  workflow: {
    ...collabWorkflow,
    status: "running",
    current_step: "analyze",
    checkpoint: { status: "running", current_step: "analyze", completed_steps: ["collect"] },
  },
  completed: new Set(["collect"]),
  traces: collabTraces,
});
const running = renderToStaticMarkup(<CollaborationGraph graph={runningGraph} />);
check(
  "执行中的节点画转圈而不是角色图标",
  running.includes("lucide-loader-circle") && running.includes("spin"),
  "状态是人要立刻看见的信号，不能被角色图标盖掉",
);
check(
  "同一张图里已跑完与未开始的节点各带自己的角色图标",
  running.includes("lucide-file-search") && running.includes("lucide-file-text"),
  "只有执行中那一格让位给状态",
);

/* 并行：同层步骤算同一波，画成一个圈定框而不是写一句「并行」 */
const parallelGraph = buildCollaboration({
  stages: collabStages,
  agents: collabAgents,
  workflow: {
    ...collabWorkflow,
    status: "running",
    current_step: "s3",
    checkpoint: {
      status: "running",
      current_step: "s3",
      completed_steps: ["s1", "s2"],
      plan: [
        { id: "s1", role: "collector", depends_on: [], status: "completed" },
        { id: "s2", role: "analyst", depends_on: [], status: "completed" },
        { id: "s3", role: "reporter", depends_on: ["s1", "s2"], status: "pending" },
      ],
    },
  },
  completed: new Set(["s1", "s2"]),
  traces: collabTraces,
});

check("模型：同层步骤算作同一波并行", parallelGraph.waves[0].length === 2, `实际 ${parallelGraph.waves[0]?.length}`);
check("模型：并行波次的节点带 parallel 标记", parallelGraph.nodes[0].parallel && !parallelGraph.nodes[2].parallel);
check("模型：上游一波多节点时连线口径为并行汇入", parallelGraph.edges.every((edge) => edge.kind === "parallel"));

const parallelLayout = layoutCollaboration(parallelGraph, 720);
const parallelRow = parallelLayout.nodes.filter((node) => node.kind === "agent").slice(0, 2);
check(
  "画布：同波节点摊在同一行",
  parallelRow.length === 2 && parallelRow[0].y === parallelRow[1].y && parallelRow[0].x !== parallelRow[1].x,
  JSON.stringify(parallelRow.map((node) => [node.x, node.y])),
);
check(
  "画布：未跑完的那一步不点亮下一条边",
  // s1/s2 都已完成 → 任务端子到规划节点那条、规划节点到 s1/s2 的两条 + 汇入 s3 的两条都亮；
  // s3 还没跑 → 到交付那条不亮。规划节点恒「已走通」：能画出这些根步骤就说明计划已产出。
  parallelLayout.edges.filter((edge) => edge.active).length === 5,
  `${parallelLayout.edges.filter((edge) => edge.active).length} 条点亮`,
);
check(
  "画布：并行波次画出圈定框",
  parallelLayout.groups.length === 1 && parallelLayout.groups[0].label === "并行协作区",
  JSON.stringify(parallelLayout.groups),
);

const parallel = renderToStaticMarkup(<CollaborationGraph graph={parallelGraph} />);
check("并行波次渲染出圈定框", parallel.includes("cv-group") && parallel.includes("并行协作区"));
check(
  "圈定框包住圆下面的标签，不切一半",
  (() => {
    const box = parallelLayout.groups[0];
    // 只看圈在框里那一行的节点：`box.y` 相对行中心下偏了 12px
    const inRow = parallelLayout.nodes.filter(
      (node) => node.kind === "agent" && Math.abs(node.y - (box.y - 12)) < 1,
    );
    const bottoms = inRow.map((node) => node.y + node.size / 2 + 20 + 16);
    return (
      inRow.length === 2 &&
      bottoms.every((bottom) => bottom <= box.y + box.height / 2)
    );
  })(),
  JSON.stringify(parallelLayout.groups[0]),
);

/* 扇出 → 并行 → 汇聚：三波混合形状。
   上面那条只覆盖「同波两根 + 一起汇入」，验不出「上游一个、下游摊开、后面再收回来」。
   而动态链路真正要表达的正是后者：规划模型按任务把一步拆成两路并行、再合并收尾。
   数据形状与后端 `dynamic_checkpoint_summary` 的输出逐字段一致
   （id / role / depends_on / status），所以这条用例同时锁住前后端契约——
   后端给得出这个形状，画布就必须画成「串行 → 并行 → 串行汇聚」。 */
const fanOutGraph = buildCollaboration({
  stages: collabStages,
  agents: collabAgents,
  workflow: {
    ...collabWorkflow,
    status: "running",
    current_step: null,
    checkpoint: {
      mode: "dynamic",
      status: "running",
      current_step: null,
      completed_steps: ["s1", "s2", "s3"],
      plan: [
        { id: "s1", role: "collector", depends_on: [], status: "completed" },
        { id: "s2", role: "collector", depends_on: ["s1"], status: "completed" },
        { id: "s3", role: "analyst", depends_on: ["s1"], status: "completed" },
        { id: "s4", role: "reporter", depends_on: ["s2", "s3"], status: "pending" },
      ],
    },
  },
  completed: new Set(["s1", "s2", "s3"]),
  traces: null,
});

check(
  "模型：一个上游分叉出的两个下游落在同一波",
  fanOutGraph.waves.map((wave) => wave.length).join(",") === "1,2,1",
  `实际 ${fanOutGraph.waves.map((wave) => wave.length).join(",")}`,
);
check(
  "模型：只有分叉那一波带并行标记",
  !fanOutGraph.nodes[0].parallel &&
    fanOutGraph.nodes[1].parallel &&
    fanOutGraph.nodes[2].parallel &&
    !fanOutGraph.nodes[3].parallel,
);
check(
  "模型：单上游是串行边，多上游是并行汇入",
  fanOutGraph.edges.map((edge) => edge.kind).join(",") === "serial,serial,parallel,parallel",
  fanOutGraph.edges.map((edge) => `${edge.from}->${edge.to}:${edge.kind}`).join(" "),
);

const fanOutLayout = layoutCollaboration(fanOutGraph, 720);
const fanOutAgents = fanOutLayout.nodes.filter((node) => node.kind === "agent");
check(
  "画布：三波各占一行，分叉那一行摊开两个",
  new Set(fanOutAgents.map((node) => node.y)).size === 3 &&
    fanOutAgents.filter((node) => node.y === fanOutAgents[1].y).length === 2,
  JSON.stringify(fanOutAgents.map((node) => [node.x, node.y])),
);
check(
  "画布：只有分叉那一波画并行协作区",
  fanOutLayout.groups.length === 1,
  `实际 ${fanOutLayout.groups.length}`,
);

// 等待态下底部那行得指「下一步是谁」，不能退回到最后一个跑完的（那正是最容易看错的时候）
const partialCanvas = renderToStaticMarkup(
  <CollabCanvas open graph={parallelGraph} onClose={() => undefined} />,
);
check("底部一行在等待态指向下一步", partialCanvas.includes("下一步：报告生成 Agent"));

check(
  "空链路给空态而不是空白",
  renderToStaticMarkup(
    <CollaborationGraph
      graph={buildCollaboration({
        stages: collabStages,
        agents: collabAgents,
        workflow: null,
        completed: new Set(),
      })}
    />,
  ).includes("协作链路"),
);

/* -------------------------------------------------------------------------- */
/* 全屏协作画布                                                                */
/* -------------------------------------------------------------------------- */

const conversations: CollabConversation[] = [
  { id: "w-collab", index: 1, label: "统计 128/2680 的占比", status: "completed" },
  { id: "w-2", index: 2, label: "再算一次 2680/128", status: "running" },
];

let canvas = "";
try {
  canvas = renderToStaticMarkup(
    <CollabCanvas
      open
      graph={collabGraph}
      conversations={conversations}
      selectedId="w-collab"
      onSelect={() => undefined}
      onClose={() => undefined}
    />,
  );
  check("全屏画布可渲染", canvas.length > 800, `长度 ${canvas.length}`);
} catch (cause) {
  check("全屏画布可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("画布用宽档画法", canvas.includes("cv-canvas wide"));
check("画布列出对话编号", canvas.includes("对话 1") && canvas.includes("对话 2"));
check("画布标出当前对话", canvas.includes("collab-conversation current"));
check("画布显示本次任务原文", canvas.includes("统计 128/2680 的占比并说明含义"));
check("画布悬停详情常驻：分配到的任务", canvas.includes("分配到的任务"));
check("画布悬停详情常驻：阶段产出与截断", canvas.includes("阶段产出") && canvas.includes("已截断"));
check("画布悬停详情常驻：本阶段工具调用", canvas.includes("本阶段工具调用"));
check("画布悬停详情常驻：生效参数", canvas.includes("生效参数") && canvas.includes("显式覆盖"));
check("画布悬停详情常驻：Token 消耗", canvas.includes("Token 消耗"));
check(
  "画布悬停详情按「接到什么 → 交出什么 → 用什么跑」排",
  canvas.indexOf("分配到的任务") < canvas.indexOf("阶段产出") &&
    canvas.indexOf("阶段产出") < canvas.indexOf("生效参数") &&
    canvas.indexOf("生效参数") < canvas.indexOf("Token 消耗"),
  "产出被压到滚动区下面就等于没显示——读的人不会为了它往下滚",
);
check(
  "悬停面板贴节点侧面而不是正下方",
  canvas.includes("cv-pop-slot is-right") || canvas.includes("cv-pop-slot is-left"),
  "挂正下方会压住下一段链路，而链路正是要给人看的东西",
);
check(
  "连线上的工具链胶囊带入参出参",
  canvas.includes("cv-edge-chip") && canvas.includes("calculator ×1") && canvas.includes("入参") && canvas.includes("出参"),
  "连线要能回答「这条数据是怎么被加工出来的」",
);
check(
  "工具链浮窗同样贴胶囊侧面",
  canvas.includes("cv-pop-edge is-right") || canvas.includes("cv-pop-edge is-left"),
  "胶囊钉在画布中线上，居中向上弹会把上游那一串节点整个盖掉",
);
check(
  "底部一行报全跑完时的最后一步",
  canvas.includes("全部阶段已完成：报告生成 Agent"),
  canvas.slice(canvas.indexOf("cv-terminal"), canvas.indexOf("cv-terminal") + 120),
);
check("底部一行标注是串行还是并行", canvas.includes("串行流水线"));

// 计划没落盘：侧栏与全屏都只报「正在规划」，一块节点也不摆（否则先画错的、再换成对的）。
const planningCanvas = renderToStaticMarkup(
  <CollaborationGraph graph={draftGraph("dynamic")} />,
);
check(
  "画布：计划没落盘时报「正在规划」而不是画固定三步",
  planningCanvas.includes("正在规划") &&
    !planningCanvas.includes("cv-canvas") &&
    !planningCanvas.includes("信息收集"),
  planningCanvas,
);
const planningFull = renderToStaticMarkup(
  <CollabCanvas open graph={draftGraph("dynamic")} onClose={() => undefined} />,
);
check(
  "画布：全屏视图同样不画固定三步，也不宣称是串行流水线",
  planningFull.includes("正在规划") &&
    planningFull.includes("链路未定") &&
    !planningFull.includes("cv-canvas") &&
    !planningFull.includes("串行流水线"),
  planningFull.slice(0, 300),
);
check("画布不再铺图例说明", !canvas.includes("collab-canvas-legend") && !canvas.includes("这次对话的具体协作工作流"));
check(
  "画布未打开时不渲染",
  renderToStaticMarkup(<CollabCanvas open={false} graph={collabGraph} onClose={() => undefined} />) === "",
);

/* -------------------------------------------------------------------------- */
/* Agent 阶段详情弹窗                                                          */
/* -------------------------------------------------------------------------- */

const agent: Agent = {
  id: "collector",
  name: "信息收集 Agent",
  role: "collector",
  model: "qwen2.5-coder:7b",
  provider: "ollama",
  provider_name: "Ollama（本地）",
  llm_model_id: null,
  temperature: 0.3,
  top_p: null,
  max_output_tokens: 2048,
  reasoning_type: "none",
  status: "idle",
  override_keys: ["temperature"],
  builtin: true,
  description: "整理任务要求与输入资料，为后续分析准备信息。",
  enabled: true,
  tool_names: null,
};

const detail: AgentStageDetail = {
  stageId: "collect",
  stageLabel: "信息收集",
  responsibility: "整理任务要求与输入资料，为后续分析准备信息。",
  status: "pending",
  agent,
  updatedAt: "2026-09-15T08:00:00Z",
};

/** 一条同时含成功与失败调用的轨迹：失败那条最该被读到，所以两种都要有。 */
const trace: StageTraceItem = {
  stage: "collect",
  role: "collector",
  input: null,
  input_from: null,
  output: "已收齐三类原始数据。",
  tool_calls: [
    {
      call_id: "call-1",
      tool_name: "list_session_files",
      status: "succeeded",
      input: { session_id: "s-1" },
      output: ["sales.csv", "returns.csv"],
      error: null,
    },
    {
      call_id: "call-2",
      tool_name: "sql_query",
      status: "failed",
      input: { sql: "select * from returns" },
      output: null,
      error: "SandboxViolation: 只读沙箱拒绝该语句",
    },
  ],
  truncated: false,
  reason: null,
};

let modal = "";
try {
  modal = renderToStaticMarkup(
    <AgentStageModal detail={detail} onClose={() => undefined} onOpenRecords={() => undefined} />,
  );
  check("Agent 详情弹窗可渲染", modal.length > 300, `长度 ${modal.length}`);
} catch (cause) {
  check("Agent 详情弹窗可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("弹窗是 dialog 且可关闭", modal.includes('role="dialog"') && modal.includes('aria-modal="true"'));
check("弹窗标题为 Agent 名并带阶段副标题", modal.includes("信息收集 Agent") && modal.includes("信息收集阶段 · collect"));
check(
  "阶段状态走 Status 的词汇，pending 不说「排队中」",
  modal.includes("等待") && !modal.includes("排队中"),
  modal.slice(0, 400),
);
check("给出任务记录入口", modal.includes("在任务记录中查看工具调用与指标"));
check(
  "没有轨迹时也要说清原因，而不是留白",
  modal.includes("正在读取") || modal.includes("尚未开始") || modal.includes("执行轨迹"),
  modal.slice(0, 400),
);

/* 弹窗是「执行轨迹」，不是「配置参数」：模型与参数在「Agent 团队」页。 */
let traceModal = "";
try {
  traceModal = renderToStaticMarkup(
    <AgentStageModal
      detail={detail}
      trace={trace}
      onClose={() => undefined}
      onOpenRecords={() => undefined}
    />,
  );
} catch (cause) {
  check("带轨迹的弹窗可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check(
  "弹窗渲染分配到的任务、工具调用与阶段产出",
  traceModal.includes("分配到的任务") &&
    traceModal.includes("list_session_files") &&
    traceModal.includes("已收齐三类原始数据"),
  traceModal.slice(0, 500),
);
check(
  "失败的调用连着原因一起显示",
  traceModal.includes("SandboxViolation") && traceModal.includes("失败"),
);
check(
  "工具调用计数写出来",
  traceModal.includes("2 次工具调用"),
);
check(
  "不再显示模型参数（那是「Agent 团队」页的事）",
  !traceModal.includes("2048 tokens") &&
    !traceModal.includes("Temperature") &&
    !traceModal.includes("覆盖项"),
  traceModal.slice(0, 500),
);
check(
  "说清隐藏推理没有落盘，不假装有思维链",
  traceModal.includes("隐藏推理") && traceModal.includes("Agent 团队"),
);

/* 轨迹主体单独挂载：三种「没有轨迹」与截断提示都要能读到。 */
const gapView = renderToStaticMarkup(
  <AgentTraceView trace={{ ...trace, output: null, tool_calls: [], reason: "该阶段正在执行：轨迹在阶段完成后写入状态存储。" }} />,
);
check(
  "服务端给的原因原样显示，不改写成「暂无数据」",
  gapView.includes("该阶段正在执行") && !gapView.includes("暂无"),
  gapView.slice(0, 300),
);

const dynamicView = renderToStaticMarkup(
  <AgentTraceView trace={null} overallReason="本次执行走的是动态编排链路，它当前不落盘逐步骤执行轨迹。" />,
);
check("动态链路说清为什么不落盘", dynamicView.includes("动态编排链路"));

const clippedView = renderToStaticMarkup(
  <AgentTraceView
    trace={{
      ...trace,
      truncated: true,
      tool_calls: [
        {
          call_id: "c3",
          tool_name: "read_session_file",
          status: "succeeded",
          input: { name: "huge.csv" },
          output: { truncated: true, bytes: 9000, preview: "x" },
          error: null,
        },
      ],
    }}
  />,
);
check(
  "截断要说出来：产出标「已截断」、出参也标",
  clippedView.includes("已截断"),
  clippedView.slice(0, 300),
);

let bare = "";
try {
  bare = renderToStaticMarkup(<AgentStageModal detail={{ ...detail, agent: null }} onClose={() => undefined} />);
  check("角色未就绪时弹窗仍可渲染", bare.includes("collect Agent"));
} catch (cause) {
  check("角色未就绪时弹窗仍可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("没有任务记录入口时不渲染该按钮", !bare.includes("在任务记录中查看工具调用与指标"));

/* -------------------------------------------------------------------------- */
/* 时间戳：历史记录必须能看出是哪一天（原来只给 HH:mm）                          */
/* -------------------------------------------------------------------------- */

const NOW = new Date(2026, 8, 21, 16, 30); // 2026-09-21 16:30 本地时间
check("今天的记录带时刻", formatStamp(new Date(2026, 8, 21, 14, 12).toISOString(), NOW) === "今天 14:12");
check("昨天的记录说「昨天」", formatStamp(new Date(2026, 8, 20, 9, 5).toISOString(), NOW) === "昨天 09:05");
check(
  "更早的记录补上日期",
  formatStamp(new Date(2026, 7, 3, 8, 0).toISOString(), NOW) === "8月3日 08:00",
);
check(
  "跨年记录补上年份",
  formatStamp(new Date(2025, 11, 31, 23, 59).toISOString(), NOW) === "2025年12月31日 23:59",
);
check("空值不显示成空白", formatStamp(null) === "—" && formatStamp(undefined) === "—");
check("无法解析的值原样回显，不显示 Invalid Date", formatStamp("not-a-date") === "not-a-date");

/* -------------------------------------------------------------------------- */
/* 用量统计：原值并排，绝不累加                                                */
/* -------------------------------------------------------------------------- */

const metric = (name: string, value: number, labels: Record<string, unknown>): Metric => ({
  metric_name: name,
  value,
  labels,
  recorded_at: "2026-09-15T08:00:00Z",
});

const groups = groupUsage([
  metric("input_tokens", 1200, { agent_id: "collector" }),
  metric("input_tokens", 1300, { agent_id: "collector" }),
  metric("output_tokens", 480, { agent_id: "collector" }),
  metric("workflow_duration_ms", 42500, {}),
  metric("macp_unrelated_ratio", 0.5, {}),
]);

check("按 agent_id 分组", groups.some((g) => g.scope === "collector"));
check("无 agent/model 标签归入任务级", groups.some((g) => g.scope === "任务级"));
check("只保留可读懂的用量指标", !JSON.stringify(groups).includes("macp_unrelated_ratio"));

const collector = groups.find((g) => g.scope === "collector");
const inputRow = collector?.rows.find((r) => r.label === "输入 Token");
check(
  "同一指标多次采样并排列出而不求和",
  inputRow?.values.join("/") === "1,200/1,300",
  JSON.stringify(inputRow?.values),
);
check("耗时换算成秒", JSON.stringify(groups).includes("42.5s"));

let usage = "";
try {
  usage = renderToStaticMarkup(<TaskUsagePanel groups={groups} loading={false} error="" onOpenRecords={() => undefined} />);
  check("用量面板可渲染", usage.length > 200, `长度 ${usage.length}`);
} catch (cause) {
  check("用量面板可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("多次采样带标记", usage.includes("多次采样"));
check("面板带任务记录入口", usage.includes("查看逐条采样与工具调用"));

check(
  "空采样给口径说明而不是 0",
  renderToStaticMarkup(<TaskUsagePanel groups={[]} loading={false} error="" />).includes("不计为 0"),
);

/* -------------------------------------------------------------------------- */
/* 多模态附件（ADR-021）                                                       */
/* -------------------------------------------------------------------------- */

// 前端那份「可接受类型 / 上限」只是给用户即时反馈用的，准入判据在服务端
// （`app/attachments/spec.py`）。两边口径一散就会「前端放行、后端 400」或者反过来
// 「前端拦掉后端本来收得下的文件」，所以这里钉的是**口径**而不是实现：
// 同一批扩展名、同一个 5 MB、同一个 4 个。
check(
  "前端扩展名表覆盖图片/代码/文档三类",
  ["png", "py", "docx", "xlsx", "pdf"].every((ext) =>
    (ATTACHMENT_EXTENSIONS as readonly string[]).includes(ext),
  ),
);
check("附件数量上限与后端一致", MAX_ATTACHMENT_COUNT === 4);
check("单文件上限与后端一致（5 MB）", MAX_ATTACHMENT_BYTES === 5 * 1024 * 1024);

check("图片按扩展名归类", classifyLocal("截图.PNG") === "image");
check("文档按扩展名归类", classifyLocal("季度报告.docx") === "document");
check("代码按文本归类", classifyLocal("pipeline_graph.py") === "text");
check(
  "不支持的格式不静默放行",
  classifyLocal("archive.zip") === "unsupported",
  classifyLocal("archive.zip"),
);

const seats: PendingAttachment[] = Array.from({ length: MAX_ATTACHMENT_COUNT }, (_, i) => ({
  key: `k${i}`,
  name: `f${i}.txt`,
  size: 1024,
  kind: "text",
  state: "ready",
}));

check(
  "空文件被拒",
  (rejectionReason({ name: "empty.txt", size: 0 }, []) ?? "").includes("空文件"),
);
check(
  "超限文件被拒且指名是哪个文件",
  (rejectionReason({ name: "big.pdf", size: 6 * 1024 * 1024 }, []) ?? "").includes("big.pdf"),
  rejectionReason({ name: "big.pdf", size: 6 * 1024 * 1024 }, []) ?? "未拒绝",
);
check(
  "占满名额后继续选被拒",
  (rejectionReason({ name: "extra.txt", size: 10 }, seats) ?? "").includes("最多"),
  rejectionReason({ name: "extra.txt", size: 10 }, seats) ?? "未拒绝",
);
check("合法文件放行", rejectionReason({ name: "ok.txt", size: 10 }, []) === null);

check("文件名折叠保留扩展名", shortenName("一个很长的文件名用来测试折叠行为.docx").endsWith(".docx"));
check("体积按 KB/MB 换算", formatBytes(1536) === "1.5 KB" && formatBytes(3 * 1024 * 1024) === "3.0 MB");

const chip = (
  key: string,
  state: PendingAttachment["state"],
  extra: Partial<PendingAttachment> = {},
): PendingAttachment => ({ key, name: `${key}.txt`, size: 2048, kind: "text", state, ...extra });

const chips = renderToStaticMarkup(
  <PendingFileChips
    items={[
      chip("upload", "uploading", { name: "现场.png", kind: "image" }),
      chip("ready", "ready", { name: "季度报告.docx", kind: "document", size: 1536 }),
      chip("failed", "failed", { name: "扫描件.pdf", kind: "document", error: "未能解析出正文" }),
    ]}
    onRemove={() => undefined}
  />,
);
check("待发附件三种状态都渲染", chips.includes("上传中") && chips.includes("1.5 KB"));
check(
  "可见文案随状态切换：上传中 / 失败原因 / 体积",
  chips.includes("<small>上传中…</small>") &&
    chips.includes("<small>未能解析出正文</small>") &&
    chips.includes("<small>1.5 KB</small>"),
  chips.slice(0, 400),
);
check("上传失败就地写出原因", chips.includes("未能解析出正文"));
check(
  "每个条目带指名到文件的移除按钮",
  chips.includes('aria-label="移除 扫描件.pdf"') && chips.includes('aria-label="移除 现场.png"'),
);
check(
  "没有待发附件时不渲染空容器",
  renderToStaticMarkup(<PendingFileChips items={[]} onRemove={() => undefined} />) === "",
);

const sentAttachment = (over: Partial<Attachment>): Attachment => ({
  id: "att-1",
  session_id: "s-1",
  message_id: "m-1",
  name: "photo.png",
  mime: "image/png",
  size_bytes: 20480,
  kind: "image",
  status: "ready",
  error: null,
  has_original: true,
  created_at: "2026-09-16T10:00:00Z",
  ...over,
});

const sent = renderToStaticMarkup(
  <MessageAttachmentList
    items={[
      sentAttachment({ id: "img-1", name: "现场.png" }),
      sentAttachment({ id: "doc-1", name: "季度报告.docx", kind: "document", size_bytes: 40960 }),
      sentAttachment({
        id: "bad-1",
        name: "扫描件.pdf",
        kind: "document",
        status: "failed",
        error: "未能解析出正文，已跳过内容。",
      }),
      // 原件留档策略（ADR-024）之前落库的行：没有字节，不该给打开原件的入口。
      sentAttachment({ id: "legacy-1", name: "会议速记.txt", kind: "text", has_original: false }),
    ]}
  />,
);
check(
  "图片附件给可点原图链接",
  sent.includes("/api/v1/attachments/img-1/content") && sent.includes("<img"),
  sent.slice(0, 400),
);
check("非图片附件不渲染 img（正文是抽取出来的文本，没有可看的图）", !sent.includes('src="/api/v1/attachments/doc-1/content"'));
check("已发送附件写明类型与体积", sent.includes("文档 · 40.0 KB"));
check(
  "解析失败的附件显式说明原因",
  sent.includes("未能解析出正文，已跳过内容。") && sent.includes("message-attachment failed"),
);
check(
  "失败项没有 error 时给兜底文案而不是空白",
  describeAttachment(sentAttachment({ status: "failed", error: null })).includes("已跳过内容"),
);
// ADR-027：`ready` 也可能带一句**降级说明**（扫描版 PDF 改用页面图像提供正文）。
// 服务端把这个字段当「解析成功但有话要说」用，界面必须转述——报 ready 却什么都不说，
// 用户会以为正文是被正常提取出来的。
check(
  "解析成功但有降级说明时显示该说明",
  describeAttachment(
    sentAttachment({
      status: "ready",
      kind: "document",
      name: "扫描件.pdf",
      error: "这份 PDF 没有文本层（扫描件），已改用页面图像提供正文。",
    }),
  ).includes("页面图像"),
  describeAttachment(sentAttachment({ kind: "text" })),
);
check(
  "没有降级说明的附件仍按「类型 · 体积」显示",
  describeAttachment(
    sentAttachment({ kind: "document", size_bytes: 40960, error: null }),
  ) === "文档 · 40.0 KB",
);
check("没有附件时不渲染空容器", renderToStaticMarkup(<MessageAttachmentList items={[]} />) === "");
// 原件留档（ADR-024）：所有类型都能拿回原件，且图片与文档的语义要分开。
check(
  "非图片附件也能打开原件，并带下载文件名",
  sent.includes('href="/api/v1/attachments/doc-1/content"') &&
    sent.includes('download="季度报告.docx"'),
  sent.slice(0, 700),
);
check(
  "图片是「看一眼」不带 download 属性",
  !/download="[^"]*现场\.png"/.test(sent),
  sent.slice(0, 700),
);
check(
  "没有原件的历史行不给任何入口",
  sent.includes("会议速记.txt") && !sent.includes('href="/api/v1/attachments/legacy-1/content"'),
  sent.slice(-600),
);

/* -------------------------------------------------------------------------- */
/* 消息正文：Markdown 渲染（components/Markdown.tsx）                           */
/* -------------------------------------------------------------------------- */

// 覆盖「模型真的会写出来」的五种结构。断言里刻意查**记号是否消失**，而不只是查元素是否存在——
// 病根是原先 `<p>{content}</p>` 把 `**` `##` `|` 原样吐出来，只查 `<h2>` 存在会漏掉这一点。
const MD_SAMPLE = [
  "## 结论",
  "",
  "占比 **4.78%**，低于阈值，见 `128/2680`。",
  "",
  "| 项目 | 数值 |",
  "| --- | --- |",
  "| 退货 | 128 |",
  "",
  "- [x] 已核对",
  "- [ ] 待补充",
  "",
  "```python",
  'print("hi")',
  "```",
  "",
  "> 引用一段",
].join("\n");

let md = "";
try {
  md = renderToStaticMarkup(<Markdown>{MD_SAMPLE}</Markdown>);
  check("Markdown 可渲染", md.length > 200, `长度 ${md.length}`);
} catch (cause) {
  check("Markdown 可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("标题渲染成 h2 而不是带井号的文本", md.includes("<h2") && !md.includes("## "), md.slice(0, 200));
check("加粗渲染成 strong", md.includes("<strong>4.78%</strong>"));
check("行内代码单独成码", md.includes("md-code") && md.includes("128/2680"));
check(
  "表格带自己的横向滚动容器",
  md.includes('class="md-table-wrap"') && md.includes("<table") && md.includes("退货"),
  "整段正文一起滚会把上面的段落推出视野",
);
check("代码块带语言角标", md.includes('data-lang="python"') && md.includes("<pre"), md.slice(-300));
check("GFM 任务列表的勾选框只读", md.includes('type="checkbox"') && md.includes("disabled"));
check("引用块渲染成 blockquote", md.includes("<blockquote") && md.includes("引用一段"));
check(
  "正文里不该再出现未渲染的 Markdown 记号",
  !md.includes("**") && !md.includes("| ---"),
  md.slice(0, 300),
);
// 离屏渲染不跑 effect，也不该播放渐进揭示——若初始值写 0 就会渲染出空正文，
// 冒烟与首帧都会拿到空白。这条同时锁住「非浏览器环境降级为全文」。
check(
  "离屏渲染直接给全文、不播动画",
  md.includes("占比") && md.includes("引用一段") && !md.includes("is-streaming"),
  "非浏览器环境必须降级为立即全文",
);

/* -------------------------------------------------------------------------- */
/* 折叠块（components/Disclosure.tsx）                                          */
/* -------------------------------------------------------------------------- */

const openBox = renderToStaticMarkup(
  <Disclosure id="d-open" title="数据分析 Agent" open onToggle={() => undefined}>
    <p>正文</p>
  </Disclosure>,
);
check(
  "展开态带 aria-expanded 与 aria-controls",
  openBox.includes('aria-expanded="true"') && openBox.includes('aria-controls="d-open"'),
  openBox.slice(0, 200),
);
check("展开态渲染正文", openBox.includes("正文") && openBox.includes("disclosure-body"));

const shutBox = renderToStaticMarkup(
  <Disclosure id="d-shut" title="数据分析 Agent" open={false} onToggle={() => undefined}>
    <p>正文</p>
  </Disclosure>,
);
check(
  "收起态不渲染正文，也不写指向空元素的 aria-controls",
  !shutBox.includes("正文") &&
    shutBox.includes('aria-expanded="false"') &&
    !shutBox.includes("aria-controls"),
  "指向不存在的 id 会让人以为内容只是被隐藏了",
);
check(
  "折叠块是按钮而不是不可达的 div",
  shutBox.includes("<button") && shutBox.includes("disclosure-head"),
);

/* -------------------------------------------------------------------------- */
/* 对话流内联执行轨迹（workspace/RunActivity.tsx）                              */
/* -------------------------------------------------------------------------- */

let doneRun = "";
try {
  doneRun = renderToStaticMarkup(
    <RunActivity
      workflow={collabWorkflow}
      traces={collabTraces}
      stages={collabStages}
      agents={collabAgents}
      completed={new Set(["collect", "analyze", "report"])}
    />,
  );
  check("跑完的执行过程可渲染", doneRun.length > 120 && doneRun.includes("run-chain-head"), `长度 ${doneRun.length}`);
} catch (cause) {
  check("跑完的执行过程可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
// 跑完只留一行：步数与工具次数是数出来的，用时是 created_at → completed_at 算出来的
// （fixture 正好 10:00:00Z → 10:00:30Z）。三项都不靠猜，所以能钉死。
check(
  "跑完收成一行：步数 / 工具次数 / 用时",
  doneRun.includes("已执行 3 个阶段") &&
    doneRun.includes("1 次工具调用") &&
    doneRun.includes("用时 30 秒"),
  doneRun.slice(0, 300),
);
check(
  "跑完默认收起，正文一步都不铺开",
  !doneRun.includes("run-steps") && !doneRun.includes("disclosure-body"),
  "整屏铺开等于把「过程」又变回「流水账」；网页 AI 也是跑完就收起",
);
check(
  "收起态不写指向空正文的 aria-controls",
  doneRun.includes('aria-expanded="false"') && !doneRun.includes("aria-controls"),
  "指向不存在的 id 会让人以为内容只是被隐藏了",
);
// 用户 2026-09-22 反馈「专门卡片区域太突兀」——卡片外壳与「任务分配」块都该没了。
check(
  "内联形态没有卡片外壳，也不再重复画布上的任务分配",
  !doneRun.includes("run-activity-head") &&
    !doneRun.includes("run-assignment") &&
    !doneRun.includes("任务分配"),
  "任务分配已由协作画布的 planner 节点承载（ADR-032），两个面各画一遍正是突兀的来源",
);
check(
  "卡片不谎称有思维链，也不再写口径脚注",
  !doneRun.includes("思维链") &&
    !doneRun.includes("隐藏推理") &&
    !doneRun.includes("本次任务："),
  "评审要求删掉两条解释脚注；卡片只呈现落盘过的事实，口径由执行台弹窗承载（doc/api.md §7）",
);

// 「0 秒」读起来像坏了。库里确有 `created_at == completed_at` 的早期行，那是数据的事，
// 前端如实说「不到一秒」，不写「0 秒」也不替它圆成别的数。
const instantRun = renderToStaticMarkup(
  <RunActivity
    workflow={{
      ...collabWorkflow,
      created_at: "2026-09-21T10:00:00Z",
      updated_at: "2026-09-21T10:00:00Z",
      completed_at: "2026-09-21T10:00:00Z",
    }}
    traces={collabTraces}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set(["collect", "analyze", "report"])}
  />,
);
check(
  "不足一秒写「< 1 秒」而不是「0 秒」",
  instantRun.includes("1 秒") && !instantRun.includes("0 秒"),
  instantRun.slice(0, 200),
);

// 正在跑的那一步必须自动摊开：人要看的就是它。
const runningRun = renderToStaticMarkup(
  <RunActivity
    workflow={{
      ...collabWorkflow,
      status: "running",
      current_step: "analyze",
      checkpoint: { status: "running", current_step: "analyze", completed_steps: ["collect"] },
    }}
    traces={collabTraces}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set(["collect"])}
  />,
);
check(
  "执行中只摊开正在跑的那一步",
  (runningRun.match(/disclosure-body/g) ?? []).length === 1,
  `摊开了 ${(runningRun.match(/disclosure-body/g) ?? []).length} 步`,
);
check(
  "执行中自动摊开外层那一行",
  runningRun.includes("run-chain-body") && runningRun.includes('aria-expanded="true"'),
  "运行中不收起来：人要看的就是它",
);
check(
  "三个 Agent 各自成一行",
  ["信息收集 Agent", "数据分析 Agent", "报告生成 Agent"].every((n) => runningRun.includes(n)),
);
check(
  "每步那一行仍写清阶段名、工具次数与状态",
  runningRun.includes("run-step-label") &&
    runningRun.includes("run-step-tools") &&
    runningRun.includes("run-step-state"),
  "收起后这几样必须还在，否则收起就是信息丢失",
);
check(
  "摊开的正文写明输入来自哪一步、并给出这一步的产出",
  runningRun.includes("分配到的任务（来自collect阶段）") &&
    runningRun.includes("该比例说明退货在总量中占比偏低"),
  runningRun.slice(0, 400),
);
// 同一份字段在两处用两套词，读的人会以为它们是两件事——这正是本仓库反复要避免的漂移。
// 两条渲染路径同时断言，避免「只在其中一处改了措辞」。
check(
  "对话流与执行台弹窗用同一套小标题",
  ["分配到的任务", "执行轨迹", "阶段产出"].every(
    (label) => traceModal.includes(label) && runningRun.includes(label),
  ),
  `弹窗缺 ${["分配到的任务", "执行轨迹", "阶段产出"].filter((l) => !traceModal.includes(l))} / 对话流缺 ${["分配到的任务", "执行轨迹", "阶段产出"].filter((l) => !runningRun.includes(l))}`,
);
check("执行中的那一步标成「执行中」", runningRun.includes("执行中"));
// 摊开的那一步没有工具调用时说清「直接由模型产出结论」，而不是留一个空列表。
check(
  "没有工具调用的步骤给出说明而不是空列表",
  runningRun.includes("没有调用工具") && !runningRun.includes("ws-trace-list"),
  runningRun.slice(0, 400),
);
check("阶段产出按 Markdown 渲染", runningRun.includes("md-body"));

// 失败的工具调用：这条最该被读到，必须连着原因一起出来。
const failedRun = renderToStaticMarkup(
  <RunActivity
    workflow={{
      ...collabWorkflow,
      status: "running",
      current_step: "collect",
      checkpoint: { status: "running", current_step: "collect", completed_steps: [] },
    }}
    traces={{
      ...collabTraces,
      items: [
        {
          ...collabTraces.items[0],
          output: null,
          tool_calls: [
            {
              call_id: "c-9",
              tool_name: "sql_query",
              status: "failed",
              input: { sql: "select 1" },
              output: null,
              error: "SandboxViolation: 只读沙箱拒绝该语句",
            },
          ],
        },
      ],
    }}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set()}
  />,
);
check(
  "失败调用连着原因一起显示",
  failedRun.includes("SandboxViolation") && failedRun.includes("失败"),
);
// 工具调用的入参出参折叠入口由 TraceParts 提供：这一段是「它到底干了什么」的答案。
check(
  "工具调用带入参与出参入口",
  failedRun.includes("入参与出参") && failedRun.includes("sql_query") && failedRun.includes("select 1"),
);
check(
  "还没产出时说清还没跑到，而不是留白",
  failedRun.includes("产出尚未写入"),
  failedRun.slice(0, 400),
);

// 动态链路不落盘逐步骤轨迹：原因说一次就够，不要每步重复一遍。
const gapRun = renderToStaticMarkup(
  <RunActivity
    workflow={{
      ...collabWorkflow,
      status: "running",
      current_step: "s2",
      checkpoint: {
        mode: "dynamic",
        status: "running",
        current_step: "s2",
        completed_steps: ["s1"],
        plan: [
          { id: "s1", role: "collector", depends_on: [], status: "completed" },
          { id: "s2", role: "analyst", depends_on: ["s1"], status: "pending" },
        ],
      },
    }}
    traces={{
      workflow_id: "w-collab",
      mode: "dynamic",
      task: "统计 128/2680 的占比",
      availability: "not_integrated",
      reason: "本次执行走的是动态编排链路，它当前不落盘逐步骤执行轨迹。",
      items: [],
    }}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set(["s1"])}
  />,
);
check(
  "整条链路没有轨迹时原因只出现一次",
  (gapRun.match(/不落盘逐步骤执行轨迹/g) ?? []).length === 1,
  `出现 ${(gapRun.match(/不落盘逐步骤执行轨迹/g) ?? []).length} 次`,
);
check(
  "轨迹缺席也不留白：步骤仍按计划声明出来",
  gapRun.includes("信息收集 Agent") &&
    gapRun.includes("数据分析 Agent") &&
    gapRun.includes("run-note"),
  gapRun.slice(0, 400),
);

// 规划窗口里没有「步」可列：这一行只报在规划，也不该长出一个点了没反应的开关。
const planningRun = renderToStaticMarkup(
  <RunActivity
    workflow={{ ...collabWorkflow, status: "running", current_step: null, checkpoint: null }}
    traces={null}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set()}
    requestedMode="dynamic"
  />,
);
check(
  "内联轨迹：计划没落盘时只报在规划，不摆固定三步",
  planningRun.includes("正在规划任务分配") &&
    !planningRun.includes("信息收集") &&
    !planningRun.includes("run-steps") &&
    !planningRun.includes("<button"),
  planningRun,
);
const staticDraftRun = renderToStaticMarkup(
  <RunActivity
    workflow={{ ...collabWorkflow, status: "running", current_step: null, checkpoint: null }}
    traces={null}
    stages={collabStages}
    agents={collabAgents}
    completed={new Set()}
    requestedMode="static"
  />,
);
check(
  "内联轨迹：按静态提交时照旧列出固定三步",
  staticDraftRun.includes("信息收集") && !staticDraftRun.includes("正在规划"),
  staticDraftRun.slice(0, 300),
);

// 单条工具调用的渲染搬到了 TraceParts，弹窗与对话流共用一份：
// 两处各写一份必然出现「弹窗标了已截断、对话流把预览当成全部」。
const sharedStep = renderToStaticMarkup(
  <ToolCallBlock
    call={{
      call_id: "c-shared",
      tool_name: "read_session_file",
      status: "succeeded",
      input: { name: "huge.csv" },
      output: { truncated: true, bytes: 9000, preview: "x" },
      error: null,
    }}
  />,
);
check(
  "共享的工具调用块保留截断标记",
  sharedStep.includes("已截断") && sharedStep.includes("huge.csv"),
);

/* -------------------------------------------------------------------------- */
/* 静态断言：假选择已删除、职责边界已写进注释                                   */
/* -------------------------------------------------------------------------- */

try {
  const styles = readFileSync("src/styles.css", "utf8");
  check(
    "样式表已移除主决策 Agent 下拉",
    !styles.includes(".decision-agent-selector") && !styles.includes(".decision-menu"),
    "残留的 .decision-* 规则说明假选择没删干净",
  );
  check("样式表保留输入区提示容器", styles.includes(".composer-hint"));

  // 减弱动态效果不该把加载指示器冻成静止图标：「不动」与「卡住了」在屏幕上长得一模一样。
  // 减动画要减的是**旋转与位移**这类会引发前庭不适的动作，不是「还在跑」这条信息。
  check(
    "减弱动态效果下加载指示器仍有动画",
    /prefers-reduced-motion:reduce\)[\s\S]*?\.spin\s*\{\s*animation:spin-breathe/.test(styles) &&
      styles.includes("@keyframes spin-breathe"),
    "全局 animation:none!important 会把所有转圈图标一起冻住",
  );

  // 画布样式在 workspace/workspace.css，不在 styles.css —— 读错文件会让断言恒真。
  const canvasStyles = readFileSync("src/workspace/workspace.css", "utf8");
  check(
    "规划中的提示行有独立样式",
    canvasStyles.includes(".cv-planning"),
    "计划没落盘时的「正在规划」不能落回 .cv-empty 的文案，两者说的不是一回事",
  );

  const app = readFileSync("src/App.tsx", "utf8");
  check(
    "App 不再出现「主决策」「自动分配」",
    !app.includes("主决策") && !app.includes("自动分配"),
    "composer 里不应再有用户可指定主 Agent 的入口",
  );
  check(
    "执行台卡片点击打开阶段弹窗而非侧栏",
    // 静态阶段走 setDetailStage，动态步骤（id 不属于 StageId 集合）直接组装 setDetailNode；
    // 两条路都进弹窗，都不许把右侧任务级边栏顶开。
    app.includes("setDetailStage(node.stage)") &&
      app.includes("setDetailNode({") &&
      !app.includes("setInspectorOpen(true)"),
  );
  const inspection = readFileSync("src/records/Inspection.tsx", "utf8");
  check(
    "工具调用与采样明细不再合体成工作台组件",
    !app.includes("WorkflowInspection") && !inspection.includes("WorkflowInspection"),
    "逐条明细只应留在任务记录页，合体组件应已删除",
  );
  // 判据要是**接线**，不能是名字：`collaborationWaves` 现在只剩注释里有（那段注释还在
  // 解释它为什么被搬走），拿它当判据是一条恒真的断言——改坏了也照样绿。
  check(
    "协作链路按波次模型渲染",
    (app.match(/buildCollaboration\(\{/g) ?? []).length === 2 && app.includes("dockNodes("),
    "链路模型只在 workspace/collaboration.ts，侧栏与全屏两处都该调它",
  );
  // 「正在规划」要成立，App 必须把**提交时声明的模式**传下去：服务端在规划窗口内没有
  // 任何字段能说明这次走的是动态编排，漏传这条线就会静默退回「先画固定三步」。
  check(
    "规划判据接上了提交时声明的编排模式",
    app.includes("const planning = isPlanning(workflow, mode);") &&
      app.includes("requestedMode: mode,") &&
      app.includes("requestedMode: isLive ? mode : undefined,") &&
      app.includes("requestedMode={mode}") &&
    "四条接线缺一处，画布就会把固定三步当成事实",
  );

  // —— 会话生命周期（`doc/api.md` §4.2 / §5.13 / §5.14）——
  // 会话只在提交首条消息时落库；初始化、新建任务、删除回退都不得建会话，否则每次刷新
  // 都会在历史里留下一条空会话（实测一次开发期积累 108 条空会话 vs 19 条真实会话）。
  const createCalls = (app.match(/api\.createSession\(/g) ?? []).length;
  check(
    "只在提交首条消息时创建会话",
    createCalls === 1,
    `App.tsx 里 api.createSession 调用 ${createCalls} 处，应只有 submit 里那一处`,
  );
  check(
    "草稿态重置不请求后端",
    app.includes("const startNewTask = () =>") && !app.includes("createNewTask"),
    "「新建任务」与「删除当前会话」都应走纯前端重置的 startNewTask",
  );
  check(
    "草稿态下输入区仍可用",
    app.includes('disabled={session?.status === "paused"}') &&
      !app.includes('disabled={!session || session.status === "paused"}'),
    "session 为 null 是草稿态，不能把输入框与发送键禁掉",
  );
  check(
    "删除成功后重拉历史列表",
    app.includes("await onDeleteSession(item);"),
    "侧栏下拉只在展开时拉过一次，删完不重拉就会「删了还在」",
  );
  const recordsSource = readFileSync("src/records/RecordsPage.tsx", "utf8");
  check(
    "记录页先删后拉、不与删除并发",
    recordsSource.includes("await onDelete(item);"),
    "并发会导致删除请求还没落地就重新拉取，拿回旧列表",
  );

  // —— 欢迎区引导卡与附件入口的版式（用户 2026-09-16 反馈）——
  // 卡片是「任务原型」不是三个功能按钮：每张卡都要写出自己的协作形态，
  // 而协作形态只有在规划 Agent 真的参与时才成立，所以点卡必须同时把策略切到「按任务规划」。
  check(
    "欢迎卡片各自写出协作形态",
    ["通常 1 个 Agent 直答", "调查 → 分析 → 总结", "多步核对与整理", "两路并行 → 汇聚"].every((shape) =>
      app.includes(shape),
    ),
  );
  check(
    "点卡片同时切到「按任务规划」",
    app.includes('onChoose(p.text, "dynamic")'),
    "只填文字不换模式，卡片上的协作形态在当前链路下就不成立",
  );
  check(
    "填提示词与设模式在同一个函数里",
    app.includes("promptMode: OrchestrationMode") && app.includes("setMode(promptMode)"),
  );
  check("输入区显式写出当前策略", app.includes("welcome-note") && styles.includes(".welcome .welcome-note"));
  check(
    "旧横排卡片规则已清理",
    !styles.includes(".suggestions button"),
    "残留的 .suggestions button 会把新卡的图标撑成整宽",
  );

  /* —— 策略控件：这次执行的「计划从哪来」（ADR-037）——
     项目里没有两套并列的编排模式：固定链是动态路径的退化情形，计划不由规划节点产出，
     而是一条常量链。所以控件不能再是两个平级分段——那正是把「固定链」摆成了对等策略。 */
  check(
    "策略控件是单按钮 + 浮层，不再是两个平级分段",
    app.includes("composer-strategy-trigger") &&
      app.includes("composer-strategy-menu") &&
      !app.includes("composer-mode"),
    "平级分段把固定链摆成对等策略，正是这次要取消的特例化",
  );
  check(
    "固定链降级为浮层里的次要项",
    app.includes('label: "固定链"') &&
      app.includes("secondary: true") &&
      styles.includes(".composer-strategy-item.is-secondary"),
    "次要项没有专属样式，降级在界面上就看不出来",
  );
  check(
    "浮层按需挂载，收起时不渲染空容器",
    app.includes("{strategyOpen ? ("),
    "常驻的空容器会让「有没有浮层」这件事从 DOM 上读不出来",
  );
  check(
    "aria-controls 只在展开时指向真实元素",
    app.includes("aria-controls={strategyOpen ? STRATEGY_MENU_ID : undefined}"),
    "收起时指向不存在的 id，会让人以为内容只是被隐藏了",
  );
  check(
    "计划来源的四条判据各归其位",
    planSourceKey({ planning: true }) === "planning" &&
      planSourceKey({ mode: "dynamic", source: "llm" }) === "planned" &&
      planSourceKey({ mode: "dynamic", source: "fallback" }) === "fallback" &&
      planSourceKey({ mode: "static" }) === "fixed" &&
      planSourceKey({}) === "fixed",
    "判据顺序错了会把「规划失败回退」读成「规划成功产出」",
  );
  const canvasSource = readFileSync("src/workspace/CollabCanvas.tsx", "utf8");
  check(
    "画布与记录页共用同一条计划来源口径",
    canvasSource.includes("planSourceKey(") &&
      recordsSource.includes("planSourceKey(") &&
      !canvasSource.includes('graph.mode === "dynamic"') &&
      !recordsSource.includes('trace.mode === "dynamic"'),
    "各写一份 mode 三目，就会出现「这边说自动编排、那边说动态编排」的两套说法",
  );
  check(
    "画布节点不再可拖",
    !canvasStyles.includes("cursor: grab") && !canvasStyles.includes("cursor:grab"),
    "过程可视化里位置在表达流程顺序，能拖只会让人以为拖动会改变什么",
  );
  check(
    "悬停面板的侧向定位规则已落盘",
    canvasStyles.includes(".cv-pop-slot.is-right") && canvasStyles.includes(".cv-pop-slot.is-left"),
    "缺一条就只有一个方向能弹，靠边那侧的节点会把面板顶出画布",
  );
  check(
    "侧栏档的面板规则已删除",
    !canvasStyles.includes(".cv-canvas.dense .cv-pop"),
    "侧栏不再有悬停浮层，留着这条规则会让「侧栏不给面板」变成口头约定",
  );
  check(
    "工具链浮窗不再挂在胶囊正上方",
    canvasStyles.includes(".cv-pop-edge.is-right") &&
      canvasStyles.includes(".cv-pop-edge.is-left") &&
      !canvasStyles.includes("bottom: calc(100% + 8px)"),
    "`bottom:100%` 是居中向上弹；胶囊在中线上，那样必盖住上游节点",
  );
  check(
    "浮窗的两条高度约束取小而不是互相顶掉",
    canvasStyles.includes("--cv-pop-max: 520px") &&
      canvasStyles.includes("min(var(--cv-pop-max), var(--cv-pop-fit"),
    "内联写 max-height 会静默顶掉 520px 这条设计上限（本轮实测被顶成 532px）",
  );
  // 关掉「块级按钮里 svg 按 baseline 坐」这处 2px 偏差。断言切片到规则块内再查属性，
  // 只查全文 contains 会被文件里别处的同名属性蒙过去。
  const closeRule = canvasStyles.slice(canvasStyles.indexOf(".collab-canvas-head .cfg-quiet"));
  const closeBlock = closeRule.slice(0, closeRule.indexOf("}"));
  check(
    "关闭按钮的图标与文字同中线",
    closeBlock.includes("inline-flex") && closeBlock.includes("align-items: center"),
    "lucide 的 svg 默认 vertical-align:baseline，块级按钮里图标会比文字高 2px",
  );
  // 侧栏被会话标题顶宽：`.sidebar` 是 `.app-shell` 的 flex item，`min-width:auto` 会让
  // 内容的最小宽度压过 `flex-basis`（实测 214 → 1093px）。同样只认**基础块内**那一条，
  // 后面还有三处 `.sidebar` 覆盖定义，查全文会假绿。
  const sidebarBase = styles.slice(styles.indexOf(".sidebar {"));
  const sidebarBlock = sidebarBase.slice(0, sidebarBase.indexOf("}"));
  check(
    "侧栏宽度不被会话标题顶宽",
    sidebarBlock.includes("min-width:0") || sidebarBlock.includes("min-width: 0"),
    "缺这条，长会话标题会把 214px 的侧栏撑到 1000px 开外",
  );

  // 构建期出过一次「CSS 规则被压缩器静默丢掉」的事故（只打 WARNING、退出码仍是 0），
  // 所以新界面引用的每个类名都要在样式表里有定义，缺一个就说明有规则没落盘。
  const requiredClasses = [
    ".suggestion-icon",
    ".suggestion-shape",
    ".composer-attach",
    ".composer-files",
    ".composer-file.failed",
    ".conversation-composer.dragging",
    // 策略控件：浮层、「兜底」项的层级各要一条——兜底项没有专属规则，降级就不可见，
    // 而那正是这次要改的东西（ADR-037）。
    ".composer-strategy-trigger",
    '.composer-strategy-trigger[aria-expanded="true"]',
    ".composer-strategy-menu",
    ".composer-strategy-item.is-secondary",
    ".message-attachments",
    ".message-attachment-thumb",
    // 原件留档后条目主体是 <a>（ADR-024）：没有这条规则会退化成蓝字下划线。
    "a.message-attachment",
    // 消息正文的 Markdown 版式：少一条就会出现「某类结构退化成浏览器默认样式」，
    // 而这种退化不会报错，只是看着不对。
    ".md-body",
    ".md-body blockquote",
    ".md-code",
    ".md-pre",
    ".md-table-wrap",
    ".md-body.is-streaming",
    // 折叠块与对话流内联执行轨迹。
    ".disclosure-head",
    ".disclosure-body",
    ".disclosure.is-open",
    ".run-activity",
    ".run-chain-head",
    ".run-chain-body",
    ".run-steps",
    ".run-step-tools",
    ".run-step-state",
    ".run-note",
  ];
  const missingClasses = requiredClasses.filter((sel) => !styles.includes(sel));
  check(
    "新增界面引用的类名都有样式定义",
    missingClasses.length === 0,
    `缺 ${missingClasses.join(" / ")}`,
  );
  // 附件条目的**类名留给状态**（uploading/ready/failed），类型写在 `data-kind` 上。
  // 写成 `.message-attachment.text` 这类选择器不会报错、只是永远不命中（初版踩过）。
  check(
    "附件类型按 data-kind 选择",
    styles.includes('.message-attachment[data-kind="text"]') &&
      styles.includes('.composer-file[data-kind="image"]'),
    "类型在 data-kind 上，用 .message-attachment.text 之类的选择器永远命不中",
  );

  // —— 消息正文渲染（Markdown + 渐进揭示）——
  // 病根是 `<p>{content}</p>` 把 `**` `##` `|` 原样吐出来。断言直接钉住那句旧写法，
  // 否则「Markdown 组件写好了但没接上」这种半成品会静默通过。
  check(
    "消息正文走 Markdown 渲染",
    app.includes("components/Markdown") && app.includes("<Markdown stream={animate}>"),
    "App.tsx 里没有 Markdown 入口",
  );
  check(
    "正文不再直接塞进 <p>",
    !app.includes("<p>{message.content}</p>"),
    "旧的纯文本写法还在，记号就还是会原样吐出来",
  );

  const streamSrc = readFileSync("src/components/useStreamText.ts", "utf8");
  check(
    "渐进揭示在非浏览器环境与减少动效时降级为全文",
    streamSrc.includes("typeof window ===") && streamSrc.includes("prefers-reduced-motion"),
    "少了任一条，离屏渲染会拿到空正文、或用户声明的减少动效被忽略",
  );
  check(
    "渐进揭示写明它是呈现效果而不是流式传输",
    streamSrc.includes("呈现效果") && streamSrc.includes("不是传输协议"),
    "边界不写下来，下一次会有人把它当成「前端已经在收 token」",
  );
  check(
    "长文按总时长反推步长",
    streamSrc.includes("MAX_DURATION_MS") && streamSrc.includes("MS_PER_CHAR"),
    "按固定步长推进会让数千字的报告打十几秒",
  );

  const runSrc = readFileSync("src/workspace/RunActivity.tsx", "utf8");
  check(
    "执行活动卡片不伪造思维链",
    runSrc.includes("不伪造思维链") && !runSrc.includes("思维链："),
    "口径同 doc/api.md §5.17：模型内部隐藏推理没有落盘",
  );
  check(
    "轨迹渲染只有一份实现",
    runSrc.includes('from "./TraceParts"') &&
      readFileSync("src/workspace/AgentStageModal.tsx", "utf8").includes('from "./TraceParts"'),
    "弹窗与对话流各写一份，必然出现「这边标已截断、那边当成全部」",
  );
  check(
    "执行活动插在报告之前而不是整段对话末尾",
    app.includes("activityByRunId.get(m.agent_run_id)") && app.includes("reportIndex < 0"),
    "过程要出现在结果的上一个位置，排到末尾会看起来像另一个任务",
  );
  check(
    "渐进揭示只给新到达的消息",
    app.includes("const revealed = useRef<Set<string>>(new Set())") &&
      app.includes("isFresh("),
    "历史会话整屏逐字重放，读的人会以为任务在重新执行",
  );
  check(
    "旧的 run-event 条已被执行活动卡片取代",
    !app.includes("run-event") && !/\.run-event\s*\{/.test(styles),
    "残留的规则说明旧的那条进度文本没删干净",
  );
} catch (cause) {
  check("读取源文件做静态断言", false, cause instanceof Error ? cause.message : String(cause));
}

/* -------------------------------------------------------------------------- */
/* 工作区审批卡片（§5.20 / §7.1）                                              */
/* -------------------------------------------------------------------------- */

function approval(status: ApprovalStatus, overrides: Partial<Approval> = {}): Approval {
  return {
    id: `ap-${status}`,
    workspace_id: "w-1",
    session_id: "s-1",
    run_id: null,
    kind: "delete",
    target: "reports/old.md",
    reason: "删除工作区内的条目",
    status,
    payload: {},
    decided_by: null,
    requested_at: "2026-09-23T02:01:00Z",
    decided_at: null,
    ...overrides,
  };
}

const pendingApproval = renderToStaticMarkup(
  <ApprovalCard approvals={[approval("pending")]} />,
);
check(
  "审批卡片：待决策时给出目标、理由与两个动作",
  pendingApproval.includes("需要你确认") &&
    pendingApproval.includes("删除 reports/old.md") &&
    pendingApproval.includes("删除工作区内的条目") &&
    pendingApproval.includes("允许一次") &&
    pendingApproval.includes("拒绝") &&
    pendingApproval.includes("1 项待决定"),
  pendingApproval.slice(0, 160),
);
check(
  "审批卡片：把「需要审批」说成等人决策，而不是失败",
  pendingApproval.includes("待你决定") && !/错误|失败/i.test(pendingApproval),
  pendingApproval.slice(0, 160),
);
check(
  "审批卡片：写明一次一授权、且需要 Agent 重试同一调用",
  pendingApproval.includes("一次授权只放行一次") &&
    APPROVAL_STATUS_TEXT.approved.includes("重试同一调用"),
  APPROVAL_STATUS_TEXT.approved,
);

const decidedApproval = renderToStaticMarkup(
  <ApprovalCard
    approvals={[
      approval("approved"),
      approval("consumed", { id: "ap-consumed" }),
      approval("denied", { id: "ap-denied" }),
      approval("expired", { id: "ap-expired" }),
    ]}
  />,
);
check(
  "审批卡片：已决策的四种状态各有明确文案",
  decidedApproval.includes("已允许——Agent 重试同一调用时才会执行") &&
    decidedApproval.includes("已放行过一次") &&
    decidedApproval.includes("已拒绝") &&
    decidedApproval.includes("已过期，未放行"),
  decidedApproval.slice(0, 200),
);
check(
  "审批卡片：已决策项不再提供动作按钮",
  !decidedApproval.includes("允许一次") && !decidedApproval.includes(">拒绝<"),
  decidedApproval.slice(0, 200),
);
check(
  "审批卡片：没有审批时不渲染任何东西",
  renderToStaticMarkup(<ApprovalCard approvals={[]} />) === "",
  "",
);

check(
  "审批卡片：不消费 allowAutoExecution（不给假开关）",
  // 断言**渲染结果**：源码注释里说明「为什么不消费它」是允许的
  // （本轮已经有三次被自己的注释绊倒：full_access / 提权 / allowAutoExecution）。
  !/allowAutoExecution/.test(pendingApproval) && !/allowAutoExecution/.test(decidedApproval),
  pendingApproval.slice(0, 120),
);

const conversationSource = readFileSync(join(process.cwd(), "src", "App.tsx"), "utf8");
check(
  "审批卡片挂在对话流：与执行活动相邻、并按会话轮询",
  /<RunActivity[\s\S]{0,400}<ApprovalCard/.test(conversationSource) &&
    conversationSource.includes("listApprovals(approvalsFor)"),
  "",
);
check(
  "导航「工作台」入口带待确认角标（状态在 App 层，卡片与角标共用同一份）",
  /badge=\{pendingApprovals\}/.test(conversationSource) &&
    /className="nav-badge"/.test(conversationSource) &&
    /pendingApprovals = approvals\.filter/.test(conversationSource),
  "",
);

/* -------------------------------------------------------------------------- */
/* 工作区抽屉（§5.19 / §7.1）：入口在工作台，不再在配置页                        */
/* -------------------------------------------------------------------------- */

const configPageSource = readFileSync(
  join(process.cwd(), "src", "config", "ConfigPage.tsx"),
  "utf8",
);
const workspacePanelSource = readFileSync(
  join(process.cwd(), "src", "workspace", "WorkspacePanel.tsx"),
  "utf8",
);
check(
  "工作区入口在工作台：顶栏开关 + 抽屉，且面板组件已搬进 workspace/",
  /setWorkspaceOpen/.test(conversationSource) &&
    /className="workspace-drawer"/.test(conversationSource) &&
    /<WorkspacePanel[\s\S]{0,200}sessionId=/.test(conversationSource) &&
    // 归属不重复：配置页不再挂这块面板，也没有这个分区
    // （只看**渲染**：注释里说明「这块搬到哪去了」是允许的）
    !/<WorkspacePanel\s*\/>/.test(configPageSource) &&
    !/id:\s*"workspaces"/.test(configPageSource),
  "",
);
check(
  "工作区按会话绑定：登记时带 session_id，草稿态只暂存不登记",
  /createWorkspace\(\{[\s\S]{0,120}session_id: sessionId/.test(workspacePanelSource) &&
    // 草稿态提前分支：先 `onDraftChange` 再 return，绝不能走到 POST
    /if \(!sessionId\) \{[\s\S]{0,200}onDraftChange\(\{ path, mode \}\)[\s\S]{0,200}return;/.test(
      workspacePanelSource,
    ) &&
    // 草稿态不拉列表：返回的会是所有登记，摆出来像「已经选好了」
    /if \(!sessionId\) \{[\s\S]{0,200}setWorkspaces\(\[\]\)/.test(workspacePanelSource),
  "",
);
check(
  "工作区：一个会话只绑定一个——文案说清「再登记就是改绑」，且改绑后回执点名旧绑定已解除",
  workspacePanelSource.includes("一个会话只绑定一个工作区") &&
    workspacePanelSource.includes("再登记就是改绑") &&
    // 静默换掉旧绑定 = 让人以为两个目录都还挂着，而执行侧只可能用一个
    /已解除/.test(workspacePanelSource) &&
    // 按 id 判"是不是换了一条"，不按路径字符串（同一个目录有多种写法）
    /previous\.id !== created\.id/.test(workspacePanelSource),
  "",
);
check(
  "草稿态 → 会话建好时补登记：绑定失败不吞掉消息，但要说出来",
  /created && workspaceDraft[\s\S]{0,600}api\.createWorkspace\(\{[\s\S]{0,120}session_id: sessionId/.test(
    conversationSource,
  ) &&
    /warnings\.push\([\s\S]{0,200}工作区没能绑定/.test(conversationSource) &&
    /消息已发送，但\$\{warnings\.join\("；"\)\}/.test(conversationSource),
  "",
);

const workspaceTree: WorkspaceTree = {
  workspace_id: "w-1",
  path: "",
  depth: 2,
  truncated: false,
  limit: 500,
  entries: [
    {
      name: "reports",
      path: "reports",
      kind: "dir",
      outside: false,
      size_bytes: null,
      modified_at: null,
      children: [
        {
          name: "summary.md",
          path: "reports/summary.md",
          kind: "file",
          outside: false,
          size_bytes: 2048,
          modified_at: null,
        },
      ],
    },
    {
      name: "escape",
      path: "escape",
      kind: "symlink",
      outside: true,
      size_bytes: null,
      modified_at: null,
    },
  ],
};
const workspaceRow: Workspace = {
  id: "w-1",
  session_id: "s-1",
  path: "project/reports",
  mode: "read_only",
  name: null,
  quota: { max_file_bytes: 5242880, max_total_bytes: 268435456, max_entries: 2000 },
  usage: { available: true, total_bytes: 2048, entries: 2, truncated: false },
  created_by: null,
  updated_by: null,
  created_at: "2026-09-23T02:00:00Z",
};
const workspaceMarkup = renderToStaticMarkup(
  <WorkspaceBoundary
    workspaces={[workspaceRow]}
    selectedId="w-1"
    tree={workspaceTree}
    sessionId="s-1"
  />,
);
check(
  "工作区：列表、档位、用量与配额都渲染出来",
  workspaceMarkup.includes("project/reports") &&
    workspaceMarkup.includes("只读") &&
    workspaceMarkup.includes("2.0 KB") &&
    workspaceMarkup.includes("256.0 MB"),
  workspaceMarkup.slice(0, 200),
);
check(
  "工作区：越界符号链接被标出来且不展开",
  workspaceMarkup.includes("工作区外") &&
    // 越界链接没有子项，所以 only 工作区内的目录才带下一层
    workspaceMarkup.includes("summary.md"),
  workspaceMarkup.slice(0, 200),
);
check(
  "工作区：提档是显式动作，且没有 full_access 这一档",
  workspaceMarkup.includes("提档为可写") && !workspaceMarkup.includes("full_access"),
  workspaceMarkup.slice(0, 200),
);
check(
  "工作区：明确说明覆盖与删除需要人工审批",
  workspaceMarkup.includes("人工审批"),
  workspaceMarkup.slice(0, 200),
);

const draftMarkup = renderToStaticMarkup(
  <WorkspaceBoundary
    workspaces={[]}
    selectedId={null}
    tree={null}
    sessionId={null}
    draft={{ path: "sessions/draft", mode: "workspace_write" }}
  />,
);
check(
  "工作区：草稿态说清「发出第一条消息才登记」，并回显暂存的选择",
  draftMarkup.includes("草稿态") &&
    draftMarkup.includes("发出第一条消息") &&
    draftMarkup.includes("sessions/draft") &&
    draftMarkup.includes("可写") &&
    draftMarkup.includes("清除"),
  draftMarkup.slice(0, 200),
);
check(
  "工作区：草稿态不渲染「已登记」的列表（避免看起来像选好了）",
  !draftMarkup.includes("cfg-ws-list") &&
    draftMarkup.includes("还没有选工作区") &&
    !draftMarkup.includes("还没有登记工作区"),
  draftMarkup.slice(0, 200),
);

check(
  "工作区面板：选择文件夹走「导入副本」，不用只在浏览器里有效的目录句柄",
  // `webkitdirectory` 是**允许**的：浏览器给相对路径 + 内容，正好用来导入副本。
  // `showDirectoryPicker`（Web File System Access API）则不行——句柄只存在于浏览器，
  // 服务端 Agent 用不上，做了就是个只能看不能用的假入口。
  /webkitdirectory/.test(workspacePanelSource) &&
    !/showDirectoryPicker/.test(workspacePanelSource) &&
    // 只看**渲染结果**：源码注释里解释「为什么不提供这一档 / 这个入口」是允许的。
    !/full_access|提权/.test(workspaceMarkup),
  workspaceMarkup.slice(0, 120),
);
check(
  "工作区面板：写清「导入的是副本」，并给出宿主侧直连的出口",
  workspaceMarkup.includes("复制") &&
    workspaceMarkup.includes("pick_work_dir.ps1") &&
    workspaceMarkup.includes("选择文件夹并导入"),
  workspaceMarkup.slice(0, 160),
);
check(
  "审批入口指向对话流而不是工作区面板（归属不重复）",
  workspacePanelSource.includes("审批入口在对话流") &&
    !/decideApproval/.test(workspacePanelSource),
  "",
);

const folder = folderTargets([
  { name: "a.md", webkitRelativePath: "myproject/docs/a.md", size: 10 } as File,
  { name: "b.bin", webkitRelativePath: "myproject/b.bin", size: 30 * 1024 * 1024 } as File,
  { name: "root.txt", webkitRelativePath: "myproject/root.txt", size: 5 } as File,
]);
check(
  "选择文件夹：剥掉顶层目录名，超过单文件上限的单独挑出来",
  folder.targets.map((item) => item.path).join(",") === "docs/a.md,root.txt" &&
    folder.oversized.join(",") === "b.bin",
  JSON.stringify(folder.targets.map((item) => item.path)) + " / " + folder.oversized.join(","),
);

/* -------------------------------------------------------------------------- */
/* 选择文件夹位置（根内目录浏览器，ADR-033 §1）                                  */
/* -------------------------------------------------------------------------- */

const clientSource = readFileSync(join(process.cwd(), "src", "api", "client.ts"), "utf8");
// 静态渲染只到「读取中」那一步（effects 不跑），但这足以钉住挂载即要数据。
const pickerMarkup = renderToStaticMarkup(
  <WorkspaceLocationPicker onPick={() => {}} onClose={() => {}} />,
);
check(
  "选位置：挂载即读根目录，且入口在登记表单里（不再是只能盲打路径）",
  pickerMarkup.includes('aria-label="选择工作区位置"') &&
    pickerMarkup.includes("读取中") &&
    /api\.workspaceRootTree\(/.test(workspacePanelSource) &&
    workspacePanelSource.includes("浏览根目录"),
  pickerMarkup.slice(0, 160),
);
check(
  "选位置：走的是**根**接口——不需要先有一条登记（用 {id}/tree 会变成循环依赖）",
  clientSource.includes("/api/v1/workspace-root/tree") &&
    // 面板里对「已经登记的工作区」仍用 {id}/tree，对「选位置」用根接口，两者不能混。
    /api\.workspaceTree\(/.test(workspacePanelSource) &&
    !/api\.workspaceRootTree\(\s*workspace/i.test(workspacePanelSource),
  "",
);
check(
  "选位置：先问宿主目录（默认形态），容器形态按错误码退回根内浏览",
  /api\.hostTree\(/.test(workspacePanelSource) &&
    // 退回依据是**错误码**，不是"把两个接口都试一遍"
    /WORKSPACE_HOST_BROWSE_DISABLED/.test(workspacePanelSource) &&
    /setMode\("root"\)/.test(workspacePanelSource) &&
    // 越界链接列出来但点不动（不可进）——两种浏览都要守这条
    /disabled=\{entry\.outside\}/.test(workspacePanelSource),
  "",
);

const hostTree: WorkspaceHostTree = {
  path: "C:\\Users\\zq",
  parent: "C:\\Users",
  home: "C:\\Users\\zq",
  roots: [
    { name: "C:", path: "C:\\" },
    { name: "D:", path: "D:\\" },
  ],
  depth: 1,
  entries: [
    {
      name: "项目",
      path: "C:\\Users\\zq\\项目",
      kind: "dir",
      outside: false,
      size_bytes: null,
      modified_at: null,
    },
    {
      name: "note.txt",
      path: "C:\\Users\\zq\\note.txt",
      kind: "file",
      outside: false,
      size_bytes: 12,
      modified_at: null,
    },
  ],
  truncated: false,
  limit: 500,
};
const hostMarkup = renderToStaticMarkup(
  <HostLocationBrowser
    tree={hostTree}
    loading={false}
    error=""
    onNavigate={() => {}}
    onRetry={() => {}}
    onPick={() => {}}
    onClose={() => {}}
  />,
);
check(
  "选位置（宿主形态）：显示绝对路径、盘符与家目录入口，且只列文件夹",
  hostMarkup.includes("C:\\Users\\zq") &&
    hostMarkup.includes("项目") &&
    hostMarkup.includes("C:") &&
    hostMarkup.includes("D:") &&
    hostMarkup.includes("家目录") &&
    hostMarkup.includes("上一级") &&
    hostMarkup.includes("选定此文件夹") &&
    // 只列目录：这一层的产物是路径，铺文件只会让人误点
    !hostMarkup.includes("note.txt"),
  hostMarkup.slice(0, 200),
);
check(
  "选位置（宿主形态）：写清读写边界就是选中的那个文件夹",
  hostMarkup.includes("Agent 可以读写") && hostMarkup.includes("文件夹之外一律拒绝"),
  hostMarkup.slice(0, 200),
);

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

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
 * node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx --bundle --platform=node \
 *   --format=cjs --jsx=automatic --loader:.css=empty --outfile="$TEMP/workspace-smoke.cjs" \
 *   && node "$TEMP/workspace-smoke.cjs"
 * ```
 *
 * 冒烟本身只跑代码，**不做类型检查**（esbuild 只转译），而 `tsconfig.json` 的
 * `include` 只有 `src`，覆盖不到本目录。改了本文件或 `preview.tsx` 后要单独过一遍：
 *
 * ```bash
 * npx tsc --noEmit --jsx react-jsx --module esnext --moduleResolution bundler \
 *   --target es2022 --lib es2022,dom,dom.iterable --strict --skipLibCheck \
 *   --esModuleInterop --isolatedModules rendercheck/preview.tsx
 * ```
 *
 * 退出码 0 = 全通过。只跑不依赖 effects 的渲染路径——`TaskUsage` / `Inspector` 这类
 * 靠 effect 拉数据的容器不在覆盖范围内，仍要人工在浏览器里过一遍。
 */

import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { CollaborationGraph } from "../src/workspace/CollaborationGraph";
import { CollabCanvas, type CollabConversation } from "../src/workspace/CollabCanvas";
import { layoutCollaboration } from "../src/workspace/GraphCanvas";
import {
  buildCollaboration,
  type CollabGraph,
  type StageMeta,
} from "../src/workspace/collaboration";
import {
  AgentStageModal,
  AgentTraceView,
  type AgentStageDetail,
} from "../src/workspace/AgentStageModal";
import { formatStamp } from "../src/config/shared";
import { TaskUsagePanel, groupUsage, usageFor } from "../src/workspace/TaskUsage";
import { MessageAttachmentList, PendingFileChips } from "../src/workspace/AttachmentList";
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
  Metric,
  StageTraceItem,
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
  // s1/s2 都已完成 → 任务端子进去的两条 + 汇入 s3 的两条都亮；s3 还没跑 → 到交付那条不亮
  parallelLayout.edges.filter((edge) => edge.active).length === 4,
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
  "底部一行报全跑完时的最后一步",
  canvas.includes("全部阶段已完成：报告生成 Agent"),
  canvas.slice(canvas.indexOf("cv-terminal"), canvas.indexOf("cv-terminal") + 120),
);
check("底部一行标注是串行还是并行", canvas.includes("串行流水线"));
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

  // 画布样式在 workspace/workspace.css，不在 styles.css —— 读错文件会让断言恒真。
  const canvasStyles = readFileSync("src/workspace/workspace.css", "utf8");

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
  check("协作链路按波次模型渲染", app.includes("collaborationWaves"));

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
  // 而协作形态只有在规划 Agent 真的参与时才成立，所以点卡必须同时切编排模式。
  check(
    "欢迎卡片各自写出协作形态",
    ["通常 1 个 Agent 直答", "调查 → 分析 → 总结", "多步核对与整理"].every((shape) =>
      app.includes(shape),
    ),
  );
  check(
    "点卡片同时切到自动编排",
    app.includes('onChoose(p.text, "dynamic")'),
    "只填文字不换模式，卡片上的协作形态在当前链路下就不成立",
  );
  check(
    "填提示词与设模式在同一个函数里",
    app.includes("promptMode: OrchestrationMode") && app.includes("setMode(promptMode)"),
  );
  check("输入区显式写出当前编排模式", app.includes("welcome-note") && styles.includes(".welcome .welcome-note"));
  check(
    "旧横排卡片规则已清理",
    !styles.includes(".suggestions button"),
    "残留的 .suggestions button 会把新卡的图标撑成整宽",
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

  // 构建期出过一次「CSS 规则被压缩器静默丢掉」的事故（只打 WARNING、退出码仍是 0），
  // 所以新界面引用的每个类名都要在样式表里有定义，缺一个就说明有规则没落盘。
  const requiredClasses = [
    ".suggestion-icon",
    ".suggestion-shape",
    ".composer-attach",
    ".composer-files",
    ".composer-file.failed",
    ".conversation-composer.dragging",
    ".composer-mode button.active",
    ".message-attachments",
    ".message-attachment-thumb",
    // 原件留档后条目主体是 <a>（ADR-024）：没有这条规则会退化成蓝字下划线。
    "a.message-attachment",
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
} catch (cause) {
  check("读取源文件做静态断言", false, cause instanceof Error ? cause.message : String(cause));
}

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

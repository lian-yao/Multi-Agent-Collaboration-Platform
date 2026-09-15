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
 * 退出码 0 = 全通过。只跑不依赖 effects 的渲染路径——`TaskUsage` / `Inspector` 这类
 * 靠 effect 拉数据的容器不在覆盖范围内，仍要人工在浏览器里过一遍。
 */

import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { CollaborationGraph, type CollaboratorNode } from "../src/workspace/CollaborationGraph";
import { AgentStageModal, type AgentStageDetail } from "../src/workspace/AgentStageModal";
import { TaskUsagePanel, groupUsage } from "../src/workspace/TaskUsage";
import type { Agent, Metric } from "../src/types/api";

const results: [boolean, string][] = [];
const check = (label: string, condition: boolean, detail = "") =>
  results.push([condition, condition ? label : `${label}  →  ${detail}`]);

/* -------------------------------------------------------------------------- */
/* 协作链路                                                                    */
/* -------------------------------------------------------------------------- */

const node = (id: string, status: string): CollaboratorNode => ({
  id,
  stageLabel: { collect: "信息收集", analyze: "数据分析", report: "报告生成" }[id] ?? id,
  agentName: { collect: "信息收集 Agent", analyze: "数据分析 Agent", report: "报告生成 Agent" }[id] ?? id,
  status,
});

let serial = "";
try {
  serial = renderToStaticMarkup(
    <CollaborationGraph
      waves={[[node("collect", "completed")], [node("analyze", "running")], [node("report", "pending")]]}
      activeId="analyze"
      onSelect={() => undefined}
    />,
  );
  check("串行链路可渲染", serial.length > 200, `长度 ${serial.length}`);
} catch (cause) {
  check("串行链路可渲染", false, cause instanceof Error ? cause.message : String(cause));
}

check("渲染出三个 Agent 节点", ["信息收集 Agent", "数据分析 Agent", "报告生成 Agent"].every((n) => serial.includes(n)));
check("波次之间标注串行", serial.includes(">串行<"), serial.slice(0, 300));
check("执行中的节点带 active 高亮", serial.includes("collab-node running active"));
check("等待中的节点标注前置依赖", serial.includes("等待前置阶段"));
check("串行链路口径为串行流水线", serial.includes("串行流水线"));
check("图例三态齐全", serial.includes("已完成") && serial.includes("执行中") && serial.includes("等待前置"));
check("节点可点击时渲染为 button", serial.includes("<button") && serial.includes('aria-label="查看信息收集 Agent的'));

let parallel = "";
try {
  parallel = renderToStaticMarkup(
    <CollaborationGraph
      waves={[[node("collect", "completed")], [node("analyze", "running"), node("report", "running")]]}
    />,
  );
  check("并行波次可渲染", parallel.includes("collab-row parallel"), parallel.slice(0, 300));
} catch (cause) {
  check("并行波次可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("并行波次口径为含并行波次", parallel.includes("含并行波次"));
check("未传 onSelect 时渲染为 div 而非 button", !parallel.includes("<button"));

check(
  "空波次给空态而不是空白",
  renderToStaticMarkup(<CollaborationGraph waves={[]} />).includes("协作关系"),
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
};

const detail: AgentStageDetail = {
  stageId: "collect",
  stageLabel: "信息收集",
  responsibility: "整理任务要求与输入资料，为后续分析准备信息。",
  status: "pending",
  agent,
  checkpointSaved: false,
  updatedAt: "2026-09-15T08:00:00Z",
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
check("渲染生效模型与 Provider", modal.includes("qwen2.5-coder:7b") && modal.includes("Ollama（本地）"));
check("渲染覆盖项", modal.includes("temperature"));
check("输出上限写成 tokens", modal.includes("2048 tokens"));
check(
  "pending 说「等待前置阶段」而不是 workflow 词汇「排队中」",
  modal.includes("等待前置阶段") && !modal.includes("排队中"),
  modal.slice(0, 400),
);
check("未保存检查点时给明确文案", modal.includes("暂无已完成记录"));
check("明示推理过程尚未对外暴露", modal.includes("尚未对外暴露"));
check("给出任务记录入口", modal.includes("在任务记录中查看工具调用与指标"));

let bare = "";
try {
  bare = renderToStaticMarkup(<AgentStageModal detail={{ ...detail, agent: null }} onClose={() => undefined} />);
  check("角色未就绪时弹窗仍可渲染", bare.includes("collect Agent"));
} catch (cause) {
  check("角色未就绪时弹窗仍可渲染", false, cause instanceof Error ? cause.message : String(cause));
}
check("没有任务记录入口时不渲染该按钮", !bare.includes("在任务记录中查看工具调用与指标"));

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

  const app = readFileSync("src/App.tsx", "utf8");
  check(
    "App 不再出现「主决策」「自动分配」",
    !app.includes("主决策") && !app.includes("自动分配"),
    "composer 里不应再有用户可指定主 Agent 的入口",
  );
  check(
    "执行台卡片点击打开阶段弹窗而非侧栏",
    app.includes("setDetailStage(s.id)") && !app.includes("setInspectorOpen(true)"),
  );
  const inspection = readFileSync("src/records/Inspection.tsx", "utf8");
  check(
    "工具调用与采样明细不再合体成工作台组件",
    !app.includes("WorkflowInspection") && !inspection.includes("WorkflowInspection"),
    "逐条明细只应留在任务记录页，合体组件应已删除",
  );
  check("协作链路按波次模型渲染", app.includes("collaborationWaves"));
} catch (cause) {
  check("读取源文件做静态断言", false, cause instanceof Error ? cause.message : String(cause));
}

const passed = results.filter(([ok]) => ok).length;
for (const [ok, label] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
console.log(`\n${passed}/${results.length} checks passed`);
console.log("RESULT: " + (passed === results.length ? "PASS" : "FAIL"));
process.exit(passed === results.length ? 0 : 1);

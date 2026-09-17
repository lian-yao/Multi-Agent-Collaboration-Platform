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
import { CollaborationGraph, type CollaboratorNode } from "../src/workspace/CollaborationGraph";
import { AgentStageModal, type AgentStageDetail } from "../src/workspace/AgentStageModal";
import { TaskUsagePanel, groupUsage } from "../src/workspace/TaskUsage";
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
import type { Agent, Attachment, Metric } from "../src/types/api";

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

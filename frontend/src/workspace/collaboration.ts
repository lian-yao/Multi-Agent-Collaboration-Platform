/**
 * 协作工作流模型：把「Agent 目录 + Workflow + 阶段轨迹（§5.17）+ 用量采样」
 * 组装成画布要的**节点**与**连线**。
 *
 * 为什么单独一个模块：侧栏紧凑态与全屏画布要画的是同一个东西，只是密度不同。
 * 如果各自从 props 现场推一遍，两处的「谁在第几步、谁跟谁并行、这条线上的工具调用
 * 算谁的」迟早会分叉。这里只做一次，视图本体只吃结果——也因此能被离屏冒烟直接挂载。
 *
 * 三件事刻意放在这里而不是散在组件里：
 *
 * - **状态词汇**（completed / running / pending / skipped）。`skipped` 与 `pending`
 *   在界面上长得像、语义相反（一个是「已经放弃」，一个是「还在等」），只留一处判断。
 * - **波次**：由 `depends_on` 算层级，而不是「每步一波」。当前档位是串行，
 *   但算层级之后，将来真出现同层步骤时并行关系会自动画对。
 * - **取值的格式化**：工具入参/出参、参数值、工具摘要。
 */
import type {
  Agent,
  Metric,
  PlanStepSummary,
  StageToolCall,
  StageTraceItem,
  Workflow,
  WorkflowStageTrace,
} from "../types/api";
import { usageFor, type UsageRow } from "./TaskUsage";

/* -------------------------------------------------------------------------- */
/* 阶段元信息                                                                  */
/* -------------------------------------------------------------------------- */

/** 画布需要的阶段元信息。图标/配色属于视图，不进模型。 */
export type StageMeta = {
  id: string;
  label: string;
  /** Agent 角色 id（collector / analyst / reporter）。 */
  agent: string;
  /** 该阶段的一句话职责。 */
  responsibility: string;
};

/** 静态阶段按 id 反查。 */
function metaForStage(stages: StageMeta[], id: string): StageMeta {
  return (
    stages.find((stage) => stage.id === id) ?? {
      id,
      label: id,
      agent: id,
      responsibility: "由规划 Agent 指派。",
    }
  );
}

/** 动态链路的计划步骤只给角色，按角色反查静态阶段元信息。 */
function metaForRole(stages: StageMeta[], role: string): StageMeta {
  return (
    stages.find((stage) => stage.agent === role) ?? {
      id: role,
      label: role,
      agent: role,
      responsibility: "由规划 Agent 指派。",
    }
  );
}

/**
 * 按「阶段 id 优先、角色兜底」解析一条步骤的元信息。
 *
 * 两条链路拿到的标识不是同一个东西：静态链路给的是阶段名（`collect`），动态链路给的是
 * 计划步骤 id（`s1`），而它的角色在 `plan[].role` 上。调用方（对话流内联轨迹、执行台）
 * 若各自判断「这个 id 认不认识」，就会出现「动态步骤显示成 s1」和「显示成收集 Agent」
 * 两种口径并存的观感问题。所以判断只放在这里。
 */
export function stageMetaFor(stages: StageMeta[], id: string, role?: string): StageMeta {
  return stages.some((stage) => stage.id === id)
    ? metaForStage(stages, id)
    : metaForRole(stages, role || id);
}

/* -------------------------------------------------------------------------- */
/* 状态词汇（执行台与画布共用）                                                */
/* -------------------------------------------------------------------------- */

/** 动态链路才产出计划；静态链路下 `checkpoint.plan` 不存在。 */
export function planSteps(workflow: Workflow | null): PlanStepSummary[] {
  const plan = workflow?.checkpoint?.plan;
  return Array.isArray(plan) ? plan : [];
}

/**
 * 「计划从哪来」——项目对外只说这一个概念。
 *
 * 这里**没有**「两套并列的编排模式」：固定链是动态路径的退化情形，计划不由规划节点产出，
 * 而是一条常量链（收集 → 分析 → 报告）。把 `static` 说成「另一种编排方式」，会让读者以为
 * 存在两条对等的链路；实际存在的只有「计划由规划节点产出」与「计划是那条常量链」。
 * 见 ADR-030 §2 与本轮 ADR-037。
 */
export type PlanSourceKey = "planning" | "planned" | "fallback" | "fixed";

export const PLAN_SOURCE_TEXT: Record<PlanSourceKey, string> = {
  planning: "正在规划",
  planned: "规划 Agent 产出",
  fallback: "回退固定链",
  fixed: "固定链",
};

export const PLAN_SOURCE_HINT: Record<PlanSourceKey, string> = {
  planning: "计划还没落盘：这次由规划 Agent 判断需要哪些角色。",
  planned: "计划由规划 Agent 按任务产出，再按依赖逐步执行。",
  fallback: "规划节点没产出可用计划，已回退固定链（收集 → 分析 → 报告）。",
  fixed: "固定链：计划恒为 收集 → 分析 → 报告，不经过规划节点。",
};

/**
 * 由链路事实推「计划来源」。
 *
 * 判据顺序即优先级：还在规划窗口 → 没有计划可谈；计划来自规划节点（`llm`，或计划已落盘
 * 但来源字段缺失的老数据）→ 规划 Agent 产出；来源是 `fallback` → 规划失败回退；既不是
 * 规划窗口也不是动态链路 → 那条固定链。
 */
export function planSourceKey(input: {
  mode?: string | null;
  planning?: boolean;
  source?: string | null;
}): PlanSourceKey {
  if (input.planning) return "planning";
  if (input.mode === "dynamic") {
    return input.source === "fallback" ? "fallback" : "planned";
  }
  return "fixed";
}

/**
 * 「还在规划」——计划没落盘之前，**不要**把写死的固定三步当成这次的事实画出来。
 *
 * 动态链路的计划由规划节点产出后才随 `mode` 一起写进 `checkpoint`；在那之前
 * `workflow_runs.checkpoint` 是 **null**（`create_workflow_run` 只建行、不写摘要），
 * 于是「没有 `plan`」这件事同时对应两种完全不同的实情：
 *
 * - **静态链路**：它没有规划环节，写死的固定三步就是全部——照画不误；
 * - **动态链路**：链路由几步、派给谁都还不知道，此时能画出来的只有固定三步，
 *   而这次它可能压根不参与（一个 Agent 一步做完）。
 *
 * 把后者当成前者，界面上就是「先画一版错的、等计划落盘再换成对的」：读数的人会把
 * 第一版当成真链路。所以判据要凑齐三条——**这次走的是动态编排**（服务端在规划窗口内
 * 没有任何字段能说明这件事，只有提交方自己知道，见 `requestedMode`）、**服务端还没写过
 * 任何链路事实**、**运行还没进终态**。
 */
export function isPlanning(
  workflow: Workflow | null,
  requestedMode?: string | null,
): boolean {
  if (!workflow) return false;
  // 判据一：这次走的是动态编排——服务端在规划窗口内没有任何字段能说明这件事，
  // 只有提交方自己知道（见 `requestedMode`）。静态链路的固定三步是常量，照画不误。
  if (requestedMode !== "dynamic") return false;
  // 判据二：服务端**还没写过任何链路事实**。`checkpoint` 一旦有值就说明链路已定型——
  // 动态链路的计划与 `mode` 是同一次写入的，有 `mode` 必有 `plan`。
  if (workflow.checkpoint) return false;
  // 判据三：运行还没进终态。终态却没有计划属于数据残缺，不该读成「还在规划」。
  return ["pending", "running"].includes(workflow.status);
}

/**
 * 计划步骤的显示状态。
 *
 * `skipped` 必须原样透出：它和 `pending` 在界面上长得像，但语义完全相反——
 * 一个是「还在等」，一个是「因为上游失败已经放弃」。
 */
export function planStepStatus(step: PlanStepSummary): string {
  if (step.status === "completed") return "completed";
  if (step.status === "failed") return "failed";
  if (step.status === "skipped") return "skipped";
  return "pending";
}

/**
 * 静态阶段的状态。
 *
 * 顺序不能反：`completed_steps` 是**已落盘的事实**，而 `current_step` 只是「现在轮到谁」。
 * 先看 `completed` 才不会把刚跑完、指针还停在原地的那一步显示成「执行中」。
 */
export function stageStatus(
  stage: string,
  workflow: Workflow | null,
  completed: Set<string>,
): string {
  if (completed.has(stage)) return "completed";
  const current = workflow?.checkpoint?.current_step ?? workflow?.current_step;
  if (
    current === stage &&
    workflow &&
    ["running", "paused", "failed"].includes(workflow.status)
  )
    return workflow.status;
  return "pending";
}

/* -------------------------------------------------------------------------- */
/* 取值格式化                                                                  */
/* -------------------------------------------------------------------------- */

/** 工具入参 / 出参的可读文案：字符串直出，其余走 JSON。 */
export function payloadText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "（无）";
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** 服务端的截断标记：截断要**说出来**，不能把预览当成全部。 */
export function isTruncated(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { truncated?: unknown }).truncated === true
  );
}

/**
 * `calculator ×2`：同名工具合并计数，连线上一眼看出这条链路动过哪些工具。
 *
 * 计数**一律写出来**，包括 `×1`。只在多于一次时写计数，会得到
 * `web_search ×3、sql_query` 这种半带数字的半截话——读者无从判断 `sql_query`
 * 到底调了一次还是根本没法计数。规则要么都写、要么都不写，这里选都写。
 *
 * 入参兼容两种形状——§5.17 的原始采样（`tool_name`）与画布用的 `CollabToolCall`（`name`）。
 * 让调用方自己去映射字段，就会出现「连线上的工具名被写成 undefined」这种只在运行时
 * 才看得见的错位。
 */
export function toolChainText(
  calls: readonly (StageToolCall | CollabToolCall)[],
): string {
  if (!calls.length) return "";
  const nameOf = (call: StageToolCall | CollabToolCall): string =>
    ("tool_name" in call ? call.tool_name : call.name) || "未命名工具";
  const counts = new Map<string, number>();
  for (const call of calls) {
    const name = nameOf(call);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return [...counts].map(([name, count]) => `${name} ×${count}`).join("、");
}

/* -------------------------------------------------------------------------- */
/* 模型                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * 这个值是从哪来的。
 *
 * 画布的悬停面板一度把**角色当前配置**当成「生效参数」显示，于是改过模型/温度之后，
 * 打开旧对话看到的是**今天**的配置——而它声称的是「这次执行用的参数」。
 * 两者必须分开说：`run` = 本次执行记录下来的，`config` = 角色当前配置。
 */
export type CollabParamSource = "run" | "config";

export type CollabParam = {
  /** 字段名（`temperature` / `top_p` / …），与 `override_keys` 同口径。 */
  key: string;
  label: string;
  value: string;
  /** 该字段来自角色的**显式覆盖**（而不是默认路由 / 环境配置）。 */
  overridden: boolean;
  /** 值的来源；见 `CollabParamSource`。 */
  source: CollabParamSource;
  /**
   * 同名字段的**当前**配置值，只在「本次用了 X、当前配的是 Y」时给出。
   *
   * 单独一个 `value` 会让人以为两者一致；并排放出来，「改过配置」这件事就自己说明白了。
   */
  configured?: string;
};

export type CollabToolCall = {
  id: string;
  name: string;
  status: string;
  input: string;
  output: string;
  error: string;
};

export type CollabNode = {
  /** 节点 id：静态阶段是 `collect/analyze/report`，动态链路是计划步骤 id。 */
  id: string;
  /** Agent 角色 id，用量采样按它归集。 */
  agentId: string;
  /** Agent 显示名。 */
  name: string;
  stageLabel: string;
  responsibility: string;
  status: string;
  /** 第几步（1 起），画布上标「第 N 步」。 */
  order: number;
  /** 波次序号（0 起）；同波 = 并行。 */
  wave: number;
  /** 同波不止一个节点。 */
  parallel: boolean;
  /** 角色**当前**绑定的模型。空串 = 角色已不在目录里（旧画布会遇到）。 */
  model: string;
  /** 本次执行**实际**用过的模型（采样里的 `model` 标签）。空串 = 本次没有采样。 */
  modelAtRun: string;
  /** 角色是否还在 Agent 目录里；不在时「当前配置」读不到，界面必须说出来。 */
  agentKnown: boolean;
  provider: string;
  params: CollabParam[];
  /** 该 Agent 的用量采样原值（**不求和**）。空数组 = 没有采样，不是 0。 */
  usage: UsageRow[];
  /** 分配到的任务：本阶段收到的上游输入；根阶段回落成整条任务原文。 */
  prompt: string | null;
  /** 上游节点 id，说明输入来自哪一步；根节点为 null。 */
  promptFrom: string | null;
  output: string;
  truncated: boolean;
  toolCalls: CollabToolCall[];
  /** 该阶段的轨迹缺失原因（§5.17）；有轨迹时为 null。 */
  traceReason: string | null;
};

/** 计划里某一步被派给了谁、为什么（「任务分配」节点的悬停明细）。 */
export type CollabAssignment = {
  id: string;
  /** 角色 id（`collector` / …）。 */
  role: string;
  /** 角色的显示名。 */
  label: string;
  /** 该步骤的一句话职责（规划节点产出）。 */
  instruction: string;
};

/**
 * 规划决策（planner 环节）。
 *
 * **只在动态链路出现**：静态链路的步骤是写死的三步常量，没有「谁被派了活」这个决策，
 * 因此 `null` 而**不是**一份空壳——画布据此决定要不要画那个节点（§5.22「计划即落盘」）。
 */
export type CollabPlanner = {
  /** 规划理由；降级（`fallback`）时这里是回退原因。 */
  rationale: string;
  /** `llm` = 规划节点真的产出了计划；`fallback` = 降级到固定三步。 */
  source: string;
  /** 各步骤的分配结果，按计划声明顺序。 */
  assignments: CollabAssignment[];
};

export type CollabEdge = {
  from: string;
  to: string;
  /** 同波内为 `parallel`，跨波为 `serial`。 */
  kind: "serial" | "parallel";
  /** 上游这一步动过的工具，如 `calculator ×1`；空串表示没调工具。 */
  toolSummary: string;
  toolCalls: CollabToolCall[];
};

export type CollabGraph = {
  nodes: CollabNode[];
  edges: CollabEdge[];
  /** 波次视图：波内并行、波间串行。 */
  waves: CollabNode[][];
  /** 本次工作流的任务原文（§5.17 的 `task`）。 */
  task: string | null;
  mode: string;
  /** 规划决策（planner 环节）；静态链路为 `null`，画布据此决定要不要画「任务分配」节点。 */
  planner: CollabPlanner | null;
  /**
   * 还在等计划落盘（`isPlanning`）。
   *
   * `true` 时 `nodes` 必为空——**不是**「这次没有节点可画」，而是「现在还不知道要画什么」。
   * 两者在界面上要分开说：一个是终态，一个是过程。
   */
  planning: boolean;
  /** 整条链路都没有分阶段轨迹时的原因（目前只有动态编排走到这里）。 */
  traceReason: string | null;
};

/**
 * 由依赖关系算层级：没有依赖的是第 0 波，其余是「所有依赖里最深的 +1」。
 *
 * 遇到成环的计划不让它把递归拖死——真实计划不会成环，但画布不能因为一份坏数据就白屏。
 */
function levelOf(
  ids: string[],
  depsOf: Map<string, string[]>,
): Map<string, number> {
  const level = new Map<string, number>();
  const resolve = (id: string, seen: Set<string>): number => {
    const known = level.get(id);
    if (known !== undefined) return known;
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = (depsOf.get(id) ?? []).filter((dep) => depsOf.has(dep));
    const value = deps.length
      ? Math.max(...deps.map((dep) => resolve(dep, seen))) + 1
      : 0;
    level.set(id, value);
    return value;
  };
  for (const id of ids) resolve(id, new Set());
  return level;
}

/** 采样里带的 `stage` 标签：静态链路就是阶段名，动态链路是 `dyn:{步骤 id}`。 */
export function metricStageOf(id: string, dynamic: boolean): string {
  return dynamic ? `dyn:${id}` : id;
}

/** 只有 Token 类采样才带 `model` 标签（见 `app/observability/metrics.py`）。 */
const MODEL_BEARING_METRICS = new Set(["input_tokens", "output_tokens", "total_tokens"]);

/**
 * 本次执行**实际**用过的模型：取该阶段那几条 Token 采样上的 `model` 标签。
 *
 * 为什么不看角色目录：目录读的是**当前**配置。用户改过绑定之后，旧对话的「当时用的什么」
 * 就再也答不出来了——而采样是执行时写下的，它才是当时的事实。
 *
 * 只认 `stage` 完全匹配的行：同一工作流里各阶段可能用不同模型，按 `role` 兜底会把
 * 另一阶段的模型安到这个节点上，那是另一种错。
 */
export function runModelFor(metrics: Metric[], stage: string): string {
  for (const metric of metrics) {
    if (!MODEL_BEARING_METRICS.has(metric.metric_name)) continue;
    const labels = metric.labels ?? {};
    if (labels.stage !== stage) continue;
    const model = labels.model;
    if (typeof model === "string" && model) return model;
  }
  return "";
}

/** 圆下方那行里的模型名：优先本次执行的记录，没有才回落到角色当前配置。 */
export function modelLabelOf(node: CollabNode): string {
  return node.modelAtRun || node.model || "由运行时提供";
}

/**
 * 角色的生效参数。
 *
 * **模型**有本次执行的采样就用采样值——那才是「这次用的什么」。其余四项
 * （Temperature / Top P / 输出上限 / 推理模式）平台**没有在执行时记录**，只能读角色当前
 * 配置，因此逐条标成 `config`，由界面明确告诉读者「这是现在的配置，不是当时的」。
 * 把两者混在一个「生效参数」标题下，就是在替一次已经跑完的执行编造参数。
 */
function paramsOf(agent: Agent | undefined, modelAtRun: string): CollabParam[] {
  const configuredModel = agent?.model ?? "";
  const overridden = new Set(agent?.override_keys ?? []);
  const params: CollabParam[] = [];

  if (modelAtRun || configuredModel) {
    params.push({
      key: "model",
      label: "模型",
      value: modelAtRun || configuredModel,
      // 采样值不是「覆盖」，所以不给它挂覆盖标记——那个标记说的是角色当前配置的来源。
      overridden: modelAtRun ? false : overridden.has("model"),
      source: modelAtRun ? "run" : "config",
      ...(modelAtRun && modelAtRun !== configuredModel ? { configured: configuredModel } : {}),
    });
  }

  if (!agent) return params;

  const rows: { key: string; label: string; value: string }[] = [
    { key: "temperature", label: "Temperature", value: String(agent.temperature) },
    {
      key: "top_p",
      label: "Top P",
      value: agent.top_p === null || agent.top_p === undefined ? "未设置" : String(agent.top_p),
    },
    {
      key: "max_output_tokens",
      label: "输出上限",
      value:
        agent.max_output_tokens === null || agent.max_output_tokens === undefined
          ? "未设置"
          : String(agent.max_output_tokens),
    },
    { key: "reasoning_type", label: "推理模式", value: agent.reasoning_type || "none" },
  ];
  for (const row of rows) {
    params.push({
      key: row.key,
      label: row.label,
      value: row.value,
      overridden: overridden.has(row.key),
      source: "config",
    });
  }
  return params;
}

function toolCallsOf(calls: StageToolCall[]): CollabToolCall[] {
  return calls.map((call, index) => ({
    id: call.call_id || `${call.tool_name}-${index}`,
    name: call.tool_name || "未命名工具",
    status: call.status,
    input: payloadText(call.input),
    output: payloadText(call.output),
    error: call.error ?? "",
  }));
}

/**
 * 组装协作画布的节点与连线。
 *
 * 轨迹（§5.17）与用量（§5.5）都是**可选**的：没有轨迹时节点仍要画出来（带 `pending`
 * 状态），不能因为「还没数据」就整块空白。
 *
 * 唯一的例外是**计划尚未落盘**（`isPlanning`）：那时连「几步、派给谁」都还不知道，
 * 返回的是 `planning: true` 的空图，而不是拿固定三步顶上（ADR-034）。
 */
export function buildCollaboration({
  stages,
  agents,
  workflow,
  completed,
  traces = null,
  metrics = [],
  requestedMode = null,
}: {
  stages: StageMeta[];
  agents: Agent[];
  workflow: Workflow | null;
  completed: Set<string>;
  traces?: WorkflowStageTrace | null;
  metrics?: Metric[];
  /**
   * 提交这次任务时声明的编排模式（`doc/api.md` §4.4 的 `orchestration_mode`）。
   *
   * 规划窗口内服务端还没有任何事实可读（`checkpoint` 是 null），**唯一**知道「这次走的是
   * 动态编排」的就是提交方自己。只有实时工作流该传：历史工作流的模式要按它自己的数据
   * 判断，借用当前那个开关会把静态老任务读成「正在规划」。
   */
  requestedMode?: string | null;
}): CollabGraph {
  const traceReason =
    traces && !traces.items.length ? traces.reason ?? null : null;
  const empty: CollabGraph = {
    nodes: [],
    edges: [],
    waves: [],
    task: traces?.task ?? null,
    mode: traces?.mode ?? "static",
    planner: null,
    planning: false,
    traceReason,
  };
  if (!workflow) return empty;

  // 规划窗口：这张图上**没有任何**可用的事实。此时不画比画错更有用——
  // 见 `isPlanning` 的注释（用户 2026-09-22 反馈：单 Agent 的问题也先画了三段固定链路）。
  if (isPlanning(workflow, requestedMode)) return { ...empty, planning: true };

  // 1) 参与本次执行的节点，静态阶段与计划步骤收敛成同一种形状
  const plan = planSteps(workflow);
  const entries: { id: string; meta: StageMeta; status: string; deps: string[] }[] =
    plan.length
      ? plan.map((step) => ({
          id: step.id,
          meta: metaForRole(stages, step.role),
          status: planStepStatus(step),
          deps: step.depends_on ?? [],
        }))
      : stages
          .filter((stage) => agents.some((agent) => agent.id === stage.agent))
          .map((stage, index, list) => ({
            id: stage.id,
            meta: stage,
            status: stageStatus(stage.id, workflow, completed),
            // 静态链路是固定串行流水线：每一步都依赖上一步
            deps: index > 0 ? [list[index - 1].id] : [],
          }));

  // 规划决策（planner 环节）：动态链路才有「谁被派了活、依据是什么」这件事——计划与理由
  // 由 §5.22「计划即落盘」写进 checkpoint。静态链路的步骤是写死的三步常量，没有这个决策，
  // 因此给 `null` 而不是一份空壳：画布据此决定要不要画「任务分配」节点。
  const planner: CollabPlanner | null = plan.length
    ? {
        rationale: workflow.checkpoint?.plan_rationale ?? "",
        source: workflow.checkpoint?.plan_source ?? "",
        assignments: plan.map((step) => ({
          id: step.id,
          role: step.role,
          label: metaForRole(stages, step.role).label,
          instruction: step.instruction ?? "",
        })),
      }
    : null;

  if (!entries.length) return empty;

  // 2) 波次：由依赖算层级，同波即并行
  const ids = entries.map((entry) => entry.id);
  const depsOf = new Map(entries.map((entry) => [entry.id, entry.deps]));
  const level = levelOf(ids, depsOf);
  const waveCounts = new Map<number, number>();
  for (const id of ids) {
    const value = level.get(id) ?? 0;
    waveCounts.set(value, (waveCounts.get(value) ?? 0) + 1);
  }

  // 3) 轨迹与用量按 Agent / 阶段归位
  const traceByStage = new Map<string, StageTraceItem>(
    (traces?.items ?? []).map((item) => [item.stage, item]),
  );

  const nodes: CollabNode[] = entries.map((entry) => {
    const agent = agents.find((item) => item.id === entry.meta.agent);
    const trace = traceByStage.get(entry.id);
    const wave = level.get(entry.id) ?? 0;
    // 「这次用的什么模型」只信**执行时写下的采样**：角色目录是当前配置，改过绑定之后
    // 拿它回答旧对话就是在编造（见 `runModelFor`）。
    const modelAtRun = runModelFor(metrics, metricStageOf(entry.id, plan.length > 0));
    return {
      id: entry.id,
      agentId: entry.meta.agent,
      name: agent?.name ?? `${entry.meta.agent} Agent`,
      stageLabel: entry.meta.label,
      responsibility: entry.meta.responsibility,
      status: entry.status,
      order: ids.indexOf(entry.id) + 1,
      wave,
      parallel: (waveCounts.get(wave) ?? 1) > 1,
      model: agent?.model ?? "",
      modelAtRun,
      agentKnown: Boolean(agent),
      provider: agent?.provider_name ?? agent?.provider ?? "",
      params: paramsOf(agent, modelAtRun),
      usage: usageFor(metrics, entry.meta.agent),
      prompt: trace?.input ?? (entry.deps.length ? null : traces?.task ?? null),
      promptFrom: trace?.input_from ?? null,
      output: trace?.output ?? "",
      truncated: trace?.truncated ?? false,
      toolCalls: toolCallsOf(trace?.tool_calls ?? []),
      traceReason: trace && !trace.output && !trace.tool_calls.length
        ? trace.reason ?? "暂无该阶段的执行记录。"
        : null,
    };
  });

  const nodeById = new Map(nodes.map((node) => [node.id, node]));

  // 4) 连线：每条依赖一条，工具链路挂在**上游**——「我是用什么工具产出了交给你的东西」
  //
  //    口径按「上游那一波有几个节点」区分：上游单节点是**串行**（上一步交给下一步），
  //    上游多节点则是**并行汇入**（几条并行分支一起汇到这里）。按「两端是否同波」判
  //    会永远判成串行——依赖天然指向更低的波次，那个分支其实不可达。
  const edges: CollabEdge[] = [];
  for (const entry of entries) {
    for (const dep of entry.deps) {
      const source = nodeById.get(dep);
      if (!source) continue;
      const sourceWave = level.get(dep) ?? 0;
      edges.push({
        from: dep,
        to: entry.id,
        kind: (waveCounts.get(sourceWave) ?? 1) > 1 ? "parallel" : "serial",
        toolSummary: toolChainText(source.toolCalls),
        toolCalls: source.toolCalls,
      });
    }
  }

  // 5) 波次视图：按层级升序分组，组内保持声明顺序
  const waves: CollabNode[][] = [];
  for (const node of nodes) {
    if (!waves[node.wave]) waves[node.wave] = [];
    waves[node.wave].push(node);
  }

  return {
    nodes,
    edges,
    waves,
    task: traces?.task ?? null,
    mode: traces?.mode ?? (plan.length ? "dynamic" : "static"),
    planner,
    planning: false,
    traceReason,
  };
}

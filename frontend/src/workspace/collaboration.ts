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
  FlowNodeSummary,
  Metric,
  PlanStepSummary,
  StageToolCall,
  StageTraceItem,
  ValidationSummary,
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

/** 平台节点（意图 / 编排 / 合成 / 校验）的展示元信息：它们没有 Agent 角色。 */
const PLATFORM_META: Record<string, { label: string; responsibility: string }> = {
  intent: { label: "意图识别", responsibility: "读懂用户这一轮要什么、要不要拆成多个子任务。" },
  plan: { label: "编排器", responsibility: "把目标拆成子任务，并决定依赖与并行波次。" },
  synthesize: { label: "合成器", responsibility: "合并多路子任务产出，冲突消解后按约束成稿。" },
  validate: { label: "结果校验", responsibility: "核对交付物是否满足原始意图与约束。" },
};

export function flowKindLabel(kind: string): string {
  return PLATFORM_META[kind]?.label ?? kind;
}

function metaForFlowNode(stages: StageMeta[], node: FlowNodeSummary): StageMeta {
  if (node.kind === "worker") {
    return metaForRole(stages, node.role || node.id);
  }
  const platform = PLATFORM_META[node.kind];
  return {
    id: node.id,
    label: node.label || platform?.label || node.kind,
    agent: node.kind,
    responsibility: platform?.responsibility ?? "平台节点。",
  };
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
  if (stages.some((stage) => stage.id === id)) return metaForStage(stages, id);
  // 平台节点（意图 / 编排 / 合成 / 校验）用固定名，不能回落成「id + Agent」。
  const platform = PLATFORM_META[id];
  if (platform) {
    return { id, label: platform.label, agent: id, responsibility: platform.responsibility };
  }
  return metaForRole(stages, role || id);
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
 * 整条流程的节点序列（ADR-038）：意图 / 编排 / 子任务 / 合成 / 校验。
 *
 * 画布**优先读它**，读不到再回落 `plan`（升级前发起的执行只有 `plan`），
 * 最后才回落静态三段。三级回落是为了让旧执行不碎：把「新链路画不全」变成
 * 「按旧形状画」，而不是整块空白。
 */
export function flowNodes(workflow: Workflow | null): FlowNodeSummary[] {
  const flow = workflow?.checkpoint?.flow;
  return Array.isArray(flow) ? flow : [];
}

/**
 * 计划步骤的显示状态。
 *
 * `skipped` 必须原样透出：它和 `pending` 在界面上长得像，但语义完全相反——
 * 一个是「还在等」，一个是「因为上游失败已经放弃」。
 */
export function planStepStatus(step: PlanStepSummary): string {
  return statusOf(step.status);
}

/** 计划步骤与流程节点共用同一份状态词汇（`skipped` 与 `pending` 语义相反）。 */
export function statusOf(status: string): string {
  if (status === "completed") return "completed";
  if (status === "failed") return "failed";
  if (status === "skipped") return "skipped";
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

export type CollabParam = {
  /** 字段名（`temperature` / `top_p` / …），与 `override_keys` 同口径。 */
  key: string;
  label: string;
  value: string;
  /** 该字段来自角色的**显式覆盖**（而不是默认路由 / 环境配置）。 */
  overridden: boolean;
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
  /** 节点类型（ADR-038）：平台节点与子任务在画布上区分展示。 */
  kind: "intent" | "plan" | "worker" | "synthesize" | "validate";
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
  model: string;
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
  /** 整条链路都没有分阶段轨迹时的原因（目前只有动态编排走到这里）。 */
  traceReason: string | null;
  /** 走了哪条路：`single` 单 Agent 直答 / `multi` 波次协作（静态链路为 null）。 */
  route: string | null;
  /** 存在失败/跳过的子任务：结果是部分的，界面上要显式说出来。 */
  partial: boolean;
  failedSteps: string[];
  skippedSteps: string[];
  /** 校验器的结论；没跑校验时为 null。 */
  validation: ValidationSummary | null;
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

function paramsOf(agent: Agent | undefined): CollabParam[] {
  if (!agent) return [];
  const overridden = new Set(agent.override_keys ?? []);
  const rows: { key: string; label: string; value: string }[] = [
    { key: "model", label: "模型", value: agent.model || "未绑定" },
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
  return rows.map((row) => ({
    key: row.key,
    label: row.label,
    value: row.value,
    overridden: overridden.has(row.key),
  }));
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
 * 轨迹（§5.17）与用量（§5.5）都是**可选**的：任务刚开始时两者都还没有，
 * 画布要先能画出来（节点带 pending 状态），不能因为「还没数据」就整块空白。
 */
export function buildCollaboration({
  stages,
  agents,
  workflow,
  completed,
  traces = null,
  metrics = [],
}: {
  stages: StageMeta[];
  agents: Agent[];
  workflow: Workflow | null;
  completed: Set<string>;
  traces?: WorkflowStageTrace | null;
  metrics?: Metric[];
}): CollabGraph {
  const traceReason =
    traces && !traces.items.length ? traces.reason ?? null : null;
  const checkpoint = workflow?.checkpoint ?? null;
  const empty: CollabGraph = {
    nodes: [],
    edges: [],
    waves: [],
    task: traces?.task ?? null,
    mode: traces?.mode ?? "static",
    traceReason,
    route: null,
    partial: false,
    failedSteps: [],
    skippedSteps: [],
    validation: null,
  };
  if (!workflow) return empty;

  // 1) 参与本次执行的节点：流程序列 → 计划步骤 → 静态三段，逐级回落。
  const plan = planSteps(workflow);
  const flow = flowNodes(workflow);
  const entries: {
    id: string;
    kind: CollabNode["kind"];
    meta: StageMeta;
    status: string;
    deps: string[];
  }[] = flow.length
    ? flow.map((node) => ({
        id: node.id,
        kind: node.kind,
        meta: metaForFlowNode(stages, node),
        status: statusOf(node.status),
        deps: node.depends_on ?? [],
      }))
    : plan.length
      ? plan.map((step) => ({
          id: step.id,
          kind: "worker" as const,
          meta: metaForRole(stages, step.role),
          status: planStepStatus(step),
          deps: step.depends_on ?? [],
        }))
      : stages
          .filter((stage) => agents.some((agent) => agent.id === stage.agent))
          .map((stage, index, list) => ({
            id: stage.id,
            kind: "worker" as const,
            meta: stage,
            status: stageStatus(stage.id, workflow, completed),
            // 静态链路是固定串行流水线：每一步都依赖上一步
            deps: index > 0 ? [list[index - 1].id] : [],
          }));

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
    // 平台节点没有 Agent 角色、不谈模型参数与用量：它们跑的是平台自己的模型配置。
    const isWorker = entry.kind === "worker";
    return {
      id: entry.id,
      kind: entry.kind,
      agentId: isWorker ? entry.meta.agent : entry.kind,
      name: isWorker
        ? agent?.name ?? `${entry.meta.agent} Agent`
        : entry.meta.label,
      stageLabel: entry.meta.label,
      responsibility: entry.meta.responsibility,
      status: entry.status,
      order: ids.indexOf(entry.id) + 1,
      wave,
      parallel: (waveCounts.get(wave) ?? 1) > 1,
      model: isWorker ? agent?.model ?? "由运行时提供" : "平台节点",
      provider: isWorker ? agent?.provider_name ?? agent?.provider ?? "" : "",
      params: isWorker ? paramsOf(agent) : [],
      usage: isWorker ? usageFor(metrics, entry.meta.agent) : [],
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
    mode: traces?.mode ?? (plan.length || flow.length ? "dynamic" : "static"),
    traceReason,
    route: checkpoint?.route ?? null,
    partial: Boolean(checkpoint?.partial),
    failedSteps: checkpoint?.failed_steps ?? [],
    skippedSteps: checkpoint?.skipped_steps ?? [],
    validation: checkpoint?.validation ?? null,
  };
}

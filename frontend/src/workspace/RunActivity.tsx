/**
 * 对话流内联的「执行过程」。
 *
 * ## 形态：一行文本，不是一张卡片
 *
 * 网页 AI 的做法是把过程压成一行灰字（`已思考（用时 12 秒）`），点开才铺开。本组件照这个
 * 形态做，**但措辞不叫「思考」**：编排层落盘的是「推理 → 行动 → 观察 → 结论」里的
 * **行动与结论**（`tool_calls` + `content`），模型内部的隐藏推理既没落盘、服务端也不提供
 * （`app/api/stage_trace.py` 模块注释）。叫它「思考」会让人以为下面铺开的是模型的推理过程，
 * 而它不是。所以写「已执行 N 步 · 用时 X」、运行中写「{角色名} 正在执行」。
 * **不伪造思维链**，这一条与 ADR-031 §5、`doc/api.md` §5.17 同口径。
 *
 * 之所以不做成卡片：过程属于这条消息，不属于一块独立面板。加边框与底色会把它从消息里
 * 割出来，看起来像另一个东西（用户 2026-09-22 反馈：专门卡片区域太突兀）。插入点仍由
 * `App.tsx::reportIndex` 定在提问与答复之间——过程要出现在结果的**上一个位置**。
 *
 * 任务分配（planner 的规划理由与分配顺序）**不在这里重复**：协作画布上已有「任务分配」
 * 节点承载同一份数据（ADR-032）。同一份数据在两个面各画一遍，正是「突兀」的来源。
 *
 * ## 折叠口径
 *
 * - 外层一行：步数 + 工具调用次数 + 用时。**用时是算出来的，不是编的**——
 *   `created_at → (completed_at ?? updated_at)`；运行中从 `created_at` 起本地走秒。
 *   不足一秒写「< 1 秒」而不是「0 秒」（库里确有 `created_at == completed_at` 的早期数据）。
 * - 展开后逐步列出，每步仍可单独点开看它调了什么工具、拿到了什么、产出了什么。
 * - 正在跑的那一步默认摊开（人要看的就是它），其余收起；用户手动拨过的开关不被自动规则顶回去。
 * - 收起不等于信息丢失：每步那一行仍写清 **谁 / 阶段名 / 动了几次工具 / 什么状态**。
 *
 * ## 三段小标题与执行台弹窗逐字一致
 *
 * （**分配到的任务 / 执行轨迹 / 阶段产出**）——同一份字段在两处用两套词，读的人会以为
 * 它们是两件事，而那是本仓库反复要避免的漂移。两处唯一的差别是根步骤这里不铺输入原文：
 * 用户的原始任务就在上方那条用户消息里，重复一遍是噪音；弹窗是独立窗口，缺了它读不到上下文。
 *
 * 界面**不写口径脚注**，也不复述任务原文；这两条已按评审删除，`workspace-smoke` 里有断言
 * 盯着它们不回来。**别把它们当成漏了又加回去。**
 *
 * ## 轨迹缺席时不留白
 *
 * 服务端给得出来的四种原因原样转述；「整条链路都没有轨迹」（动态编排）的原因只说一次。
 *
 * ## 规划窗口里不摆固定三步
 *
 * 计划还没落盘的这段时间（`isPlanning`）没有「步」可列：此时唯一的候选是写死的固定三步，
 * 而这次可能只用到一个 Agent。所以这一行改成「正在规划任务分配 · 用时 X」——一个说得出
 * 进度、也说得清自己在等的状态，而不是一列等着被替换掉的假步骤（ADR-034）。
 */
import { useEffect, useMemo, useState } from "react";
import { ChevronRight, CircleCheckBig, LoaderCircle, XCircle } from "lucide-react";
import { AgentGlyph } from "../components/AgentGlyph";
import { Disclosure } from "../components/Disclosure";
import { Markdown } from "../components/Markdown";
import { statusText } from "../components/Status";
import {
  isPlanning,
  planStepStatus,
  planSteps,
  stageMetaFor,
  stageStatus,
  type StageMeta,
} from "./collaboration";
import { ToolCallBlock } from "./TraceParts";
import type {
  Agent,
  StageToolCall,
  StageTraceItem,
  ToolCall,
  Workflow,
  WorkflowStageTrace,
} from "../types/api";

type RunStep = {
  id: string;
  /** Agent 角色 id。 */
  role: string;
  /** 角色显示名；角色未登记时回退到阶段名。 */
  name: string;
  /** 阶段中文名。 */
  label: string;
  status: string;
  /** 该步骤的执行轨迹；服务端还没落盘时为 `null`。 */
  trace: StageTraceItem | null;
  /** 轨迹缺席时的具体原因（服务端给，原样转述）。 */
  reason: string;
};

function toolCount(trace: StageTraceItem | null): number {
  return trace ? trace.tool_calls.length : 0;
}

/**
 * 本次执行的耗时（秒）。
 *
 * **不是估的**：起点取 `created_at`（真实落库时间），终点终态取 `completed_at`
 * （缺了退 `updated_at`），运行中取当下。取不到就返回 `null`，宁可这一项不显示，
 * 也不摆一个编出来的数字。
 */
function elapsedSeconds(workflow: Workflow, now: number): number | null {
  const started = Date.parse(workflow.created_at);
  if (!Number.isFinite(started)) return null;
  const finished = !["running", "paused", "pending"].includes(workflow.status);
  const endedRaw = finished ? (workflow.completed_at ?? workflow.updated_at) : null;
  const ended = endedRaw ? Date.parse(endedRaw) : now;
  if (!Number.isFinite(ended)) return null;
  return Math.max(0, Math.round((ended - started) / 1000));
}

function formatElapsed(total: number): string {
  // 不足一秒写「< 1 秒」而不是「0 秒」：后者读起来像坏了。
  // 库里确有 `created_at == completed_at` 的历史行（早期数据），那是数据的事，
  // 前端不替它圆谎——如实说「不到一秒」。
  if (total < 1) return "< 1 秒";
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
}

/**
 * 拼出本次执行要展示的步骤序列。
 *
 * 三条链路给的形状不同，但**顺序的权威来源只有一个**：动态链路按 `plan` 的声明顺序，
 * 静态链路按流水线的阶段顺序。轨迹（§5.17）只提供过程与产出，不决定顺序——
 * 让轨迹决定顺序会出现「某一步还没落盘就从列表里消失了」（它恰恰是还没轮到的那些）。
 */
function buildSteps({
  workflow,
  traces,
  stages,
  agents,
  completed,
}: {
  workflow: Workflow;
  traces: WorkflowStageTrace | null;
  stages: StageMeta[];
  agents: Agent[];
  completed: Set<string>;
}): RunStep[] {
  const plan = planSteps(workflow);
  const items = traces?.items ?? [];

  const order: string[] = [];
  const push = (id: string) => {
    if (id && !order.includes(id)) order.push(id);
  };
  if (plan.length) plan.forEach((step) => push(step.id));
  else if (items.length) items.forEach((item) => push(item.stage));
  else stages.forEach((stage) => push(stage.id));
  // 计划之外的轨迹（同一份数据两个来源之间短暂不同步）也要出现，不能悄悄丢。
  items.forEach((item) => push(item.stage));
  plan.forEach((step) => push(step.id));

  return order.map((id) => {
    const trace = items.find((item) => item.stage === id) ?? null;
    const step = plan.find((item) => item.id === id) ?? null;
    const meta = stageMetaFor(stages, id, trace?.role ?? step?.role);
    const role = trace?.role ?? step?.role ?? meta.agent;
    const agent = agents.find((item) => item.role === role) ?? null;
    return {
      id,
      role,
      name: agent?.name ?? meta.label,
      label: meta.label,
      status: step ? planStepStatus(step) : stageStatus(id, workflow, completed),
      trace,
      reason: trace?.reason ?? "",
    };
  });
}

/** 外层那一行的图标：终态给勾/叉，进行中给转圈。 */
function ChainGlyph({ status }: { status: string }) {
  if (status === "running") return <LoaderCircle size={13} className="spin" />;
  if (status === "completed") return <CircleCheckBig size={13} />;
  if (status === "failed" || status === "cancelled") return <XCircle size={13} />;
  return <LoaderCircle size={13} />;
}

export function RunActivity({
  workflow,
  traces,
  stages,
  agents,
  completed,
  requestedMode,
  liveToolCalls,
}: {
  workflow: Workflow;
  traces: WorkflowStageTrace | null;
  stages: StageMeta[];
  agents: Agent[];
  completed: Set<string>;
  /** 提交这次任务时声明的编排模式；只有实时工作流传，历史会话按它自己的数据判断。 */
  requestedMode?: string | null;
  /** 运行中「正在发生」的工具调用（§5.4 实时轮询），步骤还没落盘时用它近似流式展示。 */
  liveToolCalls?: ToolCall[];
}) {
  const running = ["running", "paused", "pending"].includes(workflow.status);
  const activeStep = workflow.checkpoint?.current_step ?? workflow.current_step ?? "";
  // 计划还没落盘（动态编排的规划窗口）：这一步没有「步」可列，见文件头注释。
  const planning = isPlanning(workflow, requestedMode);
  const steps = useMemo(
    () => (planning ? [] : buildSteps({ workflow, traces, stages, agents, completed })),
    [planning, workflow, traces, stages, agents, completed],
  );

  // 运行中的「用时」要一秒一秒地走，否则它看起来是个跑完才有的静态数字。
  // 只有运行中才挂定时器——终态的耗时是算死的，不需要重渲染。
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (workflow.status !== "running" && workflow.status !== "paused") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [workflow.status]);

  /** 用户手动拨过的开关。只记「被动过」的那些，其余走自动规则。 */
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const setStep = (id: string, open: boolean) =>
    setManual((current) => ({ ...current, [id]: open }));
  const isOpen = (step: RunStep) =>
    manual[step.id] ?? (running && step.id === activeStep);

  // 外层那一行。跑完自动收起（网页 AI 也是这个行为），运行中自动摊开——人要看的就是它。
  const [chainManual, setChainManual] = useState<boolean | null>(null);
  const chainOpen = chainManual ?? (running && steps.length > 0);
  const bodyId = `run-chain-body-${workflow.id}`;

  // 只有正在产出的那一步才播放渐进揭示：跑完的那些直接给全文，
  // 否则回看历史时整屏逐字重放，看起来像任务在重新执行。
  const lastProduced = steps.reduce(
    (found, step, index) => (step.trace?.output ? index : found),
    -1,
  );
  const anyTrace = steps.some((step) => step.trace);
  const overallReason = anyTrace ? "" : (traces?.reason ?? "");
  // 运行中「正在跑的那一步」用 §5.4 实时工具调用近似流式：把 ToolCall 转成
  // StageToolCall 形状（字段对齐 `tool_calls` 表 → §5.17 的 `tool_name` / `status`）。
  const liveCalls: StageToolCall[] = (liveToolCalls ?? []).map((call) => ({
    call_id: call.id,
    tool_name: call.tool_name,
    status: call.status === "succeeded" ? "succeeded" : call.status === "failed" ? "failed" : "running",
    input: call.input,
    output: call.output,
    error: call.error,
  }));

  const plan = planSteps(workflow);
  const done = completed.size;
  const unit = plan.length ? "步" : "个阶段";
  // 已落盘的工具调用 + 还没落盘但正在发生的那些（否则运行中这一行会一直写 0 次）。
  const tracedTools = steps.reduce((sum, step) => sum + toolCount(step.trace), 0);
  const liveOnly =
    running && activeStep && !steps.some((step) => step.id === activeStep && step.trace)
      ? liveCalls.length
      : 0;
  const totalTools = tracedTools + liveOnly;
  const seconds = elapsedSeconds(workflow, now);
  const missingReport =
    workflow.status === "completed" && !steps.some((step) => step.trace?.output);

  // 一句话说清「现在跑到哪」：跑完说步数与耗时，跑着说轮到谁。
  const activeName = steps.find((step) => step.id === activeStep)?.name ?? "";
  const headline = (() => {
    if (workflow.status === "completed") return `已执行 ${steps.length} ${unit}`;
    if (workflow.status === "failed") return `任务执行失败 · 已完成 ${done} ${unit}`;
    if (workflow.status === "cancelled") return `任务已取消 · 已完成 ${done} ${unit}`;
    if (workflow.status === "paused") return `${activeName || "任务"} 已暂停`;
    if (workflow.status === "pending") return "任务排队中";
    return `${activeName || "任务"} 正在执行`;
  })();

  return (
    <section className="run-activity" aria-label="任务执行过程">
      {steps.length ? (
        <button
          type="button"
          className={`run-chain-head${chainOpen ? " is-open" : ""}`}
          aria-expanded={chainOpen}
          {...(chainOpen ? { "aria-controls": bodyId } : {})}
          onClick={() => setChainManual(!chainOpen)}
        >
          <span className={`run-chain-glyph ${workflow.status}`}>
            <ChainGlyph status={workflow.status} />
          </span>
          <span className="run-chain-text">
            {headline}
            {totalTools > 0 && ` · ${totalTools} 次工具调用`}
            {seconds !== null && ` · 用时 ${formatElapsed(seconds)}`}
          </span>
          <ChevronRight size={12} className="run-chain-chevron" aria-hidden="true" />
        </button>
      ) : planning ? (
        // 规划窗口里没有可展开的东西，就不给按钮：一个点了没反应的开关比没有开关更糟。
        <span className="run-chain-head is-planning">
          <span className="run-chain-glyph running">
            <LoaderCircle size={13} className="spin" />
          </span>
          <span className="run-chain-text">
            正在规划任务分配
            {seconds !== null && ` · 用时 ${formatElapsed(seconds)}`}
          </span>
        </span>
      ) : null}

      {steps.length > 0 && chainOpen ? (
        <div className="run-chain-body" id={bodyId}>
          <div className="run-steps">
            {steps.map((step, index) => {
              // 实时工具调用只归属「正在跑的那一步」。
              const isLiveStep = running && step.id === activeStep && liveCalls.length > 0;
              return (
                <Disclosure
                  key={step.id}
                  id={`run-step-body-${step.id}`}
                  open={isOpen(step)}
                  onToggle={() => setStep(step.id, !isOpen(step))}
                  glyph={<AgentGlyph role={step.role} name={step.name} size={13} />}
                  title={step.name}
                  meta={
                    <>
                      {/* 收起后这一行必须自己说清「谁、什么阶段、动了几次工具、什么状态」，
                          否则收起就等于把信息丢了。 */}
                      <span className="run-step-label">{step.label}</span>
                      {toolCount(step.trace) > 0 && (
                        <span className="run-step-tools">
                          {toolCount(step.trace)} 次工具调用
                        </span>
                      )}
                      <span className={`run-step-state ${step.status}`}>
                        {statusText[step.status] ?? step.status}
                      </span>
                    </>
                  }
                >
                  {step.trace ? (
                    <>
                      {step.trace.input && (
                        <div className="ws-detail-section">
                          <h4>
                            {/* 三段小标题与执行台弹窗**逐字一致**（分配到的任务 / 执行轨迹 / 阶段产出）。
                             *  同一份字段在两处用两套词，正是本仓库反复要避免的那类漂移。
                             *  唯一的差别是根步骤这里不铺输入原文——用户的原始任务就在上方那条
                             *  消息里，重复一遍是噪音；弹窗是独立窗口，缺了它就读不到上下文。 */}
                            {`分配到的任务（来自${step.trace.input_from ?? "上游"}阶段）`}
                          </h4>
                          <pre className="ws-trace-block">{step.trace.input}</pre>
                        </div>
                      )}

                      <div className="ws-detail-section">
                        <h4>
                          执行轨迹
                          {(toolCount(step.trace) > 0 || isLiveStep) && (
                            <span className="ws-trace-count">{`${Math.max(toolCount(step.trace), isLiveStep ? liveCalls.length : 0)} 次工具调用`}</span>
                          )}
                        </h4>
                        {toolCount(step.trace) ? (
                          <div className="ws-trace-list">
                            {step.trace.tool_calls.map((call, callIndex) => (
                              <ToolCallBlock
                                key={call.call_id || `${call.tool_name}-${callIndex}`}
                                call={call}
                              />
                            ))}
                          </div>
                        ) : isLiveStep && liveCalls.length ? (
                          <div className="ws-trace-list">
                            {liveCalls.map((call, callIndex) => (
                              <ToolCallBlock
                                key={call.call_id || `${call.tool_name}-${callIndex}`}
                                call={call}
                              />
                            ))}
                          </div>
                        ) : (
                          <p className="ws-trace-note">
                            这一步没有调用工具，直接由模型产出结论。
                          </p>
                        )}
                      </div>

                      {step.trace.output ? (
                        <div className="ws-detail-section">
                          <h4>
                            阶段产出
                            {step.trace.truncated && <span className="ws-trace-count">已截断</span>}
                          </h4>
                          {/* 模型产出是 Markdown（标题/列表/表格/代码块），必须按 Markdown 渲染，
                              否则满屏记号。见 components/Markdown.tsx。 */}
                          <Markdown stream={running && index === lastProduced}>
                            {step.trace.output}
                          </Markdown>
                        </div>
                      ) : (
                        !overallReason && (
                          <p className="ws-trace-note">
                            {step.reason || "该步骤还没跑到，产出尚未写入。"}
                          </p>
                        )
                      )}
                    </>
                  ) : (
                    !overallReason && (
                      <p className="ws-trace-note">
                        {step.reason || "该步骤的执行轨迹尚未写入。"}
                      </p>
                    )
                  )}
                </Disclosure>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* 说明摆在折叠之外：它们是「这次为什么看不到过程」的答案，收起来就等于不说。 */}
      {overallReason && <p className="run-note">{overallReason}</p>}
      {!steps.length && !overallReason && (
        <p className="run-note">正在读取本次执行的阶段信息…</p>
      )}
      {missingReport && <p className="run-note">当前接口尚未返回报告正文。</p>}
      {traces?.mode === "dynamic" && anyTrace && (
        <p className="run-note">动态编排链路不落盘逐步骤顺序，步骤顺序按本次计划声明。</p>
      )}
    </section>
  );
}

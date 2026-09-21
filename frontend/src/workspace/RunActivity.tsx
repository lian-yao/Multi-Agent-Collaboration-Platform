/**
 * 对话流内联的「任务执行活动」卡片。
 *
 * ## 它是第四个面，边界必须重新划清
 *
 * `doc/decisions/018` 定过「三块视图职责互斥、不重复渲染同一份数据」：执行台弹窗管单个
 * Agent 的轨迹、侧栏管任务级、记录页管逐条明细。本卡片把**执行轨迹搬进了对话流**——
 * 于是第四个面出现了。重复渲染同一份数据在别处是浪费，在这里不是：主流 Agent 的
 * 对话流本来就把「它调了什么工具、结果是什么」铺在消息之间，让读者在**结果出现的那个
 * 位置**读到过程；要用户跑去另一块面板里对时间线，等于把「过程」从「结果」旁边搬走。
 *
 * 因此三个面的分工按**时间**重划，而不是按数据：
 * - **对话流（本卡片）**：这一次执行的**当下**——正在跑哪一步、那一步调了什么工具、
 *   拿到了什么、产出了什么。随轮询逐步长出来。
 * - **执行台弹窗**：单步的**完整**轨迹（含上游输入原文与被截断标记），跑完之后回看用。
 * - **记录页**：跨任务的逐条采样与调用明细，审计用。
 *
 * 三处都只读同一份服务端数据（§5.17），谁都不写、都不缓存，因此不存在「哪一份为准」的问题。
 *
 * ## 不伪造思维链
 *
 * 编排层落盘的是「推理 → 行动 → 观察 → 结论」里的**行动与结论**（`tool_calls` + `content`），
 * 模型内部的隐藏推理不在其中，服务端也不提供（`app/api/stage_trace.py` 模块注释）。
 * 本卡片按「这一步动了哪些工具、拿到了什么、产出了什么」如实呈现，**不编造中间过程**，
 * 也不在拿不到轨迹时留白——服务端给得出来的四种原因原样转述。
 *
 * 三段的措辞（**分配到的任务 / 执行轨迹 / 阶段产出**）与执行台弹窗**逐字一致**：同一份字段
 * 在两处用两套词，读的人会以为它们是两件事，而那是本仓库反复要避免的漂移。
 * 两处唯一的差别是根步骤这里不铺输入原文——原始任务就在上方那条用户消息里。
 *
 * ## 默认展开规则
 *
 * 正在跑的那一步自动摊开（人要看的就是它），已完成的收成一行摘要（摘要行仍写清
 * 谁 / 什么状态 / 动了几次工具，所以收起不等于信息丢失）。用户手动开过的步骤按用户的意思，
 * 不被自动规则顶回去。
 */
import { useMemo, useState } from "react";
import { Activity, LoaderCircle } from "lucide-react";
import { AgentGlyph } from "../components/AgentGlyph";
import { Disclosure } from "../components/Disclosure";
import { Markdown } from "../components/Markdown";
import { Status, statusText } from "../components/Status";
import {
  isTruncated,
  planStepStatus,
  planSteps,
  stageMetaFor,
  stageStatus,
  type StageMeta,
} from "./collaboration";
import { ToolCallBlock } from "./TraceParts";
import type {
  Agent,
  StageTraceItem,
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

/** 状态 → 左侧竖条与图标底色。状态是人要立刻看见的信号，配色跟着状态走。 */
const STEP_TONE: Record<string, string> = {
  completed: "green",
  running: "accent",
  paused: "amber",
  failed: "rose",
  skipped: "amber",
  pending: "muted",
};

function toolCount(trace: StageTraceItem | null): number {
  return trace ? trace.tool_calls.length : 0;
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

export function RunActivity({
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
}) {
  const running = ["running", "paused", "pending"].includes(workflow.status);
  const activeStep = workflow.checkpoint?.current_step ?? workflow.current_step ?? "";
  const steps = useMemo(
    () => buildSteps({ workflow, traces, stages, agents, completed }),
    [workflow, traces, stages, agents, completed],
  );

  /** 用户手动拨过的开关。只记「被动过」的那些，其余走自动规则。 */
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const setStep = (id: string, open: boolean) =>
    setManual((current) => ({ ...current, [id]: open }));
  const isOpen = (step: RunStep) =>
    manual[step.id] ?? (running && step.id === activeStep);

  // 只有正在产出的那一步才播放渐进揭示：跑完的那些直接给全文，
  // 否则回看历史时整屏逐字重放，看起来像任务在重新执行。
  const lastProduced = steps.reduce(
    (found, step, index) => (step.trace?.output ? index : found),
    -1,
  );
  const anyTrace = steps.some((step) => step.trace);
  const overallReason = anyTrace ? "" : (traces?.reason ?? "");
  const plan = planSteps(workflow);
  const done = completed.size;
  const missingReport =
    workflow.status === "completed" && !steps.some((step) => step.trace?.output);

  return (
    <section className="run-activity" aria-label="任务执行活动">
      <header className="run-activity-head">
        <span className={`run-activity-mark ${workflow.status}`}>
          {workflow.status === "running" ? (
            <LoaderCircle size={15} className="spin" />
          ) : (
            <Activity size={15} />
          )}
        </span>
        <div>
          <b>任务{statusText[workflow.status] ?? workflow.status}</b>
          <span>
            {plan.length
              ? `自动编排 · 计划 ${plan.length} 步，已完成 ${done} 步`
              : `已完成 ${done} 个阶段`}
            {" · 展开任一步可看它调用的工具与产出"}
          </span>
        </div>
      </header>

      {steps.length ? (
        <div className="run-steps">
          {steps.map((step, index) => (
            <Disclosure
              key={step.id}
              id={`run-step-body-${step.id}`}
              open={isOpen(step)}
              onToggle={() => setStep(step.id, !isOpen(step))}
              tone={STEP_TONE[step.status] ?? "muted"}
              glyph={<AgentGlyph role={step.role} name={step.name} size={14} />}
              title={step.name}
              meta={
                <>
                  {/* 收起后摘要行必须自己说清「谁、什么状态、动了几次工具」，
                      否则收起就等于把信息丢了。 */}
                  <span className="run-step-label">{step.label}</span>
                  {toolCount(step.trace) > 0 && (
                    <span className="run-step-tools">
                      {toolCount(step.trace)} 次工具调用
                    </span>
                  )}
                  <Status status={step.status} />
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
                      {toolCount(step.trace) > 0 && (
                        <span className="ws-trace-count">{`${toolCount(step.trace)} 次工具调用`}</span>
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
                        {step.trace.truncated && (
                          <span className="ws-trace-count">已截断</span>
                        )}
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
          ))}
        </div>
      ) : null}

      {overallReason && <p className="run-note">{overallReason}</p>}
      {!steps.length && !overallReason && (
        <p className="run-note">正在读取本次执行的阶段信息…</p>
      )}
      {missingReport && (
        <p className="run-note">当前接口尚未返回报告正文。</p>
      )}
      {traces?.task && (
        <p className="run-note run-task" title={traces.task}>
          本次任务：{traces.task}
        </p>
      )}
      {traces?.mode === "dynamic" && anyTrace && (
        <p className="run-note">
          动态编排链路不落盘逐步骤顺序，步骤顺序按本次计划声明。
        </p>
      )}
      {anyTrace && (
        <p className="run-note">
          这里显示的是编排层落盘的执行链路。模型内部的隐藏推理没有落盘，因此不在其中；
          单步的完整轨迹与截断细节可在「Agent 执行台」打开。
        </p>
      )}
    </section>
  );
}

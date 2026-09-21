/**
 * 单个 Agent 的执行轨迹弹窗。
 *
 * 这里回答的是「**这个 Agent 收到了什么任务、调了哪些工具、产出了什么**」——
 * 执行台卡片点开的就是这条链路本身，而不是它的模型参数。角色绑定与调参在
 * 「Agent 团队」页，逐条工具调用明细在「任务记录」页，三处不重复渲染同一份数据
 * （`doc/api.md` §7）。此前这一层放的是「生效模型 / Temperature / 覆盖项」，用户
 * 点开卡片想看的却是它干了什么——那是弹窗的定位错了，不是缺一个字段。
 *
 * 关于「思维链」：编排层落盘的是**推理 → 行动 → 观察 → 结论**里的行动与结论
 * （`tool_calls` + `content`），模型内部的隐藏推理不在其中。弹窗按「收到的输入 →
 * 工具调用 → 阶段产出」如实呈现这条链路，不伪造模型没说出口的中间过程。
 *
 * 视图本体只吃显式 props（`AgentTraceView` 可单独离屏渲染），数据由容器拉取，
 * 便于 `frontend/rendercheck` 直接挂载。
 */
import { Bot, ChevronRight, ExternalLink } from "lucide-react";
import { Modal, formatTime } from "../config/shared";
import { Status } from "../components/Status";
/** 「一次工具调用」的渲染在 `TraceParts.tsx`：对话流内联轨迹要用同一份，两处各写一份必漂移。 */
import { ToolCallBlock } from "./TraceParts";
import type { Agent, StageTraceItem } from "../types/api";
import "../config/config.css";
import "./workspace.css";

export type AgentStageDetail = {
  /** 阶段 ID，与 Workflow 的阶段字段对齐。 */
  stageId: string;
  /** 阶段显示名，如「信息收集」。 */
  stageLabel: string;
  /** 该阶段在链路里的职责说明（静态文案，由调用方提供）。 */
  responsibility: string;
  /** 阶段状态：completed / running / paused / failed / pending。 */
  status: string;
  /** `GET /api/v1/agents` 返回的生效配置；角色未就绪时为 `null`。 */
  agent: Agent | null;
  updatedAt?: string;
};

/* 工具入参 / 出参的可读文案与截断判断在 `collaboration.ts`——协作画布与对话流内联轨迹
 * 都要用同一套，各写一份会出现「弹窗说已截断、别处当成全部」的错位。 */

/**
 * 轨迹主体：只吃 trace / 三态标志，不拉数据。
 *
 * 四种「没有轨迹」的 `reason` 由服务端给出并原样显示——它们是四件不同的事，
 * 在这儿改写成一句「暂无数据」等于把服务端的判断丢掉。
 */
export function AgentTraceView({
  trace,
  loading = false,
  error = "",
  overallReason = "",
}: {
  trace: StageTraceItem | null;
  loading?: boolean;
  error?: string;
  /** 整条链路都没有轨迹时的原因（动态编排）。 */
  overallReason?: string;
}) {
  if (loading) return <p className="cfg-hint">正在读取该 Agent 的执行轨迹…</p>;
  if (error) {
    return (
      <p className="cfg-alert" role="alert">
        {error}
      </p>
    );
  }
  if (overallReason) {
    return (
      <div className="ws-detail-section">
        <h4>执行轨迹</h4>
        <p className="ws-trace-note">{overallReason}</p>
      </div>
    );
  }
  if (!trace || (!trace.output && !trace.tool_calls.length)) {
    return (
      <div className="ws-detail-section">
        <h4>执行轨迹</h4>
        <p className="ws-trace-note">{trace?.reason ?? "暂无该阶段的执行记录。"}</p>
      </div>
    );
  }

  return (
    <>
      {(trace.input || trace.output) && (
        <div className="ws-detail-section">
          <h4>{trace.input ? `分配到的任务（来自${trace.input_from ?? "上游"}阶段）` : "分配到的任务"}</h4>
          <pre className="ws-trace-block">
            {trace.input ?? "（根阶段：本阶段收到的就是用户提出的原始任务）"}
          </pre>
        </div>
      )}

      <div className="ws-detail-section">
        <h4>
          执行轨迹
          {trace.tool_calls.length > 0 && (
            <span className="ws-trace-count">{`${trace.tool_calls.length} 次工具调用`}</span>
          )}
        </h4>
        {trace.tool_calls.length ? (
          <div className="ws-trace-list">
            {trace.tool_calls.map((call, index) => (
              <ToolCallBlock key={call.call_id || `${call.tool_name}-${index}`} call={call} />
            ))}
          </div>
        ) : (
          <p className="ws-trace-note">
            该阶段没有调用工具，直接由模型产出结论。
          </p>
        )}
      </div>

      {trace.output && (
        <div className="ws-detail-section">
          <h4>阶段产出{trace.truncated && <span className="ws-trace-count">已截断</span>}</h4>
          <pre className="ws-trace-block ws-trace-output">{trace.output}</pre>
        </div>
      )}
    </>
  );
}

export function AgentStageModal({
  detail,
  trace,
  traceLoading = false,
  traceError = "",
  overallReason = "",
  onClose,
  onOpenRecords,
}: {
  detail: AgentStageDetail;
  /** 该阶段的执行轨迹（`GET /workflows/{id}/stages` 里匹配 `stageId` 的那一条）。 */
  trace?: StageTraceItem | null;
  traceLoading?: boolean;
  traceError?: string;
  overallReason?: string;
  onClose: () => void;
  /** 没有它就只读展示，不渲染「查看工具调用」入口。 */
  onOpenRecords?: () => void;
}) {
  const { agent } = detail;
  return (
    <Modal
      title={`${agent?.name ?? `${detail.stageId} Agent`} · 执行轨迹`}
      subtitle={`${detail.stageLabel}阶段 · ${detail.stageId}`}
      wide
      onClose={onClose}
      footer={
        onOpenRecords && (
          <button type="button" className="cfg-quiet" onClick={onOpenRecords}>
            <ExternalLink size={13} />
            在任务记录中查看工具调用与指标
          </button>
        )
      }
    >
      <div className="ws-detail">
        <div className="ws-detail-head">
          <span className="ws-detail-avatar">
            <Bot size={15} />
          </span>
          <span className="ws-detail-role">
            <b>{agent?.role ?? detail.stageId}</b>
            <small>
              {detail.responsibility}
              {detail.updatedAt && ` · 最近更新 ${formatTime(detail.updatedAt)}`}
            </small>
          </span>
          <Status status={detail.status} />
        </div>

        <AgentTraceView
          trace={trace ?? null}
          loading={traceLoading}
          error={traceError}
          overallReason={overallReason}
        />

        <p className="ws-detail-note">
          这里显示的是编排层落盘的执行链路：该阶段收到的上游输入、实际发生的工具调用与它的产出。
          模型内部的隐藏推理没有落盘，因此不在其中；该阶段的模型与参数在「Agent 团队」页。
          <ChevronRight size={11} />
        </p>
      </div>
    </Modal>
  );
}

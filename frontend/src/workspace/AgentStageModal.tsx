/**
 * 单个 Agent 的阶段详情弹窗。
 *
 * 职责边界：**执行台上的卡片点开的是「谁在干、干到哪一步」**，属于身份与状态；
 * 任务级的用量与协作关系留在右侧「任务协作」侧栏，逐条采样明细留在「任务记录」页。
 * 三者不重复渲染同一份数据（`doc/api.md` §7）。
 *
 * 只吃显式 props，不自己拉数据，便于离屏渲染冒烟挂载。
 */
import { Bot, ExternalLink } from "lucide-react";
import { Modal, formatTime } from "../config/shared";
import { Status } from "../components/Status";
import type { Agent } from "../types/api";
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
  /** 该阶段是否已写入检查点。 */
  checkpointSaved: boolean;
  updatedAt?: string;
};

const shown = (value: number | null | undefined, suffix = "") =>
  value === null || value === undefined ? "未设置" : `${value}${suffix}`;

/** 阶段状态文案。与 `components/Status.tsx` 的 workflow 词汇分开：
 *  这里的 `pending` 是「还没轮到」，不是「排队中」。 */
export const stageStateText: Record<string, string> = {
  pending: "等待前置阶段",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

export function AgentStageModal({
  detail,
  onClose,
  onOpenRecords,
}: {
  detail: AgentStageDetail;
  onClose: () => void;
  /** 没有它就只读展示，不渲染「查看工具调用」入口。 */
  onOpenRecords?: () => void;
}) {
  const { agent } = detail;
  return (
    <Modal
      title={agent?.name ?? `${detail.stageId} Agent`}
      subtitle={`${detail.stageLabel}阶段 · ${detail.stageId}`}
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
            <small>{agent?.status === "idle" ? "静态角色" : (agent?.status ?? "未就绪")}</small>
          </span>
          <Status status={detail.status} />
        </div>

        <div className="ws-detail-section">
          <h4>阶段职责</h4>
          <p className="inspector-copy">{detail.responsibility}</p>
        </div>

        <div className="ws-detail-section">
          <h4>阶段执行</h4>
          <dl className="agent-facts">
            <div>
              <dt>阶段状态</dt>
              <dd>{stageStateText[detail.status] ?? detail.status}</dd>
            </div>
            <div>
              <dt>检查点</dt>
              <dd>{detail.checkpointSaved ? "已保存" : "暂无已完成记录"}</dd>
            </div>
            <div>
              <dt>最近更新</dt>
              <dd>{formatTime(detail.updatedAt)}</dd>
            </div>
          </dl>
        </div>

        <div className="ws-detail-section">
          <h4>角色与模型绑定</h4>
          <dl className="agent-facts">
            <div>
              <dt>生效模型</dt>
              <dd>{agent?.model ?? "未提供"}</dd>
            </div>
            <div>
              <dt>Provider</dt>
              <dd>{agent?.provider_name ?? agent?.provider ?? "未提供"}</dd>
            </div>
            <div>
              <dt>Temperature</dt>
              <dd>{shown(agent?.temperature)}</dd>
            </div>
            <div>
              <dt>Top P</dt>
              <dd>{shown(agent?.top_p)}</dd>
            </div>
            <div>
              <dt>输出上限</dt>
              <dd>{agent?.max_output_tokens ? `${agent.max_output_tokens} tokens` : "未设置"}</dd>
            </div>
            <div>
              <dt>覆盖项</dt>
              <dd>{agent?.override_keys?.length ? agent.override_keys.join("、") : "无，走默认路由"}</dd>
            </div>
          </dl>
        </div>

        <p className="ws-detail-note">
          该阶段的推理过程与原始输出目前在编排层内部消费，尚未对外暴露；可观测到的替代信息是
          「任务记录」里的工具调用链路与指标采样。
        </p>
      </div>
    </Modal>
  );
}

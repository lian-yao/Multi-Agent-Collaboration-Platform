/**
 * 协作链路视图。
 *
 * 按「波次（wave）」表达 Agent 协作：**同一波内的节点并行，波与波之间串行**。
 * 当前后端是固定串行流水线，所以每波只有一个节点；将来编排层支持 fan-out /
 * Human-in-the-Loop 波次时，只要把同波节点放进同一个数组，这里无需改动——
 * 「并行 / 串行 / 等待」三种关系因此可以用同一个模型表达。
 *
 * 只吃显式 props、不自己拉数据：容器组件靠 effect 拉数据，静态渲染拿不到内容，
 * 视图本体必须能被离屏冒烟直接挂载（见 `frontend/rendercheck`）。
 */
import { Check, ChevronRight, CircleAlert, Clock3, LoaderCircle, Pause } from "lucide-react";
import { Status } from "../components/Status";
import "./workspace.css";

export type CollaboratorNode = {
  /** 阶段 ID，与 Workflow 的 `current_step` / `checkpoint.completed_steps` 对齐。 */
  id: string;
  /** 阶段显示名，如「信息收集」。 */
  stageLabel: string;
  /** Agent 显示名；缺配置时由调用方兜底成 `<id> Agent`。 */
  agentName: string;
  /** completed / running / paused / failed / pending，取自 `stageStatus()`。 */
  status: string;
};

const NODE_ICON: Record<string, typeof Check> = {
  completed: Check,
  running: LoaderCircle,
  paused: Pause,
  failed: CircleAlert,
};

export function CollaborationGraph({
  waves,
  activeId,
  onSelect,
}: {
  /** 波次列表：波内并行、波间串行。空数组表示本次任务还没有参与 Agent。 */
  waves: CollaboratorNode[][];
  /** 当前执行中的阶段 ID，用于高亮。 */
  activeId?: string | null;
  /** 传入即可点击节点跳转到该阶段的详情。 */
  onSelect?: (id: string) => void;
}) {
  if (!waves.length) {
    return <p className="collab-empty">任务开始后，这里会显示 Agent 之间的协作关系。</p>;
  }
  const hasParallel = waves.some((wave) => wave.length > 1);
  return (
    <div className="collab-graph" aria-label="Agent 协作链路">
      {waves.map((wave, index) => (
        <div className="collab-wave" key={wave.map((node) => node.id).join("+")}>
          {index > 0 && (
            <div className="collab-link" aria-hidden="true">
              <span />
              <small>串行</small>
            </div>
          )}
          <div className={`collab-row ${wave.length > 1 ? "parallel" : ""}`}>
            {wave.map((node) => {
              const Icon = NODE_ICON[node.status] ?? Clock3;
              const body = (
                <>
                  <span className={`collab-node-mark ${node.status}`}>
                    <Icon size={13} className={node.status === "running" ? "spin" : undefined} />
                  </span>
                  <span className="collab-node-body">
                    <b>{node.agentName}</b>
                    <small>
                      {node.stageLabel}
                      {node.status === "pending" && " · 等待前置阶段"}
                    </small>
                  </span>
                  <Status status={node.status} />
                </>
              );
              return onSelect ? (
                <button
                  type="button"
                  key={node.id}
                  className={`collab-node ${node.status} ${activeId === node.id ? "active" : ""}`}
                  aria-label={`查看${node.agentName}的${node.stageLabel}阶段详情`}
                  onClick={() => onSelect(node.id)}
                >
                  {body}
                  <ChevronRight size={13} />
                </button>
              ) : (
                <div
                  key={node.id}
                  className={`collab-node ${node.status} ${activeId === node.id ? "active" : ""}`}
                >
                  {body}
                </div>
              );
            })}
          </div>
        </div>
      ))}
      <p className="collab-legend">
        <span>
          <i className="done" />
          已完成
        </span>
        <span>
          <i className="doing" />
          执行中
        </span>
        <span>
          <i className="wait" />
          等待前置
        </span>
        <span className="collab-mode">{hasParallel ? "含并行波次" : "串行流水线"}</span>
      </p>
    </div>
  );
}

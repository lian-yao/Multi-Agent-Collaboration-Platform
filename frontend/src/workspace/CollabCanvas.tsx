/**
 * 全屏协作工作流画布。
 *
 * 侧栏那块是「一眼看个大概」，这里是把同一张图放大到能读细节：节点是 Agent 卡片，
 * 连线是数据流转，**悬停**节点看它的参数 / Token / 分配到的任务 / 产出，**悬停**连线
 * 看这条链路上到底调用了哪些工具、入参出参是什么。
 *
 * 顶部按「对话编号」切换：会话里每一次提问各自跑出一个工作流，编号沿用历史列表里的
 * 「任务 N」口径（`doc/api.md` §5.18）。编号必须固定——用户会用「对话 3 那次」指代它，
 * 一旦编号随新对话漂移，这句话就没有指代对象了。
 *
 * 悬停详情用 CSS `:hover` / `:focus-within` 常驻在 DOM 里，不引入 JS hover 状态：
 * 一是键盘 Tab 也能看到同样内容，二是离屏冒烟能直接断言这些文本确实渲染了。
 */
import { useEffect } from "react";
import { GitBranch, MessageSquareText, X } from "lucide-react";
import { Status } from "../components/Status";
import { AgentNodeCard } from "./CollaborationGraph";
import type { CollabGraph } from "./collaboration";
import "../config/config.css";
import "./workspace.css";

/** 一次对话对应的工作流。`index` 就是界面上显示的对话编号（1 起）。 */
export type CollabConversation = {
  id: string;
  index: number;
  label: string;
  status: string;
};

export function CollabCanvas({
  open,
  graph,
  conversations = [],
  selectedId = null,
  onSelect,
  loading = false,
  error = "",
  onClose,
}: {
  open: boolean;
  graph: CollabGraph;
  /** 本会话所有对话的协作工作流（升序，编号即下标 + 1）。 */
  conversations?: CollabConversation[];
  selectedId?: string | null;
  onSelect?: (workflowId: string) => void;
  loading?: boolean;
  error?: string;
  onClose: () => void;
}) {
  // Esc 关掉画布：全屏层把整页盖住了，没有键盘出口会让人以为卡死。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const hasParallel = graph.waves.some((wave) => wave.length > 1);
  const edgeInto = (id: string) => graph.edges.filter((edge) => edge.to === id);

  return (
    <div className="collab-overlay" role="dialog" aria-modal="true" aria-label="协作工作流画布">
      <header className="collab-canvas-head">
        <span className="collab-canvas-title">
          <GitBranch size={16} />
          <b>协作工作流</b>
          <small>
            {graph.mode === "dynamic" ? "自动编排" : "固定流水线"}
            {graph.nodes.length ? ` · ${graph.nodes.length} 个 Agent 节点` : ""}
            {graph.edges.length ? ` · ${graph.edges.length} 条数据流转` : ""}
          </small>
        </span>
        <div className="collab-canvas-actions">
          <button type="button" className="cfg-quiet" onClick={onClose}>
            <X size={13} />
            关闭画布
          </button>
        </div>
      </header>

      {conversations.length > 0 && (
        <nav className="collab-conversations" aria-label="对话编号">
          <span className="collab-conversations-label">
            <MessageSquareText size={12} />
            对话编号
          </span>
          {conversations.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`collab-conversation ${item.id === selectedId ? "current" : ""}`}
              aria-current={item.id === selectedId ? "true" : undefined}
              onClick={() => onSelect?.(item.id)}
              title={item.label}
            >
              <b>对话 {item.index}</b>
              <small>{item.label}</small>
              <Status status={item.status} />
            </button>
          ))}
        </nav>
      )}

      <div className="collab-canvas-body">
        {loading && !graph.nodes.length && <p className="cfg-hint">正在读取这次对话的工作流…</p>}
        {error && (
          <p className="cfg-alert" role="alert">
            {error}
          </p>
        )}
        {!loading && !error && !graph.nodes.length && (
          <p className="collab-empty">这次对话还没有可画的协作节点。</p>
        )}

        {graph.task && (
          <div className="collab-canvas-task">
            <span className="eyebrow">本次任务</span>
            <p>{graph.task}</p>
          </div>
        )}

        {graph.traceReason && <p className="collab-canvas-note">{graph.traceReason}</p>}

        {graph.nodes.length > 0 && (
          <div className="collab-canvas-graph">
            {graph.waves.map((wave, index) => (
              <div className="collab-wave" key={wave.map((node) => node.id).join("+")}>
                {index > 0 &&
                  wave.map((node) =>
                    edgeInto(node.id).map((edge) => (
                      <div
                        className={`collab-link ${edge.kind} wide`}
                        key={`${edge.from}-${edge.to}`}
                        tabIndex={0}
                      >
                        <span className="collab-link-rule" />
                        <small>{edge.kind === "parallel" ? "并行汇入" : "串行"}</small>
                        <b className="collab-link-tools">
                          {edge.toolSummary ? `工具链路：${edge.toolSummary}` : "无工具调用"}
                        </b>
                        {edge.toolCalls.length > 0 && (
                          <div className="collab-hover" role="tooltip">
                            <div className="collab-hover-block">
                              <h5>这条链路上的工具调用</h5>
                              <ul>
                                {edge.toolCalls.map((call) => (
                                  <li key={call.id} className={call.status}>
                                    <b>{call.name}</b>
                                    <span>{call.status}</span>
                                    <pre>入参 {call.input}</pre>
                                    <pre>出参 {call.output}</pre>
                                    {call.error && <em>{call.error}</em>}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          </div>
                        )}
                      </div>
                    )),
                  )}
                <div className={`collab-row ${wave.length > 1 ? "parallel" : ""}`}>
                  {wave.map((node) => (
                    <AgentNodeCard key={node.id} node={node} withDetail />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        <p className="collab-canvas-legend">
          节点是 Agent 卡片，连线是它把结果交给下一个 Agent 的那一步。
          <span className="collab-mode">{hasParallel ? "含并行波次" : "串行流水线"}</span>
          <span>悬停节点看参数、Token 与分配到的任务；悬停连线看这条链路上调用了哪些工具。</span>
        </p>
      </div>
    </div>
  );
}

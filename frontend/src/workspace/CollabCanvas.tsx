/**
 * 全屏协作画布。
 *
 * 侧栏那张是「一眼看个大概」，这里把同一张图放大到能读细节：**悬停**节点看它的生效参数、
 * Token 消耗、分配到的任务与阶段产出，**悬停**连线上的工具链胶囊看这条链路调了哪些工具、
 * 入参出参是什么。节点还能拖着挪一挪，把挤在一起的连线拉开。
 *
 * 顶部按「对话编号」切换：会话里每一次提问各跑出一个工作流，编号沿用历史列表的「任务 N」
 * 口径（`doc/api.md` §5.18）。编号必须固定——用户会用「对话 3 那次」指代它，一旦编号
 * 随新对话漂移，这句话就没有指代对象了。
 *
 * 文案只留必要的那几处：标题、编号、任务原文一行、底部当前节点一行。画布自己的图例
 * 一律不写——连线是虚是实、节点是灰是蓝，图上已经看得见，再用一段话解释一遍只是噪声。
 *
 * 计划还没落盘时不画任何节点（ADR-034）：那时连「几步、派给谁」都还没有，摆出来的只会
 * 是写死的固定三步，而这次它可能压根不参与。宁可只报「正在规划」。
 */
import { useEffect } from "react";
import { GitBranch, LoaderCircle, X } from "lucide-react";
import { Status } from "../components/Status";
import { CollaborationCanvas, useElementSize } from "./GraphCanvas";
import { PLAN_SOURCE_HINT, PLAN_SOURCE_TEXT, planSourceKey } from "./collaboration";
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
  // 画布外层框量到的可用高度：行距据此在一屏里摊开
  const [frameRef, frame] = useElementSize<HTMLDivElement>(open);

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

  // 底部一行报「现在轮到谁」。三档依次退：正在跑的 → 下一个还没跑的 → 全跑完了。
  // 只用「最后一个 completed」当兜底会在等待态指错人：那正是「跑到哪了」最容易看错的时候。
  const running = graph.nodes.find((node) => node.status === "running");
  const waiting = graph.nodes.find(
    (node) => node.status === "pending" || node.status === "failed",
  );
  const lastDone = [...graph.nodes].reverse().find((node) => node.status === "completed");
  // 计划没落盘时整张图是空的，「跑到哪了」无从谈起——先如实说在规划。
  const line = graph.planning
    ? "正在规划：等待任务分配产出"
    : running
      ? `当前节点：${running.name}`
      : waiting
        ? `下一步：${waiting.name}`
        : lastDone
          ? `全部阶段已完成：${lastDone.name}`
          : "等待任务开始";

  // 画布角标只说「计划从哪来」这一件事：固定链是动态路径的退化情形，不是并列的
  // 第二种编排方式（ADR-037）。长解释挂 `title`，角标本身保持短。
  const planSource = planSourceKey({
    mode: graph.mode,
    planning: graph.planning,
    source: graph.planner?.source,
  });

  return (
    <div className="collab-overlay" role="dialog" aria-modal="true" aria-label="协作工作流画布">
      <header className="collab-canvas-head">
        <span className="collab-canvas-title">
          <GitBranch size={16} />
          <b>协作工作流</b>
          <small title={PLAN_SOURCE_HINT[planSource]}>{PLAN_SOURCE_TEXT[planSource]}</small>
        </span>
        <button type="button" className="cfg-quiet" onClick={onClose}>
          <X size={13} />
          关闭
        </button>
      </header>

      {conversations.length > 0 && (
        <nav className="collab-conversations" aria-label="对话编号">
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
              <Status status={item.status} />
            </button>
          ))}
        </nav>
      )}

      <div className="collab-canvas-body">
        {graph.task && (
          <p className="cv-task" title={graph.task}>
            {graph.task}
          </p>
        )}
        {graph.traceReason && <p className="cv-note">{graph.traceReason}</p>}
        {graph.planning && (
          <p className="cv-planning">
            <LoaderCircle size={13} className="spin" />
            正在规划：任务分配还没产出，画布等计划落盘后再画。
          </p>
        )}
        {loading && !graph.nodes.length && <p className="cfg-hint">正在读取这次对话的工作流…</p>}
        {error && (
          <p className="cfg-alert" role="alert">
            {error}
          </p>
        )}
        {!loading && !error && !graph.nodes.length && !graph.planning && (
          <p className="cv-empty">这次对话还没有可画的协作节点。</p>
        )}
        {graph.nodes.length > 0 && (
          <div className="cv-frame" ref={frameRef}>
            <CollaborationCanvas graph={graph} minHeight={frame.height} />
          </div>
        )}
      </div>

      <footer className="cv-terminal">
        <span>&gt; {line}</span>
        <span className="cv-terminal-mode">
          {graph.planning
            ? "链路未定"
            : graph.waves.some((wave) => wave.length > 1)
              ? "含并行波次"
              : "串行流水线"}
        </span>
      </footer>
    </div>
  );
}

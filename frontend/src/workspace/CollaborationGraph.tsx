/**
 * 侧栏的协作链路视图：一张紧凑画布。
 *
 * 与「Agent 执行台」的分工（`doc/api.md` §7）：执行台回答「这个 Agent 干到哪一步了」，
 * 画布回答「这次任务里谁跟谁、各自用什么跑的」。所以节点**点了不弹执行轨迹**——
 * 两块视图一旦都能点进同一份轨迹，就又混成一个了；要看轨迹去执行台卡片上点。
 *
 * 图上只留三类信息：节点（Agent 圆点 + 名字 + Token 徽标）、连线（灰虚线要走、
 * 蓝实线走通）、以及连线上的工具链胶囊。参数全表、Prompt、产出这些细节进全屏画布的
 * 悬停面板——侧栏的高度该留给「一眼看出走到哪了」。
 *
 * 画布本体在 `GraphCanvas.tsx`，这里只负责挂上「全屏画布」入口。
 */
import { CollaborationCanvas } from "./GraphCanvas";
import type { CollabGraph } from "./collaboration";
import "./workspace.css";

export function CollaborationGraph({
  graph,
  onExpand,
}: {
  graph: CollabGraph;
  /** 有它就渲染浮在画布右上角的「全屏画布」入口。 */
  onExpand?: () => void;
}) {
  if (!graph.nodes.length) {
    return <p className="cv-empty">任务开始后，这里会画出 Agent 之间的协作链路。</p>;
  }
  return <CollaborationCanvas graph={graph} dense onExpand={onExpand} />;
}

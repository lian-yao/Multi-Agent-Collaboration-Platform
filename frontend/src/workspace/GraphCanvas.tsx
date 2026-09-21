/**
 * 协作画布本体：把 `CollabGraph` 画成一张节点图。
 *
 * 侧栏与全屏共用同一份布局与同一套渲染，只换密度（`dense`）：侧栏窄，只回答
 * 「谁跟谁、走到哪了」；全屏宽，连线挂得出工具链路、悬停出详情。
 *
 * 画法照 [`Jasper-zh/Multi-Agent-Playground`](https://github.com/Jasper-zh/Multi-Agent-Playground)
 * 的 `GraphViewer.vue`：节点是绝对定位的圆形 div，边是**同一尺寸** SVG 里的三次贝塞尔
 * `<path>`；两端各自从圆心退到圆周上，箭头才不会被节点自己盖住。灰虚线 = 要走的边，
 * 蓝实线 = 已经走通的边——进度靠颜色区分，不靠文字说明。
 *
 * 两个不能省的约束：
 *
 * - **必须量宽度**。SVG 的 `viewBox` 要等于元素的像素尺寸，坐标系才和节点定位的
 *   `left/top` 是同一套；一旦靠百分比缩放，圆会变成椭圆、箭头会被拉歪。
 * - **量不到也要能画**。离屏冒烟与 SSR 里 `getBoundingClientRect()` 返回 0，
 *   此时退回默认宽度继续算布局——视图本体因此能被静态渲染直接断言。
 *
 * 事件一律用 CSS `:hover` / `:focus-within` 驱动，不引入 JS hover 状态：键盘能拿到
 * 同样的信息，冒烟也能断言这些文本确实渲染了。
 */
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Bot, CircleAlert, LoaderCircle, Maximize2, Pause, Wrench } from "lucide-react";
import { Status } from "../components/Status";
import type { CollabGraph, CollabNode, CollabToolCall } from "./collaboration";
import "./workspace.css";

/** 合成出来的首尾端子：任务从哪进来、结果从哪出去。 */
const START_ID = "__start";
const END_ID = "__end";

type Geometry = {
  /** 节点圆直径。 */
  size: number;
  /** 相邻两行的中心距。 */
  gap: number;
  /** 左右留白：最外侧节点不贴边。 */
  padX: number;
  padY: number;
  /** 底部留白要更大：名字与「模型 · Token」挂在圆下方，得给它们留出位置。 */
  padBottom: number;
  icon: number;
};

/** 侧栏：窄、矮，紧凑到一屏放得下 5 行。 */
const DENSE: Geometry = { size: 30, gap: 72, padX: 30, padY: 26, padBottom: 54, icon: 12 };
/** 全屏：宽、松，连线上的工具链才有地方落脚。 */
const WIDE: Geometry = { size: 42, gap: 96, padX: 96, padY: 44, padBottom: 74, icon: 16 };

export type PlacedNode = {
  id: string;
  kind: "agent" | "start" | "end";
  /** 圆心（画布坐标系）。 */
  x: number;
  y: number;
  size: number;
  /** 只有 agent 节点才有原始模型（端子为 null）。 */
  node: CollabNode | null;
  label: string;
  /** `883 tok`；空串 = 没有采样，不是「消耗为 0」。 */
  token: string;
  status: string;
  /** 已经跑到过（含执行中与失败）。 */
  visited: boolean;
};

export type PlacedEdge = {
  key: string;
  /** 圆的半径 + 一点余量，箭头落在这里。 */
  d: string;
  /** 上游已经把结果交出去了。 */
  active: boolean;
  /**
   * 工具链胶囊的落点（曲线上的一个点，**不是**几何中点）。
   *
   * 取「上游标签之下、下游圆之上」那条空带的中点，否则胶囊会被上游节点的
   * 「模型 · Token」标签压住——标签有底色，压住之后像是连线把工具信息弄丢了。
   */
  mid: { x: number; y: number };
  toolSummary: string;
  toolCalls: CollabToolCall[];
  /** `A → B`，给可访问名用。 */
  title: string;
};

export type PlacedGroup = {
  key: string;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
  tag: string;
};

export type CollabLayout = {
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  groups: PlacedGroup[];
};

/* -------------------------------------------------------------------------- */
/* 布局                                                                        */
/* -------------------------------------------------------------------------- */

/** 从圆周的上下极点进出：切线是纵向的，箭头才不会插进圆里、也不会斜着扎上来。 */
function poleOf(node: PlacedNode, downward: boolean) {
  const r = node.size / 2 + 2;
  return { x: node.x, y: downward ? node.y + r : node.y - r };
}

/**
 * 三次贝塞尔的两个控制点：一左一右**纵向**拉开，曲线从节点正下方出去、从对方正上方进来。
 *
 * 不按「横向为主就给横向控制点」那套来（参考实现是这么写的）：本项目是一张自上而下
 * 的依赖图，边一律跨行，横向控制点会让分叉到左右两端的边拉出一个很夸张的大括号，
 * 而纵向切线的曲线才读得出「上一波交给下一波」。
 */
function controlPoints(from: PlacedNode, to: PlacedNode) {
  const downward = to.y >= from.y;
  const start = poleOf(from, downward);
  const end = poleOf(to, !downward);
  const dy = to.y - from.y;
  const bend = Math.max(28, Math.abs(dy) * 0.5) * (Math.sign(dy) || 1);
  return { start, cp1: { x: start.x, y: start.y + bend }, cp2: { x: end.x, y: end.y - bend }, end };
}

type Curve = ReturnType<typeof controlPoints>;

/** 曲线上 t 处的点。 */
function at(curve: Curve, t: number) {
  const u = 1 - t;
  const { start, cp1, cp2, end } = curve;
  return {
    x: u * u * u * start.x + 3 * u * u * t * cp1.x + 3 * u * t * t * cp2.x + t * t * t * end.x,
    y: u * u * u * start.y + 3 * u * u * t * cp1.y + 3 * u * t * t * cp2.y + t * t * t * end.y,
  };
}

function curvePath(curve: Curve) {
  const { start, cp1, cp2, end } = curve;
  return `M ${start.x} ${start.y} C ${cp1.x} ${cp1.y}, ${cp2.x} ${cp2.y}, ${end.x} ${end.y}`;
}

/**
 * 工具链胶囊的落点：**上游那一行的标签之下、下游圆之上**的那条空带。
 *
 * 不能直接用 t=0.5 的中点——圆下面挂着名字与「模型 · Token」，中点正好压在这两行字上，
 * 而标签是带底色的（连线从字缝里透出来很难看，所以必须给标签加底色），
 * 于是胶囊被压在标签底下，看起来像「连线把工具信息丢了」。
 */
function chipAnchor(curve: Curve, from: PlacedNode, to: PlacedNode, dense: boolean) {
  const top = from.y + from.size / 2 + (dense ? 30 : 34);
  const bottom = to.y - to.size / 2 - 4;
  if (top >= bottom) return at(curve, 0.5);
  const target = (top + bottom) / 2;
  let best = { t: 0.5, point: at(curve, 0.5) };
  for (let step = 0; step <= 24; step += 1) {
    const t = 0.25 + (0.7 * step) / 24;
    const point = at(curve, t);
    if (Math.abs(point.y - target) < Math.abs(best.point.y - target)) best = { t, point };
  }
  return best.point;
}

/** 采样里挑一行当节点下的徽标：优先总量，没有就取第一条 Token 采样。 */
function tokenBadge(node: CollabNode): string {
  const rows = node.usage.filter((row) => row.label.includes("Token"));
  if (!rows.length) return "";
  const row = rows.find((item) => item.label.includes("总")) ?? rows[0];
  const value = row.values[row.values.length - 1];
  return value ? `${value} tok` : "";
}

/**
 * 排布：首端子 → 每个波次一行 → 末端子。同波多个节点即并行，横向摊开。
 *
 * 高度由行数算出，但可以给一个 `minHeight` 把行距撑开：全屏下单行间距不够看，
 * 在一屏里摊开才像一张图。**只向上取整、不向下压缩**——信息密度可以更松，
 * 但不能为了让所有行塞进视口而把行距压到标签互相覆盖（那时宁可让外层滚）。
 */
export function layoutCollaboration(
  graph: CollabGraph,
  width: number,
  dense = false,
  minHeight = 0,
): CollabLayout {
  const m = dense ? DENSE : WIDE;
  const waves = graph.waves.map((wave) => wave.filter(Boolean)).filter((wave) => wave.length);
  const rows = waves.length + 2;
  const w = Math.max(width > 0 ? width : dense ? 248 : 720, dense ? 200 : 320);
  const h = Math.max(m.padY + m.padBottom + (rows - 1) * m.gap, minHeight);
  const yOf = (row: number) =>
    rows > 1 ? m.padY + ((h - m.padY - m.padBottom) * row) / (rows - 1) : h / 2;
  const usable = Math.max(0, w - m.padX * 2);
  const xOf = (index: number, count: number) =>
    count > 1 ? m.padX + (usable * index) / (count - 1) : w / 2;

  const nodes: PlacedNode[] = [];
  const groups: PlacedGroup[] = [];

  const start: PlacedNode = {
    id: START_ID,
    kind: "start",
    x: w / 2,
    y: yOf(0),
    size: m.size,
    node: null,
    label: "任务",
    token: "",
    status: "completed",
    visited: true,
  };
  const end: PlacedNode = {
    id: END_ID,
    kind: "end",
    x: w / 2,
    y: yOf(rows - 1),
    size: m.size,
    node: null,
    label: "交付",
    token: "",
    status: "pending",
    visited: false,
  };
  nodes.push(start);

  waves.forEach((wave, waveIndex) => {
    const y = yOf(waveIndex + 1);
    wave.forEach((node, index) => {
      nodes.push({
        id: node.id,
        kind: "agent",
        x: xOf(index, wave.length),
        y,
        size: m.size,
        node,
        label: node.name,
        token: tokenBadge(node),
        status: node.status,
        visited: node.status !== "pending",
      });
    });
    // 同波多节点 = 并行：拿一个框把它们圈起来，并行关系就是画出来的而不是写出来的。
    // 框要比「圆心 + 半径」高一些、并往下偏一点——圆下面还挂着名字与 Token，
    // 框正好卡在圆的腰上会把标签切一半。
    if (wave.length > 1) {
      groups.push({
        key: `group-${waveIndex}`,
        x: w / 2,
        y: y + (dense ? 8 : 12),
        width: Math.min(usable + m.padX, w - 8),
        height: m.size + (dense ? 50 : 78),
        label: "并行协作区",
        tag: "Parity Mesh",
      });
    }
  });

  // 末端子：最后一个走到的节点把结果交出去。端子**最后入列**，渲染层叠顺序才顺——
  // 它是终点，不该压在上面的 Agent 节点之上。
  const downstream = new Set(graph.edges.map((edge) => edge.from));
  const leaves = nodes.filter(
    (item) => item.kind === "agent" && !downstream.has(item.id),
  );
  end.status = leaves.length && leaves.every((leaf) => leaf.status === "completed") ? "completed" : "pending";
  end.visited = leaves.some((leaf) => leaf.status === "completed");
  nodes.push(end);

  const byId = new Map(nodes.map((node) => [node.id, node]));

  const edges: PlacedEdge[] = [];
  /**
   * `active` = 「这条边已经走通了」。
   *
   * 三个位置的判据各不相同，所以显式传进来而不是在函数里猜：任务端子到根步骤看
   * **下游**有没有开始跑，步骤之间看**上游**有没有跑完，末端子看最后一步有没有跑完。
   * 用同一个条件套三处，就会出现「还没开始的任务已经连出一条蓝线」。
   */
  const push = (
    from: PlacedNode | undefined,
    to: PlacedNode | undefined,
    active: boolean,
    summary: string,
    calls: CollabToolCall[],
  ) => {
    if (!from || !to) return;
    const curve = controlPoints(from, to);
    edges.push({
      key: `${from.id}->${to.id}`,
      d: curvePath(curve),
      active,
      mid: chipAnchor(curve, from, to, dense),
      toolSummary: summary,
      toolCalls: calls,
      title: `${from.label} → ${to.label}`,
    });
  };

  // 根步骤左边接任务端子，每个末端接交付端子；中间按依赖连
  const children = new Set(graph.edges.map((edge) => edge.to));
  for (const item of nodes) {
    if (item.kind !== "agent" || children.has(item.id)) continue;
    push(start, item, item.status !== "pending", "", []);
  }
  for (const edge of graph.edges) {
    const source = byId.get(edge.from);
    push(source, byId.get(edge.to), source?.status === "completed", edge.toolSummary, edge.toolCalls);
  }
  for (const leaf of leaves) push(leaf, end, leaf.status === "completed", "", []);

  return { width: w, height: h, nodes, edges, groups };
}

/* -------------------------------------------------------------------------- */
/* 渲染                                                                        */
/* -------------------------------------------------------------------------- */

/** 量元素的像素尺寸；量不到就保持 0，由布局退回默认值。 */
export function useElementSize<T extends HTMLElement>(enabled = true) {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (!enabled) return;
    const element = ref.current;
    if (!element) return;
    const read = () => {
      const rect = element.getBoundingClientRect();
      setSize((prev) =>
        prev.width === rect.width && prev.height === rect.height
          ? prev
          : { width: rect.width, height: rect.height },
      );
    };
    read();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", read);
      return () => window.removeEventListener("resize", read);
    }
    const observer = new ResizeObserver(read);
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled]);
  return [ref, size] as const;
}

function NodeGlyph({ node, size }: { node: PlacedNode; size: number }) {
  if (node.kind !== "agent") return <span className="cv-dot" />;
  if (node.status === "failed") return <CircleAlert size={size} />;
  if (node.status === "paused") return <Pause size={size} />;
  if (node.status === "running") return <LoaderCircle size={size} className="spin" />;
  return <Bot size={size} />;
}

/**
 * 悬停详情：参数全表 / Token / 分配到的任务 / 产出 / 工具明细。
 *
 * 侧栏（`compact`）只留前两块。理由不是「放不下」而是**不该放**：那一列要回答的是
 * 「这个 Agent 用什么跑的」，Prompt 与产出属于执行轨迹，去执行台看；两处都给，
 * 就又回到「侧栏和执行台说的是同一件事」。
 */
function NodeDetail({ node, compact = false }: { node: CollabNode; compact?: boolean }) {
  return (
    <div className="cv-pop cv-pop-node" role="tooltip">
      <header className="cv-pop-head">
        <b>{node.name}</b>
        <Status status={node.status} />
      </header>
      <div className="cv-pop-block">
        <h5>生效参数</h5>
        <dl>
          {node.params.map((param) => (
            <div key={param.key} className={param.overridden ? "override" : ""}>
              <dt>
                {param.label}
                {param.overridden && <i>显式覆盖</i>}
              </dt>
              <dd>{param.value}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="cv-pop-block">
        <h5>Token 消耗</h5>
        {node.usage.length ? (
          <dl>
            {node.usage.map((row) => (
              <div key={row.label}>
                <dt>{row.label}</dt>
                <dd>{row.values.join(" / ")}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="cv-pop-note">没有采样记录。缺少 Token 是「没采到」，不是消耗为 0。</p>
        )}
      </div>
      {!compact && (
        <>
          <div className="cv-pop-block">
            <h5>{node.promptFrom ? `分配到的任务（来自 ${node.promptFrom}）` : "分配到的任务"}</h5>
            <pre>{node.prompt ?? "（根节点：收到的是用户提出的原始任务）"}</pre>
          </div>
          <div className="cv-pop-block">
            <h5>阶段产出{node.truncated && <i>已截断</i>}</h5>
            <pre>{node.output || "（尚无产出）"}</pre>
          </div>
          {node.toolCalls.length > 0 && (
            <div className="cv-pop-block">
              <h5>本阶段工具调用</h5>
              <ul>
                {node.toolCalls.map((call) => (
                  <li key={call.id} className={call.status}>
                    <b>{call.name}</b>
                    <span>{call.status}</span>
                    {call.error && <em>{call.error}</em>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * 连线上的工具链胶囊。
 *
 * 挂在上游那一段而不是节点里：读者要问的是「这条数据是怎么被加工出来的」，
 * 答案属于交接动作本身。没有工具就不画胶囊——空标签只会让图更吵。
 */
function EdgeChip({ edge }: { edge: PlacedEdge }) {
  return (
    <span className="cv-edge-chip" tabIndex={0} aria-label={`${edge.title} 的工具链路`}>
      <Wrench size={9} />
      {edge.toolSummary}
      <div className="cv-pop cv-pop-edge" role="tooltip">
        <header className="cv-pop-head">
          <b>{edge.title}</b>
          <span>工具调用</span>
        </header>
        <div className="cv-pop-block">
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
    </span>
  );
}

export function CollaborationCanvas({
  graph,
  dense = false,
  draggable = false,
  minHeight = 0,
  onExpand,
}: {
  graph: CollabGraph;
  /** 侧栏紧凑态：节点更小、行距更密、连线不带胶囊。 */
  dense?: boolean;
  /** 全屏画布拿节点拖一拖，把挤在一起的边拉开。 */
  draggable?: boolean;
  /**
   * 外层给的可用高度：行距据此撑开，让图在一屏里摊平。
   *
   * 由**外层**量好再传进来，而不是画布自己往上找父元素——侧栏的父容器是内容撑高的，
   * 画布再去撑高父容器就成了互相追着长。
   */
  minHeight?: number;
  /** 有它就渲染一枚浮在画布右上角的「全屏画布」入口。 */
  onExpand?: () => void;
}) {
  const [ref, size] = useElementSize<HTMLDivElement>();
  const [offsets, setOffsets] = useState<Record<string, { x: number; y: number }>>({});
  const drag = useRef<{ id: string; startX: number; startY: number; baseX: number; baseY: number } | null>(null);

  const layout = layoutCollaboration(graph, size.width, dense, minHeight);
  const { nodes, edges, groups, width: w, height: h } = layout;

  // 换了一条工作流就把拖动位移丢掉：否则新图的节点会顶着上一张图的偏移跑偏。
  const nodeKey = graph.nodes.map((node) => node.id).join("|");
  useEffect(() => {
    setOffsets({});
  }, [nodeKey]);

  useEffect(() => {
    if (!draggable) return;
    const onMove = (event: PointerEvent) => {
      const active = drag.current;
      const element = ref.current;
      if (!active || !element) return;
      const rect = element.getBoundingClientRect();
      setOffsets((prev) => ({
        ...prev,
        [active.id]: {
          x: active.baseX + (event.clientX - rect.left - active.startX),
          y: active.baseY + (event.clientY - rect.top - active.startY),
        },
      }));
    };
    const onUp = () => {
      drag.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [draggable, ref]);

  const placed = nodes.map((node) => {
    const offset = offsets[node.id];
    return offset ? { ...node, x: node.x + offset.x, y: node.y + offset.y } : node;
  });
  const byId = new Map(placed.map((node) => [node.id, node]));

  // 拖动后连线要跟着走：两端只要有一个被挪过，就用新圆心重算这条边
  const moved = edges.map((edge) => {
    const [fromId, toId] = edge.key.split("->");
    const from = byId.get(fromId);
    const to = byId.get(toId);
    if (!from || !to) return edge;
    if (!offsets[fromId] && !offsets[toId]) return edge;
    const curve = controlPoints(from, to);
    return {
      ...edge,
      d: curvePath(curve),
      mid: chipAnchor(curve, from, to, dense),
    };
  });

  const marker = dense ? "cvArrowDense" : "cvArrowWide";
  // 箭头尺寸按模式给死（`markerUnits="userSpaceOnUse"`）：默认的 `strokeWidth` 单位会
  // 让箭头跟着线宽一起放大，30px 的圆上顶着一枚 15px 的三角，节点会被箭头吃掉。
  const arrow = dense ? 7.5 : 9.5;
  const popWidth = 264;
  const half = popWidth / 2;
  const clampX = (x: number) =>
    Math.min(Math.max(x, half + 6), Math.max(half + 6, w - half - 6));
  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>, node: PlacedNode) => {
    if (!draggable || node.kind !== "agent" || !ref.current) return;
    event.preventDefault();
    const rect = ref.current.getBoundingClientRect();
    const offset = offsets[node.id] ?? { x: 0, y: 0 };
    drag.current = {
      id: node.id,
      startX: event.clientX - rect.left,
      startY: event.clientY - rect.top,
      baseX: offset.x,
      baseY: offset.y,
    };
  };

  return (
    <div className={`cv-canvas ${dense ? "dense" : "wide"}`} ref={ref} style={{ height: h }}>
      <svg className="cv-svg cv-svg-base" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <marker
            id={marker}
            viewBox="0 0 10 10"
            refX="7.8"
            refY="5"
            markerWidth={arrow}
            markerHeight={arrow}
            markerUnits="userSpaceOnUse"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#cbd5e1" />
          </marker>
          <marker
            id={`${marker}Active`}
            viewBox="0 0 10 10"
            refX="7.8"
            refY="5"
            markerWidth={arrow}
            markerHeight={arrow}
            markerUnits="userSpaceOnUse"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#3b82f6" />
          </marker>
        </defs>
        {moved.map((edge) => (
          <path key={`base-${edge.key}`} d={edge.d} className="cv-edge" markerEnd={`url(#${marker})`} />
        ))}
        {moved
          .filter((edge) => edge.active)
          .map((edge) => (
            <path
              key={`live-${edge.key}`}
              d={edge.d}
              className="cv-edge active"
              markerEnd={`url(#${marker}Active)`}
            />
          ))}
      </svg>

      {groups.map((group) => (
        <div
          key={group.key}
          className="cv-group"
          style={{
            left: group.x,
            top: group.y,
            width: group.width,
            height: group.height,
            marginLeft: -group.width / 2,
            marginTop: -group.height / 2,
          }}
        >
          <span className="cv-group-head">
            <b>{group.label}</b>
            <i>{group.tag}</i>
          </span>
        </div>
      ))}

      {!dense && (
        <div className="cv-edge-layer">
          {moved
            .filter((edge) => edge.toolSummary)
            .map((edge) => (
              <span
                key={edge.key}
                className="cv-edge-slot"
                style={{
                  left: edge.mid.x,
                  top: edge.mid.y,
                  marginLeft: clampX(edge.mid.x) - edge.mid.x,
                }}
              >
                <EdgeChip edge={edge} />
              </span>
            ))}
        </div>
      )}

      {placed.map((node) => (
        <div
          key={node.id}
          className={`cv-node kind-${node.kind} ${node.status}${node.visited ? " visited" : ""}`}
          style={{
            left: node.x,
            top: node.y,
            width: node.size,
            height: node.size,
            marginLeft: -node.size / 2,
            marginTop: -node.size / 2,
          }}
          tabIndex={node.kind === "agent" ? 0 : -1}
          aria-label={
            node.kind === "agent"
              ? `${node.label}：${node.status}${node.token ? `，${node.token}` : ""}`
              : node.label
          }
          onPointerDown={(event) => onPointerDown(event, node)}
        >
          <NodeGlyph node={node} size={dense ? 12 : 16} />
          {node.status === "running" && <span className="cv-ring" />}
          <span className="cv-name">{node.label}</span>
          {/* 圆里只放得下一个图标，所以「用什么跑的、花了多少」写在圆下方一行 */}
          {node.node && (
            <span className="cv-meta">
              {[node.node.model, node.token].filter(Boolean).join(" · ")}
            </span>
          )}
          {node.node && (
            <div
              className="cv-pop-slot"
              style={{
                left: clampX(node.x) - node.x + node.size / 2,
                ...(node.y > h * 0.62
                  ? { bottom: node.size + 8 }
                  : { top: node.size + 8 }),
              }}
            >
              <NodeDetail node={node.node} compact={dense} />
            </div>
          )}
        </div>
      ))}

      {onExpand && (
        <button type="button" className="cv-expand" onClick={onExpand}>
          <Maximize2 size={11} />
          全屏画布
        </button>
      )}
    </div>
  );
}

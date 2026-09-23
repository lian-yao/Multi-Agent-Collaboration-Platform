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
 *
 * **节点不可拖**：这是一张「过程可视化」，位置本身就在表达流程顺序，不是可编辑的画板。
 * 能拖只会让人以为拖动能改变什么，而它什么都不会改变。
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
import {
  CircleAlert,
  GitMerge,
  ListTree,
  LoaderCircle,
  Maximize2,
  Pause,
  ShieldCheck,
  Target,
  Wrench,
} from "lucide-react";
import { Status } from "../components/Status";
import { AgentGlyph } from "../components/AgentGlyph";
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
  /** 子任务节点还是平台节点（意图 / 编排 / 合成 / 校验）：只影响视觉区分。 */
  variant: "worker" | "platform";
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
    variant: "platform",
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
    variant: "platform",
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
        variant: node.kind === "worker" ? "worker" : "platform",
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

/**
 * 圆面里的图形。**状态优先于角色**：执行中 / 失败 / 暂停是人要立刻看见的信号，
 * 此时不画角色图标。停在待命或已完成时画角色图标（与配置页同一套解析，
 * 见 `components/AgentGlyph.tsx`），不再一律画机器人。
 */
function NodeGlyph({ node, size }: { node: PlacedNode; size: number }) {
  if (node.kind !== "agent") return <span className="cv-dot" />;
  if (node.status === "failed") return <CircleAlert size={size} />;
  if (node.status === "paused") return <Pause size={size} />;
  if (node.status === "running") return <LoaderCircle size={size} className="spin" />;
  // 平台节点（意图 / 编排 / 合成 / 校验）不是 Agent：不套角色图标，
  // 否则画布会给出「这里是一个它在跑的角色」这种错误暗示。
  if (node.node && node.node.kind !== "worker") return <PlatformGlyph kind={node.node.kind} size={size} />;
  return <AgentGlyph role={node.node?.agentId ?? ""} name={node.label} size={size} />;
}

/**
 * 平台节点的图标：意图（靶心）、编排（树）、合成（归并）、校验（盾）。
 *
 * 与 Agent 角色图标分开是刻意的：这四个节点**不是**使用者在「团队」里配置的 Agent，
 * 给它们套角色图标会让人以为「平台又派了一个角色来干活」。
 */
function PlatformGlyph({ kind, size }: { kind: string; size: number }) {
  if (kind === "intent") return <Target size={size} />;
  if (kind === "plan") return <ListTree size={size} />;
  if (kind === "synthesize") return <GitMerge size={size} />;
  if (kind === "validate") return <ShieldCheck size={size} />;
  return <span className="cv-dot" />;
}

/**
 * 悬停详情：**接到什么、交出什么**在前，**用什么参数、花多少**在后。
 *
 * 排序按读图时的疑问顺序，不按数据现成的顺序：先问「它拿到了什么任务、产出了什么」，
 * 再问「中途调了什么工具」，最后才是「用什么参数跑的、花了多少 Token」。
 * 反过来排（参数 / Token 打头）会把最该看的产出压到滚动区下面——面板再大也会被读完就关。
 *
 * **只在全屏画布出现**。侧栏那一列要回答的是「这个 Agent 用什么跑的」，`模型 · Token`
 * 已经写在圆下方；侧栏再挂一份浮层，等于把执行轨迹搬回侧栏，正好是 ADR-018 拆开的同一件事。
 */
function NodeDetail({ node }: { node: CollabNode }) {
  return (
    <div className="cv-pop cv-pop-node" role="tooltip">
      <header className="cv-pop-head">
        <b>{node.name}</b>
        <Status status={node.status} />
      </header>
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
      {node.params.length > 0 && (
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
      )}
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
    </div>
  );
}

/**
 * 连线上的工具链胶囊。
 *
 * 挂在上游那一段而不是节点里：读者要问的是「这条数据是怎么被加工出来的」，
 * 答案属于交接动作本身。没有工具就不画胶囊——空标签只会让图更吵。
 *
 * 浮窗与节点浮窗同一套规矩：**贴侧面**。胶囊落在画布中线上，向上弹会正好盖住上游那一串
 * 节点——而链路正是这张图要给人看的东西。竖直方向靠 `--cv-pop-fit` 夹：面竖直居中于胶囊，
 * 所以面高只要不超过「胶囊到画布上下沿距离的两倍」，就必然不溢出（不必去量面的实际高度）。
 *
 * 这里给的只是**可容高度**这一个约束，不是面高上限——上限（520px）留在样式表里，由 CSS
 * 的 `min()` 把两者取小。直接往内联 `max-height` 上写数值会静默顶掉那条设计上限。
 */
function EdgeChip({
  edge,
  side,
  fitHeight,
}: {
  edge: PlacedEdge;
  /** 胶囊落在画布哪半边决定翻向：右半边就往左弹，免得顶出画布。 */
  side: "left" | "right";
  /** 由胶囊在画布里的竖直位置算出的可容高度（px），见上面的规矩。 */
  fitHeight: number;
}) {
  return (
    <span className="cv-edge-chip" tabIndex={0} aria-label={`${edge.title} 的工具链路`}>
      <Wrench size={9} />
      {edge.toolSummary}
      <div
        className={`cv-pop cv-pop-edge is-${side}`}
        style={{ "--cv-pop-fit": `${fitHeight}px` } as CSSProperties}
        role="tooltip"
      >
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
  minHeight = 0,
  onExpand,
}: {
  graph: CollabGraph;
  /**
   * 侧栏紧凑态：节点更小、行距更密、不画连线胶囊、**不给悬停浮层**。
   *
   * 浮层只在全屏给：侧栏要回答的是「这个 Agent 用什么跑的」，圆下方的 `模型 · Token`
   * 就是答案；再挂一份带任务与产出的浮层，等于把执行轨迹搬回侧栏（ADR-018）。
   */
  dense?: boolean;
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
  const { nodes, edges, groups, width: w, height: h } = layoutCollaboration(
    graph,
    size.width,
    dense,
    minHeight,
  );

  const marker = dense ? "cvArrowDense" : "cvArrowWide";
  // 箭头尺寸按模式给死（`markerUnits="userSpaceOnUse"`）：默认的 `strokeWidth` 单位会
  // 让箭头跟着线宽一起放大，30px 的圆上顶着一枚 15px 的三角，节点会被箭头吃掉。
  const arrow = dense ? 7.5 : 9.5;

  /**
   * 悬停面板贴节点的**侧面**，不挂正下方。
   *
   * 挂下方会直接压住下一段链路——而链路正是这张图要给人看的东西。左右两侧在竖直方向上
   * 是空的，所以面板挪到侧面；靠右的节点翻到左侧，免得顶出画布。
   */
  const popSide = (node: PlacedNode) => (node.x > w * 0.58 ? "left" : "right");
  /** 面板高度上限，用来把竖直位置夹进画布内（CSS 的 `max-height` 是同一个数）。 */
  const POP_H = 520;
  const popTop = (node: PlacedNode) => {
    const pad = POP_H / 2 + 8;
    const center = Math.min(Math.max(node.y, pad), Math.max(pad, h - POP_H / 2 - 8));
    // 槽位相对节点盒定位，节点中心在 `size / 2`；外侧再用 `translateY(-50%)` 把面板居中。
    return center - node.y + node.size / 2;
  };

  /** 工具胶囊按最宽一档（`calculator ×3、web_search ×2`）留半宽，贴边时把它拉回画布内。 */
  const chipHalf = 84;
  const clampChip = (x: number) =>
    Math.min(Math.max(x, chipHalf + 4), Math.max(chipHalf + 4, w - chipHalf - 4));

  return (
    <div className={`cv-canvas ${dense ? "dense" : "wide"}`} ref={ref} style={{ height: h }}>
      {/*
        「部分完成」必须在画布上说出来（ADR-038 §4）：终态可能仍是 completed，
        但结果是部分的。只在正文里写一句、画布上不体现，会让「已完成的绿点」误导人。
      */}
      {graph.partial && (
        <div className="cv-partial" role="status">
          <CircleAlert size={dense ? 11 : 13} />
          <b>部分子任务未完成</b>
          <span>
            {graph.failedSteps.length
              ? `失败：${graph.failedSteps.join("、")}`
              : "有子任务被跳过"}
            {graph.skippedSteps.length ? `；跳过：${graph.skippedSteps.join("、")}` : ""}
          </span>
        </div>
      )}
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
        {edges.map((edge) => (
          <path key={`base-${edge.key}`} d={edge.d} className="cv-edge" markerEnd={`url(#${marker})`} />
        ))}
        {edges
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
          {edges
            .filter((edge) => edge.toolSummary)
            .map((edge) => (
              <span
                key={edge.key}
                className="cv-edge-slot"
                style={{
                  left: edge.mid.x,
                  top: edge.mid.y,
                  marginLeft: clampChip(edge.mid.x) - edge.mid.x,
                }}
              >
                <EdgeChip
                  edge={edge}
                  side={edge.mid.x > w / 2 ? "left" : "right"}
                  fitHeight={Math.max(180, 2 * Math.min(edge.mid.y, h - edge.mid.y) - 20)}
                />
              </span>
            ))}
        </div>
      )}

      {nodes.map((node) => (
        <div
          key={node.id}
          className={`cv-node kind-${node.kind} variant-${node.variant} ${node.status}${
            node.visited ? " visited" : ""
          }`}
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
          {/* 侧栏不挂浮层：它只回答「用什么跑的」，圆下方那行就是答案（见 `dense` 的注释）。 */}
          {!dense && node.node && (
            <div className={`cv-pop-slot is-${popSide(node)}`} style={{ top: popTop(node) }}>
              <NodeDetail node={node.node} />
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

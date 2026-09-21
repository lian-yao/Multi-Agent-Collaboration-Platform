/**
 * 协作工作流视图。
 *
 * 与「Agent 执行台」的分工（`doc/api.md` §7）：
 *
 * - **执行台卡片**回答「这个 Agent 干到哪一步了」——进度与轨迹，点开看它的执行链路；
 * - **这里的协作卡片**回答「这个 Agent 是用什么跑的吗」——生效模型、参数、Token 消耗，
 *   以及它和上下游之间流过什么工具。所以它**不是**执行台的入口，点了也不跳弹窗：
 *   两块视图一旦都能点进同一份轨迹，就又混成一个了。
 *
 * 连线记录**上游**这一步动过的工具：`A ——calculator ×1——> B` 读作「A 用计算器算出结果，
 * 交给 B」。把工具挂在边上而不是节点里，是因为用户想看的正是「这条数据是怎么被加工出来的」。
 *
 * 只吃显式 props、不自己拉数据：容器组件靠 effect 拉数据，静态渲染拿不到内容，
 * 视图本体必须能被离屏冒烟直接挂载（见 `frontend/rendercheck`）。
 */
import {
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  LoaderCircle,
  Maximize2,
  Pause,
  Wrench,
} from "lucide-react";
import { Status } from "../components/Status";
import type { CollabGraph, CollabNode } from "./collaboration";
import "./workspace.css";

const NODE_ICON: Record<string, typeof Check> = {
  completed: Check,
  running: LoaderCircle,
  paused: Pause,
  failed: CircleAlert,
};

/**
 * 卡片上只摊开温度；其余参数只在「被显式覆盖」时才露头。
 *
 * 按 `key` 过滤而不是按中文标签——标签是给人看的文案，改一次措辞就会让筛选默默失效，
 * 而参数会被漏掉这件事在界面上完全看不出来。
 */
const INLINE_PARAM_KEYS = new Set(["temperature"]);

function NodeIcon({ status, size = 13 }: { status: string; size?: number }) {
  const Icon = NODE_ICON[status] ?? Clock3;
  return <Icon size={size} className={status === "running" ? "spin" : undefined} />;
}

/**
 * Agent 卡片。侧栏紧凑态与全屏画布共用同一份渲染——密度不同但内容同源，
 * 否则两处会各自解释一遍「哪个字段标成覆盖」。
 */
export function AgentNodeCard({
  node,
  withDetail = false,
}: {
  node: CollabNode;
  /** 渲染悬停详情面板（参数全表 / 分配到的任务 / 产出 / 工具明细）。 */
  withDetail?: boolean;
}) {
  const tokens = node.usage.filter((row) => row.label.includes("Token"));
  const inline = node.params.filter(
    (param) => INLINE_PARAM_KEYS.has(param.key) || param.overridden,
  );
  return (
    <article
      className={`collab-card ${node.status}${node.parallel ? " parallel" : ""}`}
      tabIndex={0}
      aria-label={`${node.name} 的协作信息`}
    >
      <header className="collab-card-head">
        <span className={`collab-card-mark ${node.status}`}>
          <NodeIcon status={node.status} />
        </span>
        <span className="collab-card-title">
          <b>{node.name}</b>
          <small>
            {node.stageLabel} · 第 {node.order} 步
            {node.parallel && " · 并行"}
          </small>
        </span>
        <Status status={node.status} />
      </header>

      <p className="collab-card-model">
        <span>{node.model}</span>
        {node.provider && <em>{node.provider}</em>}
      </p>

      {inline.length > 0 && (
        <p className="collab-card-chips">
          {inline.map((param) => (
            <span
              key={param.label}
              className={param.overridden ? "collab-chip override" : "collab-chip"}
              title={param.overridden ? "该字段来自角色的显式覆盖" : "该字段走默认路由"}
            >
              {param.label} {param.value}
              {param.overridden && <i>*</i>}
            </span>
          ))}
        </p>
      )}

      <p className="collab-card-usage">
        {tokens.length ? (
          tokens.map((row) => (
            <span key={row.label}>
              <small>{row.label}</small>
              <b>{row.values.join(" / ")}</b>
            </span>
          ))
        ) : (
          <span className="collab-card-nodata">暂无用量采样</span>
        )}
      </p>

      {node.toolCalls.length > 0 && (
        <p className="collab-card-tools">
          <Wrench size={11} />
          {node.toolCalls.length} 次工具调用
        </p>
      )}

      {node.traceReason && <p className="collab-card-note">{node.traceReason}</p>}

      {withDetail && (
        <div className="collab-hover" role="tooltip">
          <div className="collab-hover-block">
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
          <div className="collab-hover-block">
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
              <p className="collab-hover-note">
                没有采样记录。缺少 Token 是「没采到」，不是消耗为 0。
              </p>
            )}
          </div>
          <div className="collab-hover-block">
            <h5>
              {node.promptFrom ? `分配到的任务（来自 ${node.promptFrom}）` : "分配到的任务"}
            </h5>
            <pre>{node.prompt ?? "（根节点：收到的是用户提出的原始任务）"}</pre>
          </div>
          <div className="collab-hover-block">
            <h5>阶段产出{node.truncated && <i>已截断</i>}</h5>
            <pre>{node.output || "（尚无产出）"}</pre>
          </div>
          {node.toolCalls.length > 0 && (
            <div className="collab-hover-block">
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
        </div>
      )}
    </article>
  );
}

/** 一条连线：串/并行 + 上游这一步的工具链路。 */
function EdgeLine({
  kind,
  toolSummary,
  withDetail = false,
  toolCalls = [],
}: {
  kind: "serial" | "parallel";
  toolSummary: string;
  withDetail?: boolean;
  toolCalls?: CollabGraph["edges"][number]["toolCalls"];
}) {
  return (
    <div className={`collab-link ${kind}`} aria-hidden={!withDetail}>
      <span className="collab-link-rule" />
      <small>{kind === "parallel" ? "并行汇入" : "串行"}</small>
      {toolSummary && (
        <b className="collab-link-tools">
          <Wrench size={10} />
          {toolSummary}
        </b>
      )}
      {withDetail && toolCalls.length > 0 && (
        <div className="collab-hover" role="tooltip">
          <div className="collab-hover-block">
            <h5>这条链路上的工具调用</h5>
            <ul>
              {toolCalls.map((call) => (
                <li key={call.id} className={call.status}>
                  <b>{call.name}</b>
                  <span>{call.status}</span>
                  <pre>{call.input}</pre>
                  <pre>{call.output}</pre>
                  {call.error && <em>{call.error}</em>}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}

/** 侧栏紧凑态。 */
export function CollaborationGraph({
  graph,
  onExpand,
}: {
  graph: CollabGraph;
  /** 有它就渲染「展开全屏」入口。 */
  onExpand?: () => void;
}) {
  if (!graph.nodes.length) {
    return (
      <p className="collab-empty">
        任务开始后，这里会显示 Agent 之间的协作关系、各自的参数与 Token 消耗。
      </p>
    );
  }
  const hasParallel = graph.waves.some((wave) => wave.length > 1);
  const edgeInto = (id: string) => graph.edges.filter((edge) => edge.to === id);

  return (
    <div className="collab-graph" aria-label="Agent 协作链路">
      {onExpand && (
        <button type="button" className="cfg-quiet collab-expand" onClick={onExpand}>
          <Maximize2 size={12} />
          展开全屏画布
        </button>
      )}
      {graph.waves.map((wave, index) => (
        <div className="collab-wave" key={wave.map((node) => node.id).join("+")}>
          {index > 0 &&
            wave.map((node) =>
              edgeInto(node.id).map((edge) => (
                <EdgeLine
                  key={`${edge.from}-${edge.to}`}
                  kind={edge.kind}
                  toolSummary={edge.toolSummary}
                />
              )),
            )}
          <div className={`collab-row ${wave.length > 1 ? "parallel" : ""}`}>
            {wave.map((node) => (
              <AgentNodeCard key={node.id} node={node} />
            ))}
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
      <p className="collab-foot">
        <ChevronRight size={11} />
        卡片记录各 Agent 的模型、参数与 Token；执行轨迹在执行台卡片上点开。
      </p>
    </div>
  );
}

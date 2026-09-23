import { useCallback, useEffect, useId, useState } from "react";
import {
  Clock3,
  Gauge,
  Wrench,
  ChevronRight,
  History,
  MessagesSquare,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { PageTabs, type PageTab } from "../components/PageTabs";
import { Status } from "../components/Status";
import { InlineConfirm } from "../components/InlineConfirm";
import { AgentGlyph } from "../components/AgentGlyph";
import { Chip, EmptyState, describeError, formatTime } from "../config/shared";
import { api } from "../api/client";
import type {
  SessionSummary,
  StageTraceItem,
  Workflow,
  WorkflowStageTrace,
} from "../types/api";
import { PLAN_SOURCE_HINT, PLAN_SOURCE_TEXT, planSourceKey } from "../workspace/collaboration";
import { RuntimeSampling, ToolCallRecords } from "./Inspection";
import "./records.css";

/**
 * 任务记录页的分区。
 *
 * 分区依据是「记录的产生位置」，不是数据类型：
 * - runs：一次协作任务的整体执行
 * - calls：任务内单个工具的调用
 * - metrics：运行时的指标采样
 * - sessions：历史会话（跨重启持久化在 PostgreSQL）
 * 各分区的读取频率与体量差很多，混在一页会互相淹没，所以拆成副路由。
 */
export type RecordTabId = "runs" | "calls" | "metrics" | "sessions";

export const RECORD_TABS: readonly PageTab<RecordTabId>[] = [
  { id: "runs", label: "运行记录", icon: Clock3, title: "当前任务的整体执行与终态" },
  { id: "calls", label: "工具调用", icon: Wrench, title: "当前任务内单个工具的入参、出参与失败原因" },
  { id: "metrics", label: "指标采样", icon: Gauge, title: "当前任务运行时 Token 与耗时采样（原值，不累加）" },
  { id: "sessions", label: "历史会话", icon: MessagesSquare, title: "浏览并恢复所有历史任务" },
];

/* -------------------------------------------------------------------------- */
/* 运行记录：当前会话最近一次执行                                            */
/* -------------------------------------------------------------------------- */

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

/**
 * 单步阶段日志（§5.17）。
 *
 * 失败的工具调用单独顶出来：整条链路失败时，原因往往就藏在某一次工具调用里，
 * 让它埋在「输入与产出」的 JSON 里等于没有报错信息。
 */
function StageLogRow({ item }: { item: StageTraceItem }) {
  const failed = item.tool_calls.filter((call) => call.status === "failed");
  return (
    <article className="record-row record-stage">
      <div className="record-row-main">
        <AgentGlyph role={item.role} name={item.role} size={16} />
        <b>{item.role}</b>
        <span className="record-run-id">{item.stage}</span>
        {item.tool_calls.length > 0 && (
          <Chip tone="slate">{item.tool_calls.length} 次工具调用</Chip>
        )}
        {failed.length > 0 && <Chip tone="rose">{failed.length} 次失败</Chip>}
        {item.truncated && <Chip tone="amber">内容已截断</Chip>}
      </div>

      {/* reason 与「有轨迹」互斥，且四种原因指向四种不同的下一步，原样显示。 */}
      {item.reason && <p className="cfg-hint">{item.reason}</p>}

      {failed.map((call) =>
        call.error ? (
          <p role="alert" className="cfg-alert" key={call.call_id}>
            <b>{call.tool_name}</b>：{call.error}
          </p>
        ) : null,
      )}

      <details className="cfg-tool-schema">
        <summary>上游输入、本阶段产出与工具调用</summary>
        <p className="cfg-hint">
          输入来自：
          {item.input_from ?? "（根阶段，收到的就是原始任务）"}
        </p>
        <pre>
          {JSON.stringify(
            { input: item.input, output: item.output, tool_calls: item.tool_calls },
            null,
            2,
          )}
        </pre>
      </details>
    </article>
  );
}

/**
 * 运行记录：当前会话最近一次执行。
 *
 * 卡片**就地展开**，不再把整张卡做成「跳到工作台」的入口：日志是要读的，
 * 跳走之后用户还得自己找回来。展开区里是逐阶段日志，卡片自己的右下角留一个
 * 显式入口给「要看对话与执行台」这一种需求。
 *
 * 取阶段日志的时机是**展开时**，而不是卡片一渲染就取：运行记录页默认只显示终态，
 * 大多数人不需要逐阶段明细，不该为看一页状态付一次全量轨迹请求。
 */
function RunRecords({
  workflow,
  messageCount,
  onOpen,
}: {
  workflow: Workflow | null;
  messageCount: number;
  onOpen: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<WorkflowStageTrace | null>(null);
  const [traceError, setTraceError] = useState("");
  const [revision, setRevision] = useState(0);
  const panelId = useId();

  const workflowId = workflow?.id ?? "";
  // 正在跑的任务，`updated_at` 每推进一步就变；把它并进依赖，展开区会跟着一起长。
  const refreshedAt = workflow?.updated_at ?? "";

  // 换了工作流（切会话 / 新任务）就把展开区清干净：否则会拿上一条链路的阶段日志
  // 冒充本次执行，而且看不出是陈的。
  useEffect(() => {
    setTrace(null);
    setTraceError("");
  }, [workflowId]);

  useEffect(() => {
    if (!open || !workflowId) return;
    let live = true;
    setTraceError("");
    void (async () => {
      try {
        const value = await api.getWorkflowStages(workflowId);
        if (live) setTrace(value);
      } catch (cause) {
        if (!live) return;
        setTrace(null);
        setTraceError(describeError(cause, "阶段日志读取失败。"));
      }
    })();
    return () => {
      live = false;
    };
  }, [open, workflowId, refreshedAt, revision]);

  return (
    <section className="cfg-block record-block" aria-label="运行记录">
      <div className="cfg-block-head record-head">
        <div>
          <h3>运行记录</h3>
          <p>
            当前会话最近一次执行的终态、失败原因与逐阶段日志，都在这里就地展开；
            需要对话与执行台时再点卡片内的入口。切换其它历史任务请用页头「历史会话」。
          </p>
        </div>
      </div>

      {workflow ? (
        <div className="record-list">
          <div className={`record-run record-run-detail${open ? " open" : ""}`}>
            <button
              type="button"
              className="record-run-main"
              aria-expanded={open}
              {...(open ? { "aria-controls": panelId } : {})}
              onClick={() => setOpen((value) => !value)}
            >
              <span className="record-run-head">
                <ChevronRight
                  size={14}
                  className={`record-run-chevron${open ? " is-open" : ""}`}
                  aria-hidden="true"
                />
                <Status status={workflow.status} />
                <b>协作任务</b>
                <span className="record-run-id">{workflow.id.slice(0, 8)}</span>
                <time>{formatTime(workflow.updated_at)}</time>
              </span>
              <dl className="record-run-facts">
                <div>
                  <dt>当前阶段</dt>
                  <dd>{workflow.checkpoint?.current_step ?? workflow.current_step ?? "—"}</dd>
                </div>
                <div>
                  <dt>已完成步骤</dt>
                  <dd>{workflow.checkpoint?.completed_steps?.length ?? 0} 个</dd>
                </div>
                <div>
                  <dt>会话消息</dt>
                  <dd>{messageCount} 条</dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>{STATUS_LABEL[workflow.status] ?? workflow.status}</dd>
                </div>
              </dl>
              <span className="record-run-head record-run-hint">
                <span className="record-run-id">
                  {open ? "收起阶段日志" : "展开阶段日志与失败原因"}
                </span>
                <ChevronRight size={14} aria-hidden="true" />
              </span>
            </button>

            {/* 失败原因放在折叠区**之外**：失败却要先点开才看得到原因，就是这次要修的问题。
                判据是「有没有原因」而不是「status 是不是 failed」——有些失败没有落 error，
                那种情况下也不该凭空编一个原因出来。 */}
            {workflow.error && (
              <div className="record-run-alert">
                <p role="alert" className="cfg-alert">
                  <b>失败原因</b>
                  <span>{workflow.error}</span>
                </p>
              </div>
            )}

            {open && (
              <div className="record-run-body" id={panelId}>
                <div className="record-series-title">
                  <b>阶段日志</b>
                  {trace && (
                    <Chip tone="slate" title={PLAN_SOURCE_HINT[planSourceKey({ mode: trace.mode })]}>
                      {PLAN_SOURCE_TEXT[planSourceKey({ mode: trace.mode })]}
                    </Chip>
                  )}
                  <span className="record-label">
                    {trace ? `${trace.items.length} 步` : "读取中"}
                  </span>
                  <button
                    type="button"
                    className="cfg-quiet record-log-refresh"
                    onClick={() => setRevision((value) => value + 1)}
                  >
                    <RefreshCw size={13} />
                    重新读取
                  </button>
                </div>

                {traceError && (
                  <p role="alert" className="cfg-alert">
                    {traceError}
                  </p>
                )}

                {!trace && !traceError && <p className="cfg-hint">读取阶段日志…</p>}

                {trace?.availability === "not_integrated" && (
                  <p className="cfg-hint">{trace.reason ?? "后端未提供逐阶段轨迹。"}</p>
                )}

                {trace?.availability === "available" && trace.items.length === 0 && (
                  <p className="cfg-hint">这条链路还没有可展开的阶段轨迹。</p>
                )}

                {trace?.items.map((item) => (
                  <StageLogRow key={item.stage} item={item} />
                ))}

                <div className="record-run-actions">
                  <button type="button" className="cfg-quiet" onClick={onOpen}>
                    在工作台打开对话与执行台
                    <ChevronRight size={13} aria-hidden="true" />
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      ) : (
        <EmptyState
          title="还没有运行记录"
          hint="在工作台提交第一个任务后，它的执行记录会出现在这里。"
        />
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 历史会话：跨重启持久化的会话列表（`doc/api.md` §5.13）                      */
/* -------------------------------------------------------------------------- */

const PAGE_SIZE = 20;

function SessionHistory({
  onOpen,
  onDelete,
  sessionId,
}: {
  onOpen: (session: SessionSummary) => void;
  /** 必须可等待：删除**成功之后**才重拉列表，否则会删完立刻拉到旧数据（§5.14）。 */
  onDelete: (session: SessionSummary) => Promise<void>;
  sessionId: string | null;
}) {
  const [items, setItems] = useState<SessionSummary[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const load = useCallback(async (targetPage: number) => {
    setLoading(true);
    setError("");
    try {
      const result = await api.listSessions(targetPage, PAGE_SIZE);
      setItems(result.items);
      setTotal(result.total);
      setPage(targetPage);
    } catch (cause) {
      setError(describeError(cause, "无法加载历史会话"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(1);
  }, [load]);

  return (
    <section className="cfg-block record-block" aria-label="历史会话">
      <div className="cfg-block-head record-head">
        <div>
          <h3>历史会话</h3>
          <p>会话与消息持久化在 PostgreSQL，重启不丢失。点选任一会话即可在工作台恢复查看。</p>
        </div>
      </div>

      {error && (
        <p className="cfg-notice bad" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <p className="cfg-empty">加载中…</p>
      ) : items.length === 0 ? (
        <EmptyState
          title="还没有历史会话"
          hint="在工作台提交任务后，会话会自动持久化到这里。"
        />
      ) : (
        <div className="record-list">
          {items.map((item) => (
            <div
              key={item.id}
              className={`record-run record-run-session ${item.id === sessionId ? "current" : ""}`}
            >
              <button type="button" className="record-run-main" onClick={() => onOpen(item)}>
                <span className="record-run-head">
                  <b>{item.title}</b>
                  {item.id === sessionId && <span className="record-run-current">当前</span>}
                  <span className="record-run-id">{item.id.slice(0, 8)}</span>
                  <time>{formatTime(item.updated_at)}</time>
                </span>
                <dl className="record-run-facts">
                  <div>
                    <dt>最近执行</dt>
                    <dd>
                      {item.latest_workflow_status ? (
                        <Status status={item.latest_workflow_status} />
                      ) : (
                        "尚无任务"
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>会话状态</dt>
                    <dd>{item.status === "paused" ? "已暂停" : "进行中"}</dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd>{formatTime(item.created_at)}</dd>
                  </div>
                </dl>
                <span className="record-run-head">
                  <span className="record-run-id">恢复查看此会话</span>
                  <ChevronRight size={14} />
                </span>
              </button>
              <InlineConfirm
                label={`删除会话「${item.title || "（暂无消息）"}」`}
                confirmLabel="删除"
                triggerClassName="record-run-delete"
                triggerLabel={`删除会话：${item.title}`}
                triggerTitle="删除此会话，消息与运行记录一并删除且不可恢复"
                slotClassName="record-run-delete-slot"
                size="sm"
                onConfirm={() => {
                  // 先删再拉：原来是 onDelete 与 load 并发，存在「删完立刻重拉、拿回旧列表」
                  // 的竞态（表现为记录还在）。删掉本页最后一条时回退一页，不停在空页上。
                  void (async () => {
                    await onDelete(item);
                    await load(items.length === 1 && page > 1 ? page - 1 : page);
                  })();
                }}
              >
                <Trash2 size={15} />
              </InlineConfirm>
            </div>
          ))}
        </div>
      )}

      {pages > 1 && (
        <div className="record-pager">
          <button
            type="button"
            className="cfg-quiet"
            disabled={page <= 1}
            onClick={() => void load(page - 1)}
          >
            上一页
          </button>
          <span>
            {page} / {pages}
          </span>
          <button
            type="button"
            className="cfg-quiet"
            disabled={page >= pages}
            onClick={() => void load(page + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 当前任务徽标条：明确「本页聚焦当前任务」，历史任务走独立入口                  */
/* -------------------------------------------------------------------------- */

function CurrentTaskBar({
  title,
  workflow,
  onOpenHistory,
}: {
  title: string;
  workflow: Workflow | null;
  onOpenHistory: () => void;
}) {
  return (
    <section className="record-current-bar" aria-label="当前任务">
      <span className="record-current-mark">
        <Clock3 size={14} />
      </span>
      <div className="record-current-body">
        <span className="record-current-label">当前任务</span>
        <b className="record-current-title">{title.slice(0, 80) || "（尚未提交任务）"}</b>
      </div>
      {workflow && <Status status={workflow.status} />}
      <button type="button" className="cfg-quiet record-current-history" onClick={onOpenHistory}>
        <MessagesSquare size={14} />
        历史会话
      </button>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export function RecordsPage({
  tab,
  onTabChange,
  workflow,
  messageCount,
  currentTaskTitle,
  sessionId,
  onOpenRun,
  onOpenSession,
  onDeleteSession,
}: {
  tab: RecordTabId;
  onTabChange: (id: RecordTabId) => void;
  workflow: Workflow | null;
  messageCount: number;
  currentTaskTitle: string;
  sessionId: string | null;
  onOpenRun: () => void;
  onOpenSession: (session: SessionSummary) => void;
  onDeleteSession: (session: SessionSummary) => Promise<void>;
}) {
  const active = RECORD_TABS.find((item) => item.id === tab) ?? RECORD_TABS[0];

  return (
    <div className="config-page">
      <section className="page-heading">
        <span className="eyebrow">
          <History size={13} />
          RUN RECORDS
        </span>
        <h1>任务记录</h1>
        <p>{active.title}</p>
      </section>

      <CurrentTaskBar
        title={currentTaskTitle}
        workflow={workflow}
        onOpenHistory={() => onTabChange("sessions")}
      />

      <PageTabs tabs={RECORD_TABS} active={tab} onChange={onTabChange} label="记录分区" />

      <div className="cfg-tab-panel" role="tabpanel" id={`record-panel-${tab}`}>
        {tab === "runs" && (
          <RunRecords workflow={workflow} messageCount={messageCount} onOpen={onOpenRun} />
        )}
        {tab === "calls" &&
          (workflow ? (
            <ToolCallRecords workflow={workflow} />
          ) : (
            <section className="cfg-block record-block">
              <div className="cfg-block-head record-head">
                <div>
                  <h3>工具调用</h3>
                  <p>任务内单个工具的入参、出参与失败原因。</p>
                </div>
              </div>
              <EmptyState
                title="没有进行中的任务"
                hint="先在工作台提交一个任务，这里才会出现工具调用链路。"
              />
            </section>
          ))}
        {tab === "metrics" && <RuntimeSampling workflow={workflow} />}
        {tab === "sessions" && (
          <SessionHistory
            onOpen={onOpenSession}
            onDelete={onDeleteSession}
            sessionId={sessionId}
          />
        )}
      </div>
    </div>
  );
}

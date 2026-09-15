import { useCallback, useEffect, useState } from "react";
import { Clock3, Gauge, Wrench, ChevronRight, History, MessagesSquare, Trash2 } from "lucide-react";
import { PageTabs, type PageTab } from "../components/PageTabs";
import { Status } from "../components/Status";
import { EmptyState, describeError, formatTime } from "../config/shared";
import { api } from "../api/client";
import type { SessionSummary, Workflow } from "../types/api";
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

function RunRecords({
  workflow,
  messageCount,
  onOpen,
}: {
  workflow: Workflow | null;
  messageCount: number;
  onOpen: () => void;
}) {
  return (
    <section className="cfg-block record-block" aria-label="运行记录">
      <div className="cfg-block-head record-head">
        <div>
          <h3>运行记录</h3>
          <p>当前会话最近一次执行的终态与检查点。切换其它历史任务请用页头「历史会话」入口。</p>
        </div>
      </div>

      {workflow ? (
        <div className="record-list">
          <button type="button" className="record-run" onClick={onOpen}>
            <span className="record-run-head">
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
            <span className="record-run-head">
              <span className="record-run-id">打开工作台查看对话与执行台</span>
              <ChevronRight size={14} />
            </span>
          </button>
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
  onDelete: (session: SessionSummary) => void;
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
              <button
                type="button"
                className="record-run-delete"
                aria-label={`删除会话：${item.title}`}
                title="删除此会话"
                onClick={() => {
                  if (
                    window.confirm(
                      `确定删除会话「${item.title || "（暂无消息）"}」？\n该会话的消息与运行记录将一并删除，且不可恢复。`,
                    )
                  ) {
                    onDelete(item);
                    void load(page);
                  }
                }}
              >
                <Trash2 size={15} />
              </button>
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
  onDeleteSession: (session: SessionSummary) => void;
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

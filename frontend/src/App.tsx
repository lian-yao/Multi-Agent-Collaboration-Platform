import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Activity,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  Gauge,
  History,
  LoaderCircle,
  Maximize2,
  MessageSquareText,
  Network,
  PanelBottom,
  PanelLeft,
  PanelRight,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  ShieldCheck,
  Trash2,
  UsersRound,
  X,
  Zap,
} from "lucide-react";
import { MorphIcon } from "morphicons/react";
import {
  ArrowDown as ArrowDownData,
  ArrowUp as ArrowUpData,
  Menu as MenuData,
  PanelBottom as PanelBottomData,
  PanelRight as PanelRightData,
  Pause as PauseData,
  Play as PlayData,
  X as XData,
} from "lucide";
import { api } from "./api/client";
import { Status, statusText } from "./components/Status";
import { InlineConfirm } from "./components/InlineConfirm";
import { ConfigPage } from "./config/ConfigPage";
import { AgentPanel } from "./config/AgentPanel";
import { RecordsPage, type RecordTabId } from "./records/RecordsPage";
import { AgentStageModal, type AgentStageDetail } from "./workspace/AgentStageModal";
import { CollaborationGraph, type CollaboratorNode } from "./workspace/CollaborationGraph";
import { TaskUsage } from "./workspace/TaskUsage";
import type { Agent, Message, Session, SessionSummary, Workflow } from "./types/api";

const stages = [
  {
    id: "collect",
    label: "信息收集",
    agent: "collector",
    icon: Database,
    tone: "amber",
  },
  {
    id: "analyze",
    label: "数据分析",
    agent: "analyst",
    icon: Gauge,
    tone: "blue",
  },
  {
    id: "report",
    label: "报告生成",
    agent: "reporter",
    icon: FileText,
    tone: "green",
  },
] as const;
const time = (v?: string) =>
  v
    ? new Date(v).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
type View = "workspace" | "records" | "team" | "tools";

function MorphStateIcon({ state, size = 16 }: { state: "menu" | "close" | "play" | "pause" | "up" | "down" | "panel" | "panel-open"; size?: number }) {
  const icons = { menu: MenuData, close: XData, play: PlayData, pause: PauseData, up: ArrowUpData, down: ArrowDownData, panel: PanelRightData, "panel-open": PanelBottomData };
  return <MorphIcon icon={(icons[state] as any)[2]} size={size} spring="snappy" reducedMotion="user" />;
}

export function App() {
  const [view, setView] = useState<View>("workspace");
  // 任务记录页的副路由：runs / calls / metrics，切页时保留用户上次的选择。
  const [recordTab, setRecordTab] = useState<RecordTabId>("runs");
  const [session, setSession] = useState<Session | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [dockOpen, setDockOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const finalRefreshDone = useRef<string | null>(null);
  const refresh = useCallback(async () => {
    if (!session) return;
    setRefreshing(true);
    try {
      const [s, m] = await Promise.all([
        api.getSession(session.id),
        api.getMessages(session.id),
      ]);
      setSession(s);
      setMessages(m);
      if (workflow) setWorkflow(await api.getWorkflow(workflow.id));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法刷新运行状态");
    } finally {
      setRefreshing(false);
    }
  }, [session, workflow]);
  // 初始化只拉角色清单，**不**建会话：工作台起步于「草稿态」（session = null），会话在提交
  // 首条消息时才落库。否则每次刷新页面都会在历史里留下一条空会话（`doc/api.md` §4.2 / §7）。
  useEffect(() => {
    void (async () => {
      try {
        setAgents(await api.getAgents());
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "无法连接后端服务");
      } finally {
        setLoading(false);
      }
    })();
  }, []);
  useEffect(() => {
    if (!workflow) return;
    if (["completed", "failed", "cancelled"].includes(workflow.status)) {
      if (finalRefreshDone.current === workflow.id) return;
      const timer = window.setTimeout(() => {
        finalRefreshDone.current = workflow.id;
        void refresh();
      }, 1500);
      return () => window.clearTimeout(timer);
    }
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [workflow, refresh]);
  const completed = useMemo(
    () => new Set(workflow?.checkpoint?.completed_steps ?? []),
    [workflow],
  );
  const activeStage =
    workflow && ["running", "paused"].includes(workflow.status)
      ? (workflow.checkpoint?.current_step ?? workflow.current_step)
      : null;
  const activeAgent = agents.find(
    (a) => a.id === stages.find((s) => s.id === activeStage)?.agent,
  );
  // 当前任务标题：取首条用户消息，无则视为「新建任务」。侧栏用户区与记录页页头共用。
  const currentTaskTitle =
    messages.find((message) => message.role === "user")?.content ?? "";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (
      session?.status === "paused" ||
      sending ||
      !content.trim() ||
      workflow?.status === "running" ||
      workflow?.status === "pending"
    )
      return;
    setSending(true);
    setError("");
    // 草稿态（session = null）：首条消息才把会话落到库里，用户只感知到「发出去了一条消息」。
    let sessionId: string | null = session?.id ?? null;
    let created = false;
    try {
      if (!sessionId) {
        const fresh = await api.createSession();
        sessionId = fresh.id;
        created = true;
        setSession(fresh);
      }
      const accepted = await api.sendMessage(sessionId, content.trim());
      setContent("");
      setWorkflow(await api.getWorkflow(accepted.workflow_id));
      setMessages(await api.getMessages(sessionId));
      finalRefreshDone.current = null;
    } catch (cause) {
      // 首条消息就没发出去 → 撤掉刚建的会话，别在历史里留一条空数据（`doc/api.md` §4.2）。
      if (created && sessionId) {
        try {
          await api.deleteSession(sessionId);
        } catch {
          /* 撤销失败不覆盖主错误：最坏情况只多出一条空会话 */
        }
        setSession(null);
      }
      setError(cause instanceof Error ? cause.message : "任务提交失败");
    } finally {
      setSending(false);
    }
  };
  /** 回到草稿态的新对话模板：纯前端重置，**不**向后端建会话（`doc/api.md` §4.2）。 */
  const startNewTask = () => {
    setError("");
    setSession(null);
    setMessages([]);
    setWorkflow(null);
    setContent("");
    finalRefreshDone.current = null;
    setView("workspace");
  };
  const toggleSession = async () => {
    if (!session || !workflow) return;
    try {
      const r =
        session.status === "paused"
          ? await api.resumeSession(session.id)
          : await api.pauseSession(session.id);
      setSession(r.session);
      if (r.workflow) setWorkflow(r.workflow);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "状态切换失败");
    }
  };
  const openSession = async (target: SessionSummary) => {
    setError("");
    try {
      const [nextSession, nextMessages] = await Promise.all([
        api.getSession(target.id),
        api.getMessages(target.id),
      ]);
      setSession(nextSession);
      setMessages(nextMessages);
      // 历史会话可能没有 workflow（尚未提交任务）；有则按摘要里的 id 取最近一条。
      setWorkflow(
        target.latest_workflow_id
          ? await api.getWorkflow(target.latest_workflow_id)
          : null,
      );
      setContent("");
      finalRefreshDone.current = null;
      setView("workspace");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法恢复历史会话");
    }
  };
  /** 删除会话。返回 Promise 让调用方（侧栏下拉 / 记录页）能在删除成功后再重拉列表。 */
  const deleteSession = async (target: SessionSummary): Promise<void> => {
    setError("");
    try {
      await api.deleteSession(target.id);
      // 删的是当前会话 → 回到草稿态的新对话模板，而不是再建一个会话。
      if (session?.id === target.id) startNewTask();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法删除会话");
    }
  };
  if (loading)
    return (
      <div className="loading-screen">
        <LoaderCircle className="spin" size={21} />
        正在连接协作运行时
      </div>
    );
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "is-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark">
            <Network size={18} />
          </div>
          <div>
            <strong>协作台</strong>
            <span>AGENT OPERATIONS</span>
          </div>
          <button
            className="icon-button mobile-close"
            onClick={() => setMobileNav(false)}
            aria-label="关闭导航"
          >
            <MorphStateIcon state="close" size={17} />
          </button>
        </div>
        <SidebarUser
          session={session}
          currentTaskTitle={currentTaskTitle}
          workflow={workflow}
          open={userMenuOpen}
          onToggle={() => setUserMenuOpen((v) => !v)}
          onOpenSession={(target) => {
            setUserMenuOpen(false);
            void openSession(target);
          }}
          onDeleteSession={deleteSession}
          onNewTask={() => {
            setUserMenuOpen(false);
            startNewTask();
          }}
        />
        <nav className="main-nav">
          <NavButton
            icon={PanelLeft}
            label="工作台"
            active={view === "workspace"}
            onClick={() => {
              setView("workspace");
              setMobileNav(false);
            }}
          />
          <NavButton
            icon={Clock3}
            label="任务记录"
            active={view === "records"}
            onClick={() => {
              setView("records");
              setMobileNav(false);
            }}
          />
          <NavButton
            icon={UsersRound}
            label="Agent 团队"
            active={view === "team"}
            onClick={() => {
              setView("team");
              setMobileNav(false);
            }}
          />
          <NavButton
            icon={Settings2}
            label="工具与配置"
            active={view === "tools"}
            onClick={() => {
              setView("tools");
              setMobileNav(false);
            }}
          />
        </nav>
        <div className="sidebar-spacer" />
        <div className="runtime-card">
          <div className="runtime-card-head">
            <span>运行时</span>
            <b className={error ? "bad" : ""}>
              <i />
              {error ? "异常" : "在线"}
            </b>
          </div>
          <div className="runtime-item">
            <span>
              <ShieldCheck size={14} />
              API 服务
            </span>
            <b>{error ? "检查中" : "正常"}</b>
          </div>
          <div className="runtime-item">
            <span>
              <Zap size={14} />
              Dapr Workflow
            </span>
            <b>{workflow ? "已连接" : "待命"}</b>
          </div>
        </div>
        <div className="sidebar-footer">
          <span>v0.1.0</span>
          <Settings2 size={15} />
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="topbar-left">
            <button className="icon-button mobile-menu" onClick={() => setMobileNav(true)} aria-label="打开导航"><MorphStateIcon state="menu" size={19} /></button>
            {view === "workspace" ? (
              <button className="new-task-button" type="button" onClick={startNewTask}><span>＋</span>新建任务</button>
            ) : (
              <div className="crumb"><b>{view === "records" ? "任务记录" : view === "team" ? "Agent 团队" : "工具与配置"}</b></div>
            )}
          </div>
          {view === "workspace" && <div className="topbar-title">{messages.find((message) => message.role === "user")?.content.slice(0, 42) || "新建协作任务"}</div>}
          <div className="topbar-actions">
            {view === "workspace" && workflow && <Status status={workflow.status} />}
            {view === "workspace" && workflow && <button className="topbar-status-button" onClick={toggleSession} disabled={!(["running", "paused"].includes(workflow.status))} aria-label={session?.status === "paused" ? "恢复会话与任务" : "暂停会话与任务"}>{session?.status === "paused" ? <MorphStateIcon state="play" size={14} /> : <MorphStateIcon state="pause" size={14} />}<span>{session?.status === "paused" ? "恢复" : "暂停"}</span></button>}
            <span className={`connection ${error ? "bad" : ""}`}><i />{error ? "连接异常" : "API 已连接"}</span>
            <button className="icon-button" onClick={() => void refresh()} title="刷新状态" aria-label="刷新状态"><RefreshCw size={16} className={refreshing ? "spin" : ""} /></button>
            {view === "workspace" && <>
              <button className={`icon-button toolbar-toggle ${dockOpen ? "is-active" : ""}`} onClick={() => setDockOpen(!dockOpen)} title={dockOpen ? "隐藏 Agent 执行台" : "显示 Agent 执行台"} aria-label={dockOpen ? "隐藏 Agent 执行台" : "显示 Agent 执行台"} aria-pressed={dockOpen}><MorphStateIcon state="panel-open" /></button>
              <button className={`icon-button toolbar-toggle ${inspectorOpen ? "is-active" : ""}`} onClick={() => setInspectorOpen(!inspectorOpen)} title={inspectorOpen ? "隐藏协作详情" : "显示协作详情"} aria-label={inspectorOpen ? "隐藏协作详情" : "显示协作详情"} aria-pressed={inspectorOpen}><MorphStateIcon state="panel" /></button>
            </>}
          </div>
        </header>
        <main
          className={`page-content ${view === "workspace" ? "workspace-page" : ""}`}
        >
          {error && (
            <div className="error-banner">
              <CircleAlert size={16} />
              <span>{error}</span>
              <button
                className="icon-button"
                onClick={() => void refresh()}
                aria-label="重试"
              >
                <RotateCcw size={15} />
              </button>
            </div>
          )}
          {view === "workspace" && (
            <Workspace
              session={session}
              agents={agents}
              messages={messages}
              workflow={workflow}
              content={content}
              setContent={setContent}
              sending={sending}
              completed={completed}
              activeStage={activeStage}
              activeAgent={activeAgent}
              dockOpen={dockOpen}
              setDockOpen={setDockOpen}
              inspectorOpen={inspectorOpen}
              setInspectorOpen={setInspectorOpen}
              submit={submit}
              toggleSession={toggleSession}
              onOpenRecords={() => {
                setRecordTab("calls");
                setView("records");
              }}
            />
          )}
          {view === "records" && (
            <RecordsPage
              tab={recordTab}
              onTabChange={setRecordTab}
              workflow={workflow}
              messageCount={messages.length}
              currentTaskTitle={currentTaskTitle}
              sessionId={session?.id ?? null}
              onOpenRun={() => setView("workspace")}
              onOpenSession={openSession}
              onDeleteSession={deleteSession}
            />
          )}
          {view === "team" && <AgentTeamPage activeAgentId={activeAgent?.id} />}
          {view === "tools" && <ConfigPage />}
        </main>
      </div>
    </div>
  );
}
function NavButton({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: typeof PanelLeft;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={`nav-button ${active ? "active" : ""}`}
      onClick={onClick}
    >
      <Icon size={17} />
      <span>{label}</span>
      {active && <ChevronRight size={14} />}
    </button>
  );
}

/**
 * 侧栏「当前任务」区（原「演示用户」）。
 *
 * 展示当前打开会话的任务标题与状态，点击展开历史会话下拉，可切换/恢复任一历史
 * 会话，或新建任务。历史列表复用 `GET /api/v1/sessions`（`doc/api.md` §5.13）。
 */
function SidebarUser({
  session,
  currentTaskTitle,
  workflow,
  open,
  onToggle,
  onOpenSession,
  onDeleteSession,
  onNewTask,
}: {
  session: Session | null;
  currentTaskTitle: string;
  workflow: Workflow | null;
  open: boolean;
  onToggle: () => void;
  onOpenSession: (target: SessionSummary) => void;
  /** 删除成功后由本组件重拉列表，所以必须是可等待的（`doc/api.md` §5.14）。 */
  onDeleteSession: (target: SessionSummary) => Promise<void>;
  onNewTask: () => void;
}) {
  const [items, setItems] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    api
      .listSessions(1, 20)
      .then((result) => setItems(result.items))
      .catch((cause) =>
        setError(cause instanceof Error ? cause.message : "无法加载历史会话"),
      )
      .finally(() => setLoading(false));
  }, []);

  // 下拉展开时才拉取历史列表，避免每次进侧栏都发请求。
  useEffect(() => {
    if (!open) return;
    load();
  }, [open, load]);

  const title = currentTaskTitle.slice(0, 42) || "新建协作任务";
  const sub = session
    ? session.status === "paused"
      ? "已暂停"
      : workflow
        ? `任务${statusText[workflow.status] ?? ""}`
        : "会话进行中"
    : "新对话";

  return (
    <div className={`sidebar-user ${open ? "is-open" : ""}`}>
      <button
        type="button"
        className="sidebar-user-main"
        onClick={onToggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`当前任务：${title}，点击查看历史会话`}
      >
        <span className="sidebar-user-avatar">
          {currentTaskTitle ? "D" : <Plus size={14} />}
        </span>
        <span className="sidebar-user-meta">
          <b>{title}</b>
          <small>{sub}</small>
        </span>
        <ChevronDown size={14} className={open ? "flip" : ""} />
      </button>

      {open && (
        <>
          <div className="sidebar-user-backdrop" onClick={onToggle} />
          <div className="sidebar-user-menu" role="listbox" aria-label="历史会话">
            <div className="sidebar-user-menu-head">
              <span>历史会话</span>
              <button type="button" className="cfg-quiet" onClick={onNewTask}>
                <Plus size={13} />
                新建任务
              </button>
            </div>
            {loading ? (
              <p className="sidebar-user-empty">加载中…</p>
            ) : error ? (
              <p className="sidebar-user-empty bad">{error}</p>
            ) : items.length === 0 ? (
              <p className="sidebar-user-empty">暂无历史会话</p>
            ) : (
              <ul className="sidebar-user-list">
                {items.map((item) => (
                  <li key={item.id} className={item.id === session?.id ? "current" : ""}>
                    <button
                      type="button"
                      className="sidebar-user-item"
                      onClick={() => onOpenSession(item)}
                    >
                      <span className="sidebar-user-item-title">
                        {item.title || "（暂无消息）"}
                      </span>
                      <span className="sidebar-user-item-sub">
                        {item.id === session?.id
                          ? "当前会话"
                          : item.latest_workflow_status
                            ? statusText[item.latest_workflow_status] ?? item.latest_workflow_status
                            : "尚无任务"}
                        {" · "}
                        {time(item.updated_at)}
                      </span>
                    </button>
                    <InlineConfirm
                      label={`删除会话「${item.title || "（暂无消息）"}」`}
                      confirmLabel="删除"
                      triggerClassName="sidebar-user-item-delete"
                      triggerLabel={`删除会话：${item.title || item.id.slice(0, 8)}`}
                      triggerTitle="删除此会话，消息与运行记录一并删除且不可恢复"
                      size="sm"
                      onConfirm={() => {
                        // 删完必须重拉列表：下拉只在展开时拉过一次，否则被删的行会一直留在
                        // 列表里，看起来像「删除没生效」（`doc/api.md` §5.14）。
                        void (async () => {
                          await onDeleteSession(item);
                          load();
                        })();
                      }}
                    >
                      <Trash2 size={14} />
                    </InlineConfirm>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
type WorkspaceProps = {
  session: Session | null;
  agents: Agent[];
  messages: Message[];
  workflow: Workflow | null;
  content: string;
  setContent: (v: string) => void;
  sending: boolean;
  completed: Set<string>;
  activeStage: string | null;
  activeAgent?: Agent;
  dockOpen: boolean;
  setDockOpen: (value: boolean) => void;
  inspectorOpen: boolean;
  setInspectorOpen: (value: boolean) => void;
  submit: (e: FormEvent) => void;
  toggleSession: () => void;
  /** 跳到「任务记录」页看逐条采样与工具调用（侧栏与弹窗都只给入口，不重复渲染）。 */
  onOpenRecords: () => void;
};

type StageId = (typeof stages)[number]["id"];
const responsibilities: Record<StageId, string> = {
  collect: "整理任务要求与输入资料，为后续分析准备信息。",
  analyze: "基于收集结果进行分析，梳理关键结论。",
  report: "整合分析结果，组织结构化报告。",
};
function stageStatus(
  stage: StageId,
  workflow: Workflow | null,
  completed: Set<string>,
) {
  if (completed.has(stage)) return "completed";
  const current = workflow?.checkpoint?.current_step ?? workflow?.current_step;
  if (
    current === stage &&
    workflow &&
    ["running", "paused", "failed"].includes(workflow.status)
  )
    return workflow.status;
  return "pending";
}

/** Agent 目录里不存在的角色不进执行台，也不进协作链路（doc/api.md §7）。 */
function participatingStages(agents: Agent[]): (typeof stages)[number][] {
  return stages.filter((stage) =>
    agents.some((agent) => agent.id === stage.agent),
  );
}

/**
 * 协作链路的波次。
 *
 * 后端当前是固定串行流水线，所以每波只有一个节点；编排层支持并行波次后，
 * 只需在这里把同波阶段放进同一个数组，`CollaborationGraph` 无需改动。
 */
function collaborationWaves(
  list: (typeof stages)[number][],
  workflow: Workflow | null,
  agents: Agent[],
  completed: Set<string>,
): CollaboratorNode[][] {
  if (!workflow) return [];
  return list.map((stage) => [
    {
      id: stage.id,
      stageLabel: stage.label,
      agentName:
        agents.find((agent) => agent.id === stage.agent)?.name ??
        `${stage.agent} Agent`,
      status: stageStatus(stage.id, workflow, completed),
    },
  ]);
}

/** 运行时长：进行中按「到现在」算，终态用 `completed_at`。 */
function formatDuration(ms: number) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

/** 组装执行台卡片弹窗的入参，保证弹窗本体保持无副作用、只吃 props。 */
function stageDetail(
  stage: StageId,
  workflow: Workflow | null,
  agents: Agent[],
  completed: Set<string>,
): AgentStageDetail {
  const meta = stages.find((item) => item.id === stage);
  return {
    stageId: stage,
    stageLabel: meta?.label ?? stage,
    responsibility: responsibilities[stage],
    status: stageStatus(stage, workflow, completed),
    agent: agents.find((agent) => agent.id === meta?.agent) ?? null,
    checkpointSaved: completed.has(stage),
    updatedAt: workflow?.updated_at,
  };
}

// Keep the conversation, composer and execution dock mounted across run transitions.
// See design module 6: session history and workflow visibility.
function Workspace({
  session,
  agents,
  messages,
  workflow,
  content,
  setContent,
  sending,
  completed,
  activeStage,
  dockOpen,
  setDockOpen,
  inspectorOpen,
  setInspectorOpen,
  submit,
  toggleSession,
  onOpenRecords,
}: WorkspaceProps) {
  const stream = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const followLatest = useRef(true);
  const [currentMessage, setCurrentMessage] = useState("");
  // 执行台卡片点开的是「单个 Agent 的阶段详情」，与右侧任务级侧栏解耦。
  const [detailStage, setDetailStage] = useState<StageId | null>(null);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const busy =
    sending || workflow?.status === "running" || workflow?.status === "pending";
  const participating = participatingStages(agents);
  const waves = collaborationWaves(participating, workflow, agents, completed);
  // 执行台把当前阶段置顶，便于执行中一眼看到谁在跑；协作链路视图仍按真实顺序渲染。
  const runtimeStages = workflow
    ? [...participating].sort((left, right) =>
        left.id === activeStage ? -1 : right.id === activeStage ? 1 : 0,
      )
    : [];
  const detail = detailStage
    ? stageDetail(detailStage, workflow, agents, completed)
    : null;

  useEffect(() => {
    if (followLatest.current && stream.current)
      stream.current.scrollTop = stream.current.scrollHeight;
  }, [messages.length, workflow?.status]);

  function trackScroll() {
    const el = stream.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
    followLatest.current = bottom;
    setAtBottom(bottom);
    const top = el.getBoundingClientRect().top;
    const articles = Array.from(
      el.querySelectorAll<HTMLElement>("[data-message-id]"),
    );
    const nearest = articles.filter(
      (a) => a.getBoundingClientRect().bottom > top + 24,
    )[0];
    if (nearest) setCurrentMessage(nearest.dataset.messageId ?? "");
  }
  function jumpTo(id: string) {
    const el = stream.current;
    const target = document.getElementById("message-" + id);
    if (!el || !target) return;
    followLatest.current = false;
    setCurrentMessage(id);
    el.scrollTo({
      top:
        el.scrollTop +
        target.getBoundingClientRect().top -
        el.getBoundingClientRect().top -
        24,
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
    });
    target.focus({ preventScroll: true });
  }
  function choosePrompt(prompt: string) {
    setContent(prompt);
    composer.current?.focus();
  }

  return (
    <div className="conversation-shell">
      <section className="conversation-main">

        <div className="conversation-stage">
          <ConversationOutline
            messages={messages}
            selected={currentMessage}
            onJump={jumpTo}
          />
          <div className="stream-region">
            <div
              className="message-stream"
              ref={stream}
              onScroll={trackScroll}
              role="region"
              aria-label="任务对话"
              tabIndex={0}
            >
              {messages.length ? (
                <div className="conversation-transcript">
                  {messages.map((m) => (
                    <MessageBubble key={m.id} message={m} />
                  ))}
                  {workflow && (
                    <div className="run-event" role="status">
                      {workflow.status === "running" ? (
                        <LoaderCircle size={16} className="spin" />
                      ) : (
                        <Activity size={16} />
                      )}
                      <div>
                        <b>任务{statusText[workflow.status]}</b>
                        <span>
                          已完成 {completed.size} 个阶段 · 可在下方查看执行状态
                        </span>
                        {workflow.status === "completed" &&
                          !messages.some(
                            (m) =>
                              m.role === "assistant" &&
                              m.agent_run_id === workflow.agent_run_id,
                          ) && <span>当前接口尚未返回报告正文。</span>}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <Welcome onChoose={choosePrompt} />
              )}
            </div>
            {!atBottom && (
              <button
                className="latest-message"
                onClick={() => {
                  if (stream.current)
                    stream.current.scrollTop = stream.current.scrollHeight;
                  followLatest.current = true;
                  setAtBottom(true);
                }}
              >
                <ChevronDown size={14} />
                回到最新
              </button>
            )}
          </div>
        </div>
        <form
          className={`conversation-composer ${composerExpanded ? "expanded" : ""}`}
          onSubmit={(e) => {
            followLatest.current = true;
            submit(e);
          }}
        >
          <button type="button" className="composer-resize" aria-label={composerExpanded ? "收起输入框" : "展开输入框"} onClick={() => setComposerExpanded((expanded) => !expanded)}><Maximize2 size={13} /></button>
          <textarea
            ref={composer}
            aria-label="任务输入"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="描述任务，或继续补充你的想法…"
            disabled={session?.status === "paused"}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="composer-toolbar">
            <div className="composer-hint">
              <small>
                {session?.status === "paused"
                  ? "会话已暂停"
                  : busy
                    ? "等待本次执行完成"
                    : "由编排层自动决策参与 Agent · Enter 发送 · Shift + Enter 换行"}
              </small>
            </div>
            <button
              className="primary-button"
              aria-label="发送任务"
              disabled={
                busy || !content.trim() || session?.status === "paused"
              }
            >
              {sending ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Send size={17} />
              )}
            </button>
          </div>
        </form>
      <Inspector
        open={inspectorOpen}
        onClose={() => setInspectorOpen(false)}
        workflow={workflow}
        completed={completed}
        activeStage={activeStage}
        waves={waves}
        onSelectStage={setDetailStage}
        onOpenRecords={onOpenRecords}
      />
      </section>
      {detail && (
        <AgentStageModal
          detail={detail}
          onClose={() => setDetailStage(null)}
          onOpenRecords={() => {
            setDetailStage(null);
            onOpenRecords();
          }}
        />
      )}
      <section
        className={`agent-dock ${dockOpen ? "" : "collapsed"}`}
        aria-label="多 Agent 执行台"
      >
        <header className="dock-header">
          <div>
            <Network size={16} />
            <b>Agent 执行台</b>
            <span>{workflow ? "当前工作流" : "任务开始后加载参与的 Agent"}</span>
          </div>
          <div>
            <span>
              {workflow
                ? `${completed.size} / ${runtimeStages.length} 完成`
                : "等待任务"}
            </span>
            <button
              className="icon-button"
              aria-label={dockOpen ? "收起执行台" : "展开执行台"}
              aria-expanded={dockOpen}
              onClick={() => setDockOpen(!dockOpen)}
            >
              <ChevronDown className={dockOpen ? "" : "flip"} size={16} />
            </button>
          </div>
        </header>
        {dockOpen && workflow && (
          <div className="dock-track">
            {runtimeStages.map((s) => {
              const Icon = s.icon;
              const state = stageStatus(s.id, workflow, completed);
              const agent = agents.find((entry) => entry.id === s.agent);
              const stepText = state === "running" ? "正在执行当前阶段" : state === "completed" ? "阶段已完成，检查点已保存" : state === "failed" ? "阶段执行失败，等待处理" : "等待前置阶段完成";
              return (
                <button
                  key={s.id}
                  className={`dock-node cli-node ${s.tone} ${state} ${detailStage === s.id ? "selected" : ""}`}
                  aria-label={`查看${agent?.name ?? `${s.label} Agent`}的阶段详情`}
                  aria-pressed={detailStage === s.id}
                  onClick={() => setDetailStage(s.id)}
                >
                  <span className="cli-node-head">
                    <span className="dock-icon">{state === "completed" ? <Check size={17} /> : state === "running" ? <LoaderCircle size={17} className="spin" /> : <Icon size={17} />}</span>
                    <span className="cli-node-title"><strong>{agent?.name ?? `${s.agent} Agent`}</strong><small>{agent?.model ?? "模型由运行时提供"}</small></span>
                    <Status status={state} />
                  </span>
                  <span className="cli-step"><i className={state === "running" ? "pulse" : ""} />{stepText}</span>
                  <span className="cli-log"><code>{state === "running" ? ">" : "$"}</code><span>{state === "completed" ? "checkpoint.persisted" : `${s.label} · ${responsibilities[s.id]}`}</span></span>
                  <span className="cli-meta"><span><Clock3 size={12} />{time(workflow.updated_at)}</span><span><Zap size={12} />{state === "completed" ? "已记录" : "监听中"}</span><ChevronRight size={14} /></span>
                </button>
              );
            })}
          </div>
        )}
        {dockOpen && !workflow && <div className="dock-empty"><Network size={15} />任务开始后，当前工作流使用的 Agent 会显示在这里</div>}
      </section>
    </div>
  );
}

function ConversationOutline({
  messages,
  selected,
  onJump,
}: {
  messages: Message[];
  selected: string;
  onJump: (id: string) => void;
}) {
  const turns = messages.filter((m) => m.role === "user");
  return (
    <nav className="conversation-outline" aria-label="对话大纲">
      {turns.length ? turns.map((m, index) => {
        const nextInput = messages.indexOf(turns[index + 1]);
        const after = messages.slice(messages.indexOf(m) + 1, nextInput < 0 ? undefined : nextInput);
        const reply = after.find((item) => item.role === "assistant");
        return (
          <button key={m.id} className={`outline-mark ${selected === m.id ? "current" : ""}`} aria-label={`定位第 ${index + 1} 个任务：${m.content.slice(0, 50)}`} aria-current={selected === m.id ? "location" : undefined} onClick={() => onJump(m.id)}>
            <span className="outline-dash" />
            <span className="outline-preview"><small>任务 {index + 1} · {time(m.created_at)}</small><b>{m.content}</b><span>{reply?.content ?? "暂无回复正文"}</span></span>
          </button>
        );
      }) : (
        <span className="outline-placeholder" title="发送任务后，可在这里定位历史对话"><MessageSquareText size={16} /></span>
      )}
    </nav>
  );
}
function Welcome({ onChoose }: { onChoose: (value: string) => void }) {
  const prompts = [
    {
      icon: FileText,
      title: "解读一篇文章",
      text: "请分析以下技术文章，提取核心观点并生成结构化报告：",
      tone: "blue",
    },
    {
      icon: Gauge,
      title: "比较两种方案",
      text: "请比较以下两种方案的优缺点，并整理为对比报告：",
      tone: "amber",
    },
    {
      icon: Database,
      title: "整理任务资料",
      text: "请整理以下资料，归纳主题、分析结论并生成报告：",
      tone: "green",
    },
  ];
  return (
    <div className="welcome">
      <div className="welcome-icon">
        <Network size={32} strokeWidth={1.5} />
      </div>
      <h2>我们一起完成什么？</h2>
      <p>写下目标，让 Agent 团队接力协作。</p>
      <div className="suggestions">
        {prompts.map((p) => (
          <button
            key={p.title}
            className={p.tone}
            onClick={() => onChoose(p.text)}
          >
            <p.icon size={20} />
            <b>{p.title}</b>
            <ChevronRight size={14} />
          </button>
        ))}
      </div>
    </div>
  );
}
function MessageBubble({ message }: { message: Message }) {
  const user = message.role === "user";
  return (
    <article
      id={"message-" + message.id}
      data-message-id={message.id}
      tabIndex={-1}
      className={`message-bubble ${user ? "user" : "assistant"}`}
    >
      <div className="message-avatar">{user ? "D" : <Bot size={17} />}</div>
      <div className="message-body">
        <div className="message-meta">
          <b>
            {user
              ? "你"
              : message.role === "assistant"
                ? "Agent 团队"
                : message.role}
          </b>
          <time>{time(message.created_at)}</time>
        </div>
        <p>{message.content}</p>
      </div>
    </article>
  );
}
/** 协作链路节点回传的是字符串 id，回到阶段类型前先收窄。 */
function isStageId(value: string): value is StageId {
  return stages.some((stage) => stage.id === value);
}

/**
 * 「任务协作」侧栏。
 *
 * 职责边界（ADR-018）：
 * - 只放**任务级**信息：整体状态、运行时长、协作链路、用量采样。
 * - 单个 Agent 的阶段详情在 `AgentStageModal`，由执行台卡片点开；侧栏不再跟随卡片
 *   点击而改变内容，避免把「谁在干」和「整体怎么样」两件事混成一个状态。
 * - 逐条工具调用与采样明细留在「任务记录」页，侧栏只给入口，不重复渲染同一份数据。
 */
function Inspector({
  open,
  onClose,
  workflow,
  completed,
  activeStage,
  waves,
  onSelectStage,
  onOpenRecords,
}: {
  open: boolean;
  onClose: () => void;
  workflow: Workflow | null;
  completed: Set<string>;
  activeStage: string | null;
  waves: CollaboratorNode[][];
  onSelectStage: (stage: StageId) => void;
  onOpenRecords: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const running = workflow?.status === "running";
  // 执行中的「运行时长」需要自己走秒：轮询只在 workflow 有变化时回来。
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const total = waves.length;
  const elapsed = workflow
    ? (workflow.completed_at ? new Date(workflow.completed_at).getTime() : now) -
      new Date(workflow.created_at).getTime()
    : 0;

  return (
    <aside className={`inspector ${open ? "open" : ""}`} aria-label="任务协作概览">
      <header className="inspector-title">
        <span>
          <Network size={16} />
          任务协作概览
        </span>
        <button
          className="icon-button inspector-close"
          aria-label="关闭任务协作概览"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </header>
      <section className="inspector-panel">
        <div className="inspector-head">
          <span className="eyebrow">本次任务</span>
          <Status status={workflow?.status ?? "idle"} />
        </div>
        <h3>{workflow ? `${completed.size} / ${total} 阶段完成` : "等待任务"}</h3>
        {workflow ? (
          <dl className="agent-facts task-stats">
            <div>
              <dt>运行时长</dt>
              <dd>{formatDuration(elapsed)}</dd>
            </div>
            <div>
              <dt>参与 Agent</dt>
              <dd>{total} 个</dd>
            </div>
            <div>
              <dt>开始时间</dt>
              <dd>{time(workflow.created_at)}</dd>
            </div>
            <div>
              <dt>{workflow.completed_at ? "结束时间" : "最近更新"}</dt>
              <dd>{time(workflow.completed_at ?? workflow.updated_at)}</dd>
            </div>
          </dl>
        ) : (
          <p className="inspector-copy">任务开始后，这里会显示协作过程与用量统计。</p>
        )}
      </section>
      <section className="inspector-panel">
        <div className="inspector-head">
          <span className="eyebrow">
            <Network size={13} />
            协作链路
          </span>
        </div>
        <p className="inspector-copy">波次之间串行、波次内部并行。点节点看该 Agent 的阶段详情。</p>
        <CollaborationGraph
          waves={waves}
          activeId={activeStage}
          onSelect={(id) => {
            if (isStageId(id)) onSelectStage(id);
          }}
        />
      </section>
      {workflow && (
        <section className="inspector-panel">
          <div className="inspector-head">
            <span className="eyebrow">
              <Gauge size={13} />
              任务用量
            </span>
          </div>
          <TaskUsage workflow={workflow} onOpenRecords={onOpenRecords} />
        </section>
      )}
    </aside>
  );
}
/**
 * Agent 团队页。
 *
 * 这里只写「角色 → 模型」的路由：哪张卡绑定哪个注册表模型、覆盖了哪些参数。
 * 端点与凭据属于「工具与配置」，本页只读不写，避免同一份数据在两处被改。
 */
function AgentTeamPage({ activeAgentId }: { activeAgentId?: string }) {
  return (
    <div className="config-page">
      <section className="page-heading">
        <span className="eyebrow">
          <UsersRound size={13} />
          AGENT TEAM
        </span>
        <h1>Agent 团队</h1>
        <p>为每个角色绑定注册表模型与参数覆盖。未绑定的角色走默认路由。</p>
      </section>
      <AgentPanel activeAgentId={activeAgentId} />
    </div>
  );
}

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
  Paperclip,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
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
import { formatStamp } from "./config/shared";
import { ConfigPage } from "./config/ConfigPage";
import { AgentPanel } from "./config/AgentPanel";
import { RecordsPage, type RecordTabId } from "./records/RecordsPage";
import { AgentStageModal, type AgentStageDetail } from "./workspace/AgentStageModal";
import { CollaborationGraph } from "./workspace/CollaborationGraph";
import { CollabCanvas, type CollabConversation } from "./workspace/CollabCanvas";
import { groupUsage, TaskUsagePanel, useWorkflowMetrics } from "./workspace/TaskUsage";
import {
  buildCollaboration,
  planStepStatus,
  planSteps,
  stageStatus,
  type StageMeta,
} from "./workspace/collaboration";
import { MessageAttachmentList, PendingFileChips } from "./workspace/AttachmentList";
import {
  ATTACHMENT_ACCEPT,
  MAX_ATTACHMENT_COUNT,
  classifyLocal,
  fileToBase64,
  rejectionReason,
  type PendingAttachment,
} from "./workspace/attachments";
import type {
  Agent,
  Message,
  OrchestrationMode,
  Session,
  SessionSummary,
  Workflow,
  WorkflowStageTrace,
} from "./types/api";

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
/**
 * 时间戳：今天只给时刻，往日补上日期。
 *
 * 实现在 `config/shared.tsx::formatStamp`——原来这里是「只给 HH:mm」，于是历史会话
 * 列表与消息气泡上的时间在跨天之后完全无法区分是哪一天。时间戳的写法一旦分叉，
 * 就会出现「这边带日期、那边不带」的两套观感，所以只留一个实现、一个短名字。
 */
const time = formatStamp;
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
  // 编排模式：默认自动编排（ADR-019 的 `dynamic`）。服务端默认仍是 static，
  // 这里是**前端每次请求显式声明**的取值，输入框右侧随时可切回固定三步。
  const [mode, setMode] = useState<OrchestrationMode>("dynamic");
  // 待发送附件：选文件即上传，提交时只带 id（ADR-021）。
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
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

  /**
   * 选文件即上传（ADR-021）。
   *
   * 提前上传而不是等提交时再传：上传要花时间，失败要重试，两者都该在**输入阶段**
   * 让用户看到，而不是点了发送之后等一个说不清是「在传」还是「在跑」的状态。
   * 提交时只提交已经拿到 id 的那几个，失败的条目留在输入区让用户自己决定。
   */
  const pickFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    // 配对而不是按下标对齐：被拒的文件不会进 `accepted`，按下标取会整体错位，
    // 结果是把 A 的字节当成 B 的名字上传。
    const pairs: { item: PendingAttachment; file: File }[] = [];
    const rejected: string[] = [];
    let slots = MAX_ATTACHMENT_COUNT - attachments.length;
    for (const file of list) {
      const reason = rejectionReason(file, [...attachments, ...pairs.map((p) => p.item)]);
      if (reason || slots <= 0) {
        rejected.push(reason ?? `单条消息最多附带 ${MAX_ATTACHMENT_COUNT} 个附件。`);
        continue;
      }
      slots -= 1;
      pairs.push({
        file,
        item: {
          key: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
          name: file.name,
          size: file.size,
          kind: classifyLocal(file.name),
          state: "uploading",
        },
      });
    }
    if (rejected.length) setError(rejected[0]);
    if (!pairs.length) return;
    setAttachments((current) => [...current, ...pairs.map((pair) => pair.item)]);

    await Promise.all(
      pairs.map(async ({ item, file }) => {
        try {
          const base64 = await fileToBase64(file);
          const uploaded = await api.uploadAttachment(item.name, file.type, base64);
          setAttachments((current) =>
            current.map((entry) =>
              entry.key === item.key
                ? {
                    ...entry,
                    state: uploaded.status === "failed" ? "failed" : "ready",
                    id: uploaded.id,
                    // 服务端解析失败时附件仍然登记成功，但正文取不出来——如实标出来，
                    // 否则用户会以为这份 PDF 被读进去了。
                    error: uploaded.error ?? undefined,
                  }
                : entry,
            ),
          );
        } catch (cause) {
          setAttachments((current) =>
            current.map((entry) =>
              entry.key === item.key
                ? {
                    ...entry,
                    state: "failed",
                    error: cause instanceof Error ? cause.message : "上传失败",
                  }
                : entry,
            ),
          );
        }
      }),
    );
  };

  const removeAttachment = (key: string) => {
    const target = attachments.find((item) => item.key === key);
    setAttachments((current) => current.filter((item) => item.key !== key));
    // 已经登记到服务端的顺手删掉；删失败不影响界面（最坏情况只留一条未归属的附件行）。
    if (target?.id) void api.deleteAttachment(target.id).catch(() => undefined);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ready = attachments.filter((item) => item.state === "ready" && item.id);
    const uploading = attachments.some((item) => item.state === "uploading");
    if (
      session?.status === "paused" ||
      sending ||
      uploading ||
      (!content.trim() && !ready.length) ||
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
      const accepted = await api.sendMessage(sessionId, content.trim(), {
        attachmentIds: ready.map((item) => item.id as string),
        orchestrationMode: mode,
      });
      setContent("");
      setAttachments([]);
      if (accepted.unattached_attachment_ids.length) {
        // 消息发出去了，附件缺了几个 —— 部分失败单独提示，不能并进提交失败里。
        setError(
          `消息已发送，但有 ${accepted.unattached_attachment_ids.length} 个附件没能附上，请重新上传。`,
        );
      }
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
    // 未发出的附件已登记在服务端，换任务时顺手清掉，别留孤儿行。
    attachments.forEach((item) => {
      if (item.id) void api.deleteAttachment(item.id).catch(() => undefined);
    });
    setAttachments([]);
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
              attachments={attachments}
              onPickFiles={pickFiles}
              onRemoveAttachment={removeAttachment}
              mode={mode}
              setMode={setMode}
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
  /** 待发送附件（ADR-021）。上传在选文件时就发生，这里只承载状态。 */
  attachments: PendingAttachment[];
  onPickFiles: (files: FileList | File[]) => void;
  onRemoveAttachment: (key: string) => void;
  /** 编排模式（ADR-019）：`static` 固定三步，`dynamic` 由规划节点按任务分配角色。 */
  mode: OrchestrationMode;
  setMode: (value: OrchestrationMode) => void;
};

type StageId = (typeof stages)[number]["id"];
const responsibilities: Record<StageId, string> = {
  collect: "整理任务要求与输入资料，为后续分析准备信息。",
  analyze: "基于收集结果进行分析，梳理关键结论。",
  report: "整合分析结果，组织结构化报告。",
};

/** 协作模型的阶段元信息：图标与配色属于视图，进模型的只有 id / 名称 / 角色 / 职责。 */
const STAGE_META: StageMeta[] = stages.map((stage) => ({
  id: stage.id,
  label: stage.label,
  agent: stage.agent,
  responsibility: responsibilities[stage.id],
}));
/** 状态词汇（阶段 / 计划步骤）在 `workspace/collaboration.ts`——执行台、协作画布
 *  与阶段弹窗共用同一套判断，`skipped` 与 `pending` 的区分只能有一处实现。 */

/** Agent 目录里不存在的角色不进执行台，也不进协作链路（doc/api.md §7）。 */
function participatingStages(agents: Agent[]): (typeof stages)[number][] {
  return stages.filter((stage) =>
    agents.some((agent) => agent.id === stage.agent),
  );
}

/**
 * 协作链路的节点与连线由 `workspace/collaboration.ts::buildCollaboration` 组装。
 *
 * 原先这里有一份 `collaborationWaves`，只把每个 Agent 摊成一波、不带参数与用量；
 * 侧栏现在要画的是「模型 / 参数 / Token + 工具链路」，同一件事不能再有两个来源，
 * 所以那份实现整体搬进了模型模块（侧栏与全屏画布共用）。
 */

/**
 * 执行台节点：把「静态阶段」与「动态计划步骤」收敛成同一种形状，
 * 渲染分支因此只有一处——否则动态链路一上线，执行台就会空着不动
 * （它按 `stages` 常量渲染，而动态链路的进度字段是 `s1/s2/…`）。
 */
type DockNode = {
  id: string;
  label: string;
  agent: string;
  icon: (typeof stages)[number]["icon"];
  tone: string;
  responsibility: string;
  state: string;
  /** 静态阶段才有，用于打开固定的阶段详情弹窗。 */
  stage: StageId | null;
};

/** 角色 id 反查静态阶段元信息；未知角色给一份兜底，不让执行台缺节点。 */
function stageForRole(role: string) {
  return (
    stages.find((stage) => stage.agent === role) ?? {
      id: role as StageId,
      label: role,
      agent: role,
      icon: Bot,
      tone: "blue",
    }
  );
}

function dockNodes(
  workflow: Workflow | null,
  agents: Agent[],
  completed: Set<string>,
): DockNode[] {
  if (!workflow) return [];
  const plan = planSteps(workflow);
  if (plan.length) {
    return plan.map((step) => {
      const meta = stageForRole(step.role);
      return {
        id: step.id,
        label: `${meta.label} · ${step.id}`,
        agent: meta.agent,
        icon: meta.icon,
        tone: meta.tone,
        responsibility:
          step.depends_on.length > 0
            ? `由规划 Agent 指派，依赖 ${step.depends_on.join("、")}`
            : "由规划 Agent 指派，直接接收用户任务",
        state: planStepStatus(step),
        stage: null,
      };
    });
  }
  return participatingStages(agents).map((stage) => ({
    id: stage.id,
    label: stage.label,
    agent: stage.agent,
    icon: stage.icon,
    tone: stage.tone,
    responsibility: responsibilities[stage.id],
    state: stageStatus(stage.id, workflow, completed),
    stage: stage.id,
  }));
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
  attachments,
  onPickFiles,
  onRemoveAttachment,
  mode,
  setMode,
}: WorkspaceProps) {
  const stream = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);
  const followLatest = useRef(true);
  const [currentMessage, setCurrentMessage] = useState("");
  // 执行台卡片点开的是「单个 Agent 的阶段详情」，与右侧任务级侧栏解耦。
  const [detailStage, setDetailStage] = useState<StageId | null>(null);
  // 动态链路的节点 id 是计划步骤（s1/s2…），不属于固定的 `StageId` 集合，
  // 因此单独存一份已组装好的详情，避免为一个新形态去放宽既有的阶段类型。
  const [detailNode, setDetailNode] = useState<AgentStageDetail | null>(null);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const busy =
    sending || workflow?.status === "running" || workflow?.status === "pending";
  // 执行台把当前阶段置顶，便于执行中一眼看到谁在跑；协作链路视图仍按真实顺序渲染。
  const runtimeNodes = dockNodes(workflow, agents, completed).sort((left, right) =>
    left.id === activeStage ? -1 : right.id === activeStage ? 1 : 0,
  );
  const detail =
    detailNode ?? (detailStage ? stageDetail(detailStage, workflow, agents, completed) : null);
  const uploading = attachments.some((item) => item.state === "uploading");

  /* ---------------------------------------------------------------------- */
  /* 阶段执行轨迹（§5.17）：随 Workflow 轮询刷新                              */
  /*                                                                        */
  /* 不再「弹窗打开才拉」：侧栏的协作卡片也要用这份轨迹画工具链路，两处各拉一次    */
  /* 会变成同一接口双倍请求，还可能落在不同批次上、卡片与弹窗对不上。             */
  /* ---------------------------------------------------------------------- */

  const [traces, setTraces] = useState<WorkflowStageTrace | null>(null);
  const [traceError, setTraceError] = useState("");
  const [traceLoading, setTraceLoading] = useState(false);
  const detailStageId = detail?.stageId ?? null;
  const workflowId = workflow?.id ?? null;
  // 依赖用**实测变化的原始值**：Workflow 轮询在终态停止，所以这里只在阶段推进时重拉。
  const workflowRevision = workflow?.updated_at ?? "";

  useEffect(() => {
    if (!workflowId) {
      setTraces(null);
      setTraceError("");
      setTraceLoading(false);
      return;
    }
    let live = true;
    setTraceLoading(true);
    api
      .getWorkflowStages(workflowId)
      .then((data) => {
        if (!live) return;
        setTraces(data);
        setTraceError("");
      })
      .catch((cause) => {
        if (!live) return;
        setTraces(null);
        setTraceError(
          cause instanceof Error ? cause.message : "无法读取该 Agent 的执行轨迹",
        );
      })
      .finally(() => {
        if (live) setTraceLoading(false);
      });
    return () => {
      live = false;
    };
  }, [workflowId, workflowRevision]);

  const activeTrace =
    traces?.items.find((item) => item.stage === detailStageId) ?? null;
  // 整条链路都没有分阶段轨迹（目前只有动态编排）时，原因说在弹窗主体里，
  // 而不是让每个阶段各自显示一句「暂无记录」。
  const traceGap = traces && !traces.items.length ? (traces.reason ?? "") : "";

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
  function choosePrompt(prompt: string, promptMode: OrchestrationMode) {
    setContent(prompt);
    // 卡片承诺的是「按任务分配角色」，所以必须同时把编排模式切过去——
    // 只填文字不换模式，卡片上的协作形态就是一句在默认链路下不成立的话。
    setMode(promptMode);
    composer.current?.focus();
  }
  function pickFiles(files: FileList | File[]) {
    if (!files || !("length" in files) || !files.length) return;
    onPickFiles(files);
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
                          {planSteps(workflow).length
                            ? `自动编排 · 计划 ${planSteps(workflow).length} 步，已完成 ${completed.size} 步`
                            : `已完成 ${completed.size} 个阶段`}
                          {" · 可在下方查看执行状态"}
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
                <Welcome onChoose={choosePrompt} mode={mode} />
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
          className={`conversation-composer ${composerExpanded ? "expanded" : ""} ${dragging ? "dragging" : ""}`}
          onSubmit={(e) => {
            followLatest.current = true;
            submit(e);
          }}
          onDragOver={(e) => {
            // 只在真的拖了文件时才进入拖放态，否则会把文本拖选也变成「松手即上传」。
            if (!Array.from(e.dataTransfer.types).includes("Files")) return;
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={(e) => {
            if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
            setDragging(false);
          }}
          onDrop={(e) => {
            if (!e.dataTransfer.files?.length) return;
            e.preventDefault();
            setDragging(false);
            pickFiles(e.dataTransfer.files);
          }}
        >
          <button type="button" className="composer-resize" aria-label={composerExpanded ? "收起输入框" : "展开输入框"} onClick={() => setComposerExpanded((expanded) => !expanded)}><Maximize2 size={13} /></button>
          <PendingFileChips items={attachments} onRemove={onRemoveAttachment} />
          <textarea
            ref={composer}
            aria-label="任务输入"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="描述任务，上传图片或文档，或继续补充你的想法…"
            disabled={session?.status === "paused"}
            onPaste={(e) => {
              // 截图直接粘贴是最常见的收图方式，比先存盘再选文件少两步。
              const files = Array.from(e.clipboardData?.files ?? []);
              if (!files.length) return;
              e.preventDefault();
              pickFiles(files);
            }}
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
              <input
                ref={filePicker}
                type="file"
                multiple
                accept={ATTACHMENT_ACCEPT}
                hidden
                onChange={(e) => {
                  if (e.target.files) pickFiles(e.target.files);
                  // 清空 value：否则连续选同一个文件不会再触发 change。
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                className="composer-attach"
                aria-label="添加附件"
                disabled={session?.status === "paused" || attachments.length >= MAX_ATTACHMENT_COUNT}
                onClick={() => filePicker.current?.click()}
              >
                <Paperclip size={14} />
                附件
              </button>
              <small>
                {session?.status === "paused"
                  ? "会话已暂停"
                  : busy
                    ? "等待本次执行完成"
                    : attachments.length
                      ? `已附 ${attachments.length}/${MAX_ATTACHMENT_COUNT} 个文件 · 图片需所选模型支持视觉`
                      : "可拖拽或粘贴图片/文档 · Enter 发送 · Shift + Enter 换行"}
              </small>
            </div>
            <div className="composer-mode" role="group" aria-label="编排模式">
              {([
                ["dynamic", "自动编排", "由规划 Agent 按任务决定谁参与"],
                ["static", "固定三步", "始终走 收集 → 分析 → 报告"],
              ] as const).map(([value, label, hint]) => (
                <button
                  key={value}
                  type="button"
                  className={mode === value ? "active" : ""}
                  aria-pressed={mode === value}
                  title={hint}
                  onClick={() => setMode(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <button
              className="primary-button"
              aria-label="发送任务"
              disabled={
                busy ||
                uploading ||
                session?.status === "paused" ||
                (!content.trim() && !attachments.some((item) => item.state === "ready"))
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
        agents={agents}
        completed={completed}
        traces={traces}
        messages={messages}
        sessionId={session?.id ?? null}
        onOpenRecords={onOpenRecords}
      />
      </section>
      {detail && (
        <AgentStageModal
          detail={detail}
          trace={activeTrace}
          traceLoading={traceLoading}
          traceError={traceError}
          overallReason={traceGap}
          onClose={() => {
            setDetailStage(null);
            setDetailNode(null);
          }}
          onOpenRecords={() => {
            setDetailStage(null);
            setDetailNode(null);
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
                ? `${completed.size} / ${runtimeNodes.length} 完成`
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
            {runtimeNodes.map((node) => {
              const Icon = node.icon;
              const agent = agents.find((entry) => entry.id === node.agent);
              const selected = detail?.stageId === node.id;
              const stepText =
                node.state === "running"
                  ? "正在执行当前步骤"
                  : node.state === "completed"
                    ? "步骤已完成，检查点已保存"
                    : node.state === "failed"
                      ? "步骤执行失败，下游已跳过"
                      : node.state === "skipped"
                        ? "上游未成功，本步已跳过"
                        : "等待前置步骤完成";
              return (
                <button
                  key={node.id}
                  className={`dock-node cli-node ${node.tone} ${node.state} ${selected ? "selected" : ""}`}
                  aria-label={`查看${agent?.name ?? `${node.label} Agent`}的步骤详情`}
                  aria-pressed={selected}
                  onClick={() => {
                    if (node.stage) {
                      setDetailNode(null);
                      setDetailStage(node.stage);
                      return;
                    }
                    // 动态链路的步骤 id 不属于固定的 StageId 集合，直接组装详情。
                    setDetailStage(null);
                    setDetailNode({
                      stageId: node.id,
                      stageLabel: node.label,
                      responsibility: node.responsibility,
                      status: node.state,
                      agent: agent ?? null,
                      updatedAt: workflow.updated_at,
                    });
                  }}
                >
                  <span className="cli-node-head">
                    <span className="dock-icon">{node.state === "completed" ? <Check size={17} /> : node.state === "running" ? <LoaderCircle size={17} className="spin" /> : <Icon size={17} />}</span>
                    <span className="cli-node-title"><strong>{agent?.name ?? `${node.agent} Agent`}</strong><small>{agent?.model ?? "模型由运行时提供"}</small></span>
                    <Status status={node.state} />
                  </span>
                  <span className="cli-step"><i className={node.state === "running" ? "pulse" : ""} />{stepText}</span>
                  <span className="cli-log"><code>{node.state === "running" ? ">" : "$"}</code><span>{node.state === "completed" ? "checkpoint.persisted" : `${node.label} · ${node.responsibility}`}</span></span>
                  <span className="cli-meta"><span><Clock3 size={12} />{time(workflow.updated_at)}</span><span><Zap size={12} />{node.state === "completed" ? "已记录" : "监听中"}</span><ChevronRight size={14} /></span>
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
/**
 * 空白会话的引导区。
 *
 * 三张卡片是**任务原型**，不是三个功能按钮：它们各自代表一类协作形态，
 * 点下去会把问题填进输入框、同时把编排模式切到「自动编排」——
 * 卡片上写的「通常 1 个 Agent 直答」「调查 → 分析 → 总结」只有在规划 Agent
 * 真的参与判断时才成立，所以切模式是卡片语义的一部分，不是附带的方便操作。
 *
 * 当前生效的模式在卡片下方**显式写出**，用户随时能在输入框右侧改回来；
 * 宁可让这句提示显得啰嗦，也不要让卡片承诺一件当前链路不会做的事。
 */
function Welcome({
  onChoose,
  mode,
}: {
  onChoose: (value: string, mode: OrchestrationMode) => void;
  mode: OrchestrationMode;
}) {
  const prompts = [
    {
      icon: MessageSquareText,
      title: "直接提问",
      text: "解释一下什么是幂等，并给一个前端场景里的例子。",
      shape: "通常 1 个 Agent 直答",
      tone: "blue",
    },
    {
      icon: Search,
      title: "需要检索的问题",
      text: "查一下最近一周医药板块的行情变化，并说明可能的原因。",
      shape: "调查 → 分析 → 总结",
      tone: "amber",
    },
    {
      icon: FileText,
      title: "资料整理与长文",
      text: "阅读我上传的附件，归纳主题、列出关键结论，并指出还需要补充什么。",
      shape: "多步核对与整理",
      tone: "green",
    },
  ];
  return (
    <div className="welcome">
      <div className="welcome-icon">
        <Network size={32} strokeWidth={1.5} />
      </div>
      <h2>我们一起完成什么？</h2>
      <p>写下目标；简单的直接回答，复杂的交给多个 Agent 接力。</p>
      <div className="suggestions">
        {prompts.map((p) => (
          <button
            key={p.title}
            type="button"
            className={`suggestion ${p.tone}`}
            onClick={() => onChoose(p.text, "dynamic")}
          >
            <span className="suggestion-icon">
              <p.icon size={20} />
            </span>
            <b>{p.title}</b>
            <span className="suggestion-shape">{p.shape}</span>
          </button>
        ))}
      </div>
      <p className="welcome-note">
        {mode === "dynamic"
          ? "当前：自动编排 — 规划 Agent 先判断需要哪些角色，再按依赖逐步执行。"
          : "当前：固定三步 — 始终走 收集 → 分析 → 报告。选任意卡片会切到自动编排。"}
      </p>
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
        {/* 只带附件不带文字的消息是合法的（截图提问），此时不渲染空的正文段落。 */}
        {message.content ? <p>{message.content}</p> : null}
        <MessageAttachmentList items={message.attachments ?? []} />
      </div>
    </article>
  );
}
/**
 * 「任务协作」侧栏。
 *
 * 三块视图职责互斥（ADR-018，`doc/api.md` §7）：
 *
 * - **本侧栏**：任务级的整体状态与运行时长，加上每个 Agent「用什么跑、花了多少」——
 *   生效模型、参数与 Token 消耗。侧栏卡片**不是**执行轨迹的入口：它点了不跳弹窗，
 *   否则「谁在干」和「用什么干的」又会被混成同一个东西。
 * - **Agent 执行台卡片弹窗**：单个 Agent 收到了什么、调了什么工具、产出了什么（§5.17）。
 * - **任务记录页**：逐条采样与工具调用明细的唯一入口，侧栏只给入口。
 *
 * 「协作链路」升级为协作画布：节点是 Agent 卡片，连线记录**上游**那一步动过的工具，
 * 并可展开全屏——全屏里按「对话编号」回看本会话每一次提问各自跑出的工作流。
 */
function Inspector({
  open,
  onClose,
  workflow,
  agents,
  completed,
  traces,
  messages,
  sessionId,
  onOpenRecords,
}: {
  open: boolean;
  onClose: () => void;
  workflow: Workflow | null;
  /** Agent 目录：协作卡片上的模型、参数都取自它。 */
  agents: Agent[];
  completed: Set<string>;
  /** 当前 Workflow 的阶段执行轨迹，与执行台弹窗**共用同一份**。 */
  traces: WorkflowStageTrace | null;
  /** 会话消息，用来给每次对话贴「任务 N」的标签。 */
  messages: Message[];
  sessionId: string | null;
  onOpenRecords: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Workflow[]>([]);
  const [otherTraces, setOtherTraces] = useState<WorkflowStageTrace | null>(null);
  const running = workflow?.status === "running";
  // 执行中的「运行时长」需要自己走秒：轮询只在 workflow 有变化时回来。
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  /** 当前对话的用量采样：协作卡片与下方用量面板共用，避免同一接口拉两次。 */
  const live = useWorkflowMetrics(workflow);
  const graph = buildCollaboration({
    stages: STAGE_META,
    agents,
    workflow,
    completed,
    traces,
    metrics: live.metrics,
  });

  /* ---------------------------------------------------------------------- */
  /* 全屏画布：默认看当前对话，选中别的对话时另拉那一份轨迹与用量              */
  /* ---------------------------------------------------------------------- */

  const selected = conversations.find((item) => item.id === selectedId) ?? null;
  const canvasWorkflow = selected ?? workflow;
  const isLive = !selected || selected.id === workflow?.id;
  // hook 不能条件调用，所以「选了别的对话」时才把 workflow 递给它（否则传 null 即空转）。
  const other = useWorkflowMetrics(isLive ? null : canvasWorkflow);
  const canvasMetrics = isLive ? live.metrics : other.metrics;
  const canvasTraces = isLive ? traces : otherTraces;
  const canvasCompleted = new Set(
    canvasWorkflow?.checkpoint?.completed_steps ?? [],
  );
  const canvasGraph = buildCollaboration({
    stages: STAGE_META,
    agents,
    workflow: canvasWorkflow,
    completed: canvasCompleted,
    traces: canvasTraces,
    metrics: canvasMetrics,
  });

  // 打开画布时锚到当前对话；`updated_at` 一并作依赖，让新任务跑完能出现在列表里。
  useEffect(() => {
    if (canvasOpen) setSelectedId(workflow?.id ?? null);
  }, [canvasOpen, workflow?.id]);

  useEffect(() => {
    if (!canvasOpen || !sessionId) {
      setConversations([]);
      return;
    }
    let live2 = true;
    api
      .getSessionWorkflows(sessionId)
      .then((page) => {
        if (live2) setConversations(page.items);
      })
      .catch(() => {
        // 列表拉不到不影响看当前对话：画布退化成「只有当前这一次」而不是报错空白。
        if (live2) setConversations([]);
      });
    return () => {
      live2 = false;
    };
  }, [canvasOpen, sessionId, workflow?.updated_at]);

  useEffect(() => {
    if (!canvasWorkflow || isLive) {
      setOtherTraces(null);
      return;
    }
    let live2 = true;
    api
      .getWorkflowStages(canvasWorkflow.id)
      .then((data) => {
        if (live2) setOtherTraces(data);
      })
      .catch(() => {
        if (live2) setOtherTraces(null);
      });
    return () => {
      live2 = false;
    };
  }, [canvasWorkflow?.id, isLive]);

  /** 对话编号沿用历史列表的「任务 N」口径：同一轮提问的 workflow 才配得到编号。 */
  const turns = messages.filter((message) => message.role === "user");
  const conversationItems: CollabConversation[] = conversations.map((item, index) => {
    const turn = turns.find(
      (message) => message.agent_run_id && message.agent_run_id === item.agent_run_id,
    );
    const label = (turn?.content ?? `第 ${index + 1} 轮任务`).replace(/\s+/g, " ").trim();
    return {
      id: item.id,
      index: index + 1,
      label: label.length > 40 ? `${label.slice(0, 40)}…` : label,
      status: item.status,
    };
  });

  const total = graph.nodes.length;
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
        <p className="inspector-copy">
          节点是 Agent 卡片：各自的模型、参数与 Token 消耗。连线记录上游那一步用过的工具。
          想看某个 Agent 具体干了什么，在执行台卡片上点开。
        </p>
        <CollaborationGraph graph={graph} onExpand={() => setCanvasOpen(true)} />
      </section>
      {workflow && (
        <section className="inspector-panel">
          <div className="inspector-head">
            <span className="eyebrow">
              <Gauge size={13} />
              任务用量
            </span>
          </div>
          <TaskUsagePanel
            groups={groupUsage(live.metrics)}
            loading={live.loading}
            error={live.error}
            onOpenRecords={onOpenRecords}
          />
        </section>
      )}
      <CollabCanvas
        open={canvasOpen}
        graph={canvasGraph}
        conversations={conversationItems}
        selectedId={canvasWorkflow?.id ?? null}
        onSelect={setSelectedId}
        loading={isLive ? false : other.loading}
        error={isLive ? "" : other.error}
        onClose={() => setCanvasOpen(false)}
      />
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

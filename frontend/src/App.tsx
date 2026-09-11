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
  CirclePause,
  Clock3,
  Database,
  FileText,
  Gauge,
  Hammer,
  LoaderCircle,
  Maximize2,
  MessageSquareText,
  Network,
  PanelBottom,
  PanelLeft,
  PanelRight,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  ShieldCheck,
  SquareTerminal,
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
import { RuntimeConfig, WorkflowInspection } from "./Inspection";
import type { Agent, Message, Session, Workflow } from "./types/api";

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
const statusText: Record<string, string> = {
  pending: "排队中",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};
const time = (v?: string) =>
  v
    ? new Date(v).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
type View = "workspace" | "history" | "team" | "tools";

function MorphStateIcon({ state, size = 16 }: { state: "menu" | "close" | "play" | "pause" | "up" | "down" | "panel" | "panel-open"; size?: number }) {
  const icons = { menu: MenuData, close: XData, play: PlayData, pause: PauseData, up: ArrowUpData, down: ArrowDownData, panel: PanelRightData, "panel-open": PanelBottomData };
  return <MorphIcon icon={(icons[state] as any)[2]} size={size} spring="snappy" reducedMotion="user" />;
}

export function App() {
  const [view, setView] = useState<View>("workspace");
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
  useEffect(() => {
    void (async () => {
      try {
        const [a, s] = await Promise.all([
          api.getAgents(),
          api.createSession(),
        ]);
        setAgents(a);
        setSession(s);
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
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (
      !session ||
      session.status === "paused" ||
      sending ||
      !content.trim() ||
      workflow?.status === "running" ||
      workflow?.status === "pending"
    )
      return;
    setSending(true);
    setError("");
    try {
      const accepted = await api.sendMessage(session.id, content.trim());
      setContent("");
      setWorkflow(await api.getWorkflow(accepted.workflow_id));
      setMessages(await api.getMessages(session.id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "任务提交失败");
    } finally {
      setSending(false);
    }
  };
  const createNewTask = async () => {
    setError("");
    try {
      const nextSession = await api.createSession();
      setSession(nextSession);
      setMessages([]);
      setWorkflow(null);
      setContent("");
      setView("workspace");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法创建新任务");
    }
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
        <div className="sidebar-user">
          <span className="sidebar-user-avatar">D</span>
          <span><b>演示用户</b><small>当前账户</small></span>
        </div>
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
            active={view === "history"}
            onClick={() => {
              setView("history");
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
              <button className="new-task-button" type="button" onClick={() => void createNewTask()}><span>＋</span>新建任务</button>
            ) : (
              <div className="crumb"><b>{view === "history" ? "任务记录" : view === "team" ? "Agent 团队" : "工具与配置"}</b></div>
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
            />
          )}
          {view === "history" && (
            <History
              workflow={workflow}
              messages={messages}
              onOpen={() => setView("workspace")}
            />
          )}
          {view === "team" && (
            <Team agents={agents} activeAgent={activeAgent} />
          )}
          {view === "tools" && <RuntimeConfig />}
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
}: WorkspaceProps) {
  const stream = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const followLatest = useRef(true);
  const [currentMessage, setCurrentMessage] = useState("");
  const [selectedStage, setSelectedStage] = useState<StageId>("collect");
  const [decisionAgentId, setDecisionAgentId] = useState<string | "auto">("auto");
  const [decisionMenuOpen, setDecisionMenuOpen] = useState(false);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const selectedStatus = stageStatus(selectedStage, workflow, completed);
  const busy =
    sending || workflow?.status === "running" || workflow?.status === "pending";
  const runtimeStages = workflow
    ? stages
        .filter((stage) => agents.some((agent) => agent.id === stage.agent))
        .sort((left, right) => (left.id === activeStage ? -1 : right.id === activeStage ? 1 : 0))
    : [];

  useEffect(() => {
    if (activeStage && stages.some((s) => s.id === activeStage))
      setSelectedStage(activeStage as StageId);
  }, [activeStage]);
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
            disabled={!session || session.status === "paused"}
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
            <div className="decision-control">
              <button
                type="button"
                className="decision-agent-selector"
                aria-label="选择主决策 Agent"
                aria-expanded={decisionMenuOpen}
                onClick={() => setDecisionMenuOpen((open) => !open)}
              >
                <Bot size={14} />
                <span>
                  {decisionAgentId === "auto"
                    ? "自动选择主决策 Agent"
                    : agents.find((agent) => agent.id === decisionAgentId)?.name ??
                      "选择主决策 Agent"}
                </span>
                <ChevronDown
                  size={13}
                  className={decisionMenuOpen ? "flip" : ""}
                />
              </button>
              {decisionMenuOpen && (
                <div className="decision-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    className={decisionAgentId === "auto" ? "selected" : ""}
                    onClick={() => {
                      setDecisionAgentId("auto");
                      setDecisionMenuOpen(false);
                    }}
                  >
                    <Bot size={14} /> 自动分配
                  </button>
                  {agents.map((agent) => (
                    <button
                      type="button"
                      role="menuitem"
                      className={decisionAgentId === agent.id ? "selected" : ""}
                      key={agent.id}
                      onClick={() => {
                        setDecisionAgentId(agent.id);
                        setDecisionMenuOpen(false);
                      }}
                    >
                      <Bot size={14} /> {agent.name}
                    </button>
                  ))}
                  <small>仅用于界面选择，主决策 Agent 接口尚未接入。</small>
                </div>
              )}
              <small>
                {session?.status === "paused"
                  ? "会话已暂停"
                  : busy
                    ? "等待本次执行完成"
                    : "Enter 发送 · Shift + Enter 换行"}
              </small>
            </div>
            <button
              className="primary-button"
              aria-label="发送任务"
              disabled={
                busy ||
                !session ||
                !content.trim() ||
                session.status === "paused"
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
        selectedStage={selectedStage}
        selectedStatus={selectedStatus}
        onSelect={setSelectedStage}
      />
      </section>
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
                  className={`dock-node cli-node ${s.tone} ${state} ${selectedStage === s.id ? "selected" : ""}`}
                  aria-label={`查看${s.label} Agent`}
                  aria-pressed={selectedStage === s.id}
                  onClick={() => { setSelectedStage(s.id); setInspectorOpen(true); }}
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
function Inspector({
  open,
  onClose,
  workflow,
  agents,
  selectedStage,
  selectedStatus,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  workflow: Workflow | null;
  agents: Agent[];
  selectedStage: StageId;
  selectedStatus: string;
  onSelect: (stage: StageId) => void;
}) {
  const selected = stages.find((item) => item.id === selectedStage) ?? stages[0];
  const selectedAgent = agents.find((item) => item.id === selected.agent);
  return (
    <aside
      className={`inspector ${open ? "open" : ""}`}
      aria-label="Agent 详情"
    >
      <header className="inspector-title">
        <span>
          <Bot size={16} />
          Agent 详情
        </span>
        <button
          className="icon-button inspector-close"
          aria-label="关闭 Agent 详情"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </header>
      <section className="inspector-panel collaboration-panel">
        <div className="inspector-head">
          <span className="eyebrow">本次协作</span>
          <Status status={workflow?.status ?? "idle"} />
        </div>
        <h3>{workflow ? "协作成员与工具" : "等待任务"}</h3>
        <p className="inspector-copy">{workflow ? "按执行顺序记录本次任务涉及的 Agent。" : "任务开始后，这里会显示实际参与的 Agent 和工具。"}</p>
        {workflow && <div className="collaboration-list">{stages.map((item) => {
          const agent = agents.find((entry) => entry.id === item.agent);
          const state = stageStatus(item.id, workflow, new Set(workflow.checkpoint?.completed_steps ?? []));
          const expanded = selectedStage === item.id;
          return <div className={`collaboration-agent ${expanded ? "expanded" : ""}`} key={item.id}>
            <button onClick={() => onSelect(item.id)} aria-expanded={expanded}>
              <span className={`agent-mini-icon ${item.tone}`}><item.icon size={14} /></span>
              <span><b>{agent?.name ?? `${item.agent} Agent`}</b><small>{item.label}</small></span>
              <Status status={state} />
              <ChevronDown size={14} className={expanded ? "flip" : ""} />
            </button>
            {expanded && <dl className="agent-facts compact-facts"><div><dt>模型</dt><dd>{agent?.model ?? "未提供"}</dd></div><div><dt>阶段状态</dt><dd>{statusText[state] ?? state}</dd></div><div><dt>时间记录</dt><dd>{time(workflow.updated_at)}</dd></div><div><dt>Token 消耗</dt><dd>见下方本次任务采样</dd></div></dl>}
          </div>;
        })}</div>}
      </section>
      <section className="inspector-panel">
        <div className="inspector-head">
          <span className="eyebrow">
            <Activity size={13} />
            阶段执行
          </span>
        </div>
        <dl className="agent-facts">
          <div>
            <dt>阶段状态</dt>
            <dd>{workflow ? (statusText[selectedStatus] ?? selectedStatus) : "尚未开始"}</dd>
          </div>
          <div>
            <dt>检查点</dt>
            <dd>
              {workflow?.checkpoint?.completed_steps?.includes(selectedStage)
                ? "已保存"
                : "暂无已完成记录"}
            </dd>
          </div>
          <div>
            <dt>最近更新</dt>
            <dd>{time(workflow?.updated_at)}</dd>
          </div>
        </dl>
        <p className="inspector-copy">
          下方展示本次 Workflow 的工具调用与指标采样。
        </p>
      </section>
      {workflow && <WorkflowInspection key={workflow.id} workflow={workflow} />}
    </aside>
  );
}
function Status({ status }: { status: string }) {
  return (
    <span className={`status-pill ${status}`}>
      <i />
      {status === "idle"
        ? "待命"
        : status === "pending"
          ? "等待"
          : (statusText[status] ?? status)}
    </span>
  );
}
function History({
  workflow,
  messages,
  onOpen,
}: {
  workflow: Workflow | null;
  messages: Message[];
  onOpen: () => void;
}) {
  return (
    <>
      <section className="page-heading">
        <span className="eyebrow">
          <Clock3 size={13} />
          RUN HISTORY
        </span>
        <h1>任务记录</h1>
        <p>当前会话最近一次执行。完整历史查询尚未接入。</p>
      </section>
      <section className="history-panel">
        {workflow ? (
          <button className="history-row" onClick={onOpen}>
            <div>
              <b>协作任务 {workflow.id.slice(0, 8)}</b>
              <small>Agent 团队三节点协作</small>
            </div>
            <Status status={workflow.status} />
            <span>{messages.length} 条消息</span>
            <ChevronRight size={16} />
          </button>
        ) : (
          <div className="empty-panel">
            <Clock3 size={20} />
            <b>还没有任务记录</b>
            <span>提交第一个任务后，它会显示在这里。</span>
          </div>
        )}
      </section>
    </>
  );
}
function Team({
  agents,
  activeAgent,
}: {
  agents: Agent[];
  activeAgent?: Agent;
}) {
  return (
    <>
      <section className="page-heading">
        <span className="eyebrow">
          <UsersRound size={13} />
          AGENT DIRECTORY
        </span>
        <h1>Agent 团队</h1>
        <p>默认演示团队；展示 API 进程当前 Provider 与模型配置。</p>
      </section>
      <div className="team-grid">
        {agents.map((a, i) => (
          <article
            className={`team-card ${activeAgent?.id === a.id ? "active" : ""}`}
            key={a.id}
          >
            <div className="team-top">
              <span>0{i + 1}</span>
              <em>
                <i />
                {activeAgent?.id === a.id ? "当前阶段" : "待命"}
              </em>
            </div>
            <div className="team-avatar">
              <Bot size={22} />
            </div>
            <h2>{a.name}</h2>
            <p>{a.role}</p>
            <div className="team-model">
              <SquareTerminal size={14} />
              {a.provider} · {a.model} · Temperature {a.temperature}
            </div>
          </article>
        ))}
      </div>
    </>
  );
}

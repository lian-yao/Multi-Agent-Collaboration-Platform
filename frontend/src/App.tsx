import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api/client";
import type { Agent, Message, Session, Workflow } from "./types/api";

const stages = ["collect", "analyze", "report"] as const;
const labels: Record<string, string> = {
  collect: "信息收集",
  analyze: "数据分析",
  report: "报告生成",
};

const formatTime = (value?: string) =>
  value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "—";

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!session) return;
    const [freshSession, freshMessages] = await Promise.all([
      api.getSession(session.id),
      api.getMessages(session.id),
    ]);
    setSession(freshSession);
    setMessages(freshMessages);
    if (workflow) setWorkflow(await api.getWorkflow(workflow.id));
  }, [session, workflow]);

  useEffect(() => {
    const bootstrap = async () => {
      try {
        const [freshAgents, freshSession] = await Promise.all([
          api.getAgents(),
          api.createSession(),
        ]);
        setAgents(freshAgents);
        setSession(freshSession);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "无法连接后端服务");
      } finally {
        setLoading(false);
      }
    };
    void bootstrap();
  }, []);

  useEffect(() => {
    if (!workflow || workflow.status === "completed" || workflow.status === "failed") return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [workflow, refresh]);

  const completed = useMemo(
    () => new Set(workflow?.checkpoint?.completed_steps ?? []),
    [workflow],
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !content.trim()) return;
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

  const toggleSession = async () => {
    if (!session) return;
    try {
      const result = session.status === "paused"
        ? await api.resumeSession(session.id)
        : await api.pauseSession(session.id);
      setSession(result.session);
      if (result.workflow) setWorkflow(result.workflow);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "状态切换失败");
    }
  };

  if (loading) return <div className="splash">正在加载 Agent 协作工作台…</div>;

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">◈</span><div><p className="eyebrow">D5—D6 / COLLABORATION CONSOLE</p><h1>Agent 协作工作台</h1></div></div>
        <div className="top-actions"><span className={`connection ${error ? "offline" : ""}`}><i />{error ? "服务异常" : "系统在线"}</span><button className="ghost-button" onClick={() => void refresh()}>刷新状态</button></div>
      </header>

      {error && <div className="alert">{error}</div>}

      <section className="dashboard-grid">
        <aside className="panel session-panel">
          <div className="panel-heading"><div><p className="eyebrow">SESSION</p><h2>当前会话</h2></div><span className={`status-pill ${session?.status}`}>{session?.status === "paused" ? "已暂停" : "进行中"}</span></div>
          <div className="session-id">{session?.id ?? "尚未创建"}</div>
          <div className="session-meta"><span>用户</span><strong>{session?.user_id ?? "demo-user"}</strong></div>
          <div className="session-meta"><span>创建时间</span><strong>{formatTime(session?.created_at)}</strong></div>
          <button className="outline-button wide" onClick={() => void toggleSession()} disabled={!session}>{session?.status === "paused" ? "恢复会话" : "暂停会话"}</button>
          <div className="history-title"><span>消息历史</span><span>{messages.length} 条</span></div>
          <div className="message-list">{messages.length === 0 ? <p className="empty">发送一条任务，开始协作。</p> : messages.map((message) => <div className="history-item" key={message.id}><span className={`role-dot ${message.role}`} /><div><strong>{message.role === "user" ? "你" : message.role}</strong><p>{message.content}</p></div><time>{formatTime(message.created_at)}</time></div>)}</div>
        </aside>

        <section className="panel workflow-panel">
          <div className="panel-heading"><div><p className="eyebrow">WORKFLOW STATUS</p><h2>任务执行</h2></div>{workflow && <span className={`status-pill ${workflow.status}`}>{workflow.status}</span>}</div>
          <div className="workflow-id">{workflow ? `WORKFLOW / ${workflow.id}` : "等待提交任务"}</div>
          <div className="stage-track">{stages.map((stage, index) => { const active = workflow?.current_step === stage; const done = completed.has(stage); return <div className={`stage ${done ? "done" : ""} ${active ? "active" : ""}`} key={stage}><div className="stage-node">{done ? "✓" : `0${index + 1}`}</div><div><strong>{labels[stage]}</strong><span>{done ? "已完成" : active ? "执行中" : "等待中"}</span></div>{index < stages.length - 1 && <div className="stage-line" />}</div>; })}</div>
          <div className="checkpoint-card"><div><span className="muted">当前阶段</span><strong>{workflow?.current_step ? labels[workflow.current_step] ?? workflow.current_step : "—"}</strong></div><div><span className="muted">最近更新</span><strong>{formatTime(workflow?.updated_at)}</strong></div><div><span className="muted">完成步骤</span><strong>{workflow?.checkpoint?.completed_steps?.length ?? 0} / 3</strong></div></div>
          <form className="task-form" onSubmit={submit}><div className="input-label">提交新的协作任务</div><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="例如：分析一篇技术文章并生成结构化报告" disabled={!session || session.status === "paused"} /><button className="primary-button" disabled={sending || !content.trim() || session?.status === "paused"}>{sending ? "提交中…" : "启动协作任务 →"}</button></form>
        </section>

        <aside className="panel team-panel"><div className="panel-heading"><div><p className="eyebrow">AGENT TEAM</p><h2>团队状态</h2></div><span className="team-count">{agents.length} AGENTS</span></div><div className="agent-list">{agents.map((agent) => <div className="agent-card" key={agent.id}><div className={`agent-avatar ${agent.role}`}>{agent.role.slice(0, 1).toUpperCase()}</div><div className="agent-copy"><strong>{agent.name}</strong><span>{agent.role} · {agent.model}</span></div><span className="agent-state"><i />{agent.status === "idle" ? "空闲" : agent.status}</span></div>)}</div><div className="team-note"><span>PIPELINE MODE</span><strong>固定三步协作</strong><p>信息收集 → 数据分析 → 报告生成</p></div></aside>
      </section>
      <footer><span>LANGGRAPH × DAPR WORKFLOW</span><span>轮询间隔 2s · API /api/v1</span></footer>
    </main>
  );
}

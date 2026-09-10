import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api/client";
import type { Agent, Message, Session, Workflow } from "./types/api";

const stages = ["collect", "analyze", "report"] as const;
const labels: Record<string, string> = { collect: "信息收集", analyze: "数据分析", report: "报告生成" };
const statusText: Record<string, string> = { pending: "排队中", running: "执行中", paused: "已暂停", completed: "已完成", failed: "失败", cancelled: "已取消" };
const formatTime = (value?: string) => value ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  // 终态后只补刷一次消息，避免 workflow 对象每次刷新都触发新一轮定时器。
  const finalRefreshDone = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    if (!session) return;
    try {
      const [freshSession, freshMessages] = await Promise.all([api.getSession(session.id), api.getMessages(session.id)]);
      setSession(freshSession); setMessages(freshMessages);
      if (workflow) setWorkflow(await api.getWorkflow(workflow.id));
      setError("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法刷新任务状态"); }
  }, [session, workflow]);

  useEffect(() => {
    const bootstrap = async () => {
      try { const [freshAgents, freshSession] = await Promise.all([api.getAgents(), api.createSession()]); setAgents(freshAgents); setSession(freshSession); }
      catch (cause) { setError(cause instanceof Error ? cause.message : "无法连接后端服务"); }
      finally { setLoading(false); }
    };
    void bootstrap();
  }, []);

  useEffect(() => {
    if (!workflow) return;
    if (workflow.status === "completed" || workflow.status === "failed") {
      // 终态活动刚把报告落库，补一次延迟刷新，避免最后一次轮询早于落库（ADR-008）。
      if (finalRefreshDone.current === workflow.id) return;
      finalRefreshDone.current = workflow.id;
      const timer = window.setTimeout(() => void refresh(), 1500);
      return () => window.clearTimeout(timer);
    }
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [workflow, refresh]);

  const completed = useMemo(() => new Set(workflow?.checkpoint?.completed_steps ?? []), [workflow]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!session || !content.trim()) return;
    setSending(true); setError("");
    try { const accepted = await api.sendMessage(session.id, content.trim()); setContent(""); setWorkflow(await api.getWorkflow(accepted.workflow_id)); setMessages(await api.getMessages(session.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "任务提交失败"); }
    finally { setSending(false); }
  };
  const toggleSession = async () => {
    if (!session) return;
    try { const result = session.status === "paused" ? await api.resumeSession(session.id) : await api.pauseSession(session.id); setSession(result.session); if (result.workflow) setWorkflow(result.workflow); setError(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "状态切换失败"); }
  };
  if (loading) return <div className="loading-screen">正在加载工作台</div>;

  return <div className="app-frame">
    <aside className="sidebar">
      <div className="product-mark"><span className="mark-square">M</span><div><strong>协作平台</strong><small>Multi-Agent</small></div></div>
      <nav className="main-nav" aria-label="主导航"><button className="nav-item active"><span className="nav-icon">□</span>工作台</button><button className="nav-item"><span className="nav-icon">≡</span>任务记录</button><button className="nav-item"><span className="nav-icon">◇</span>Agent 团队</button></nav>
      <div className="sidebar-bottom"><div className="runtime-label">运行环境</div><div className="runtime-row"><span className={`health-dot ${error ? "bad" : ""}`} />后端服务<span className="runtime-state">{error ? "异常" : "正常"}</span></div><div className="runtime-row"><span className="health-dot" />Dapr Workflow<span className="runtime-state">{workflow ? "已连接" : "待命"}</span></div></div>
    </aside>
    <div className="page-shell">
      <header className="page-header"><div><div className="breadcrumb">工作台 / 当前会话</div><h1>协作任务</h1></div><div className="header-actions"><span className={`service-state ${error ? "is-error" : ""}`}><i />{error ? "服务异常" : "服务正常"}</span><button className="secondary-button" onClick={() => void refresh()}>刷新</button><button className="user-button">D<span>演示用户</span></button></div></header>
      {error && <div className="error-banner"><strong>暂时无法连接服务</strong><span>{error}</span><button onClick={() => void refresh()}>重试</button></div>}
      <section className="summary-row"><div className="summary-item"><span>当前会话</span><strong>{session ? "已建立" : "未建立"}</strong><small>{session?.user_id ?? "demo-user"}</small></div><div className="summary-item"><span>任务状态</span><strong className={workflow?.status}>{workflow ? statusText[workflow.status] : "等待任务"}</strong><small>{workflow ? formatTime(workflow.updated_at) : "尚未提交"}</small></div><div className="summary-item"><span>执行进度</span><strong>{workflow?.checkpoint?.completed_steps?.length ?? 0}<em>/3</em></strong><small>固定流水线</small></div><div className="summary-item summary-action"><button className="secondary-button" onClick={() => void toggleSession()} disabled={!session}>{session?.status === "paused" ? "恢复会话" : "暂停会话"}</button></div></section>
      <main className="content-grid">
        <section className="workspace-column">
          <div className="section-heading"><div><span className="section-kicker">TASK EXECUTION</span><h2>执行中的任务</h2></div>{workflow && <span className={`task-status ${workflow.status}`}>{statusText[workflow.status]}</span>}</div>
          <div className="task-board"><div className="task-board-head"><span>{workflow ? `任务 ${workflow.id.slice(0, 8)}` : "还没有活动任务"}</span><span>{workflow ? `更新于 ${formatTime(workflow.updated_at)}` : "提交任务后将在这里显示执行进度"}</span></div><div className="pipeline-row">{stages.map((stage, index) => { const active = workflow?.current_step === stage; const done = completed.has(stage); return <div className={`pipeline-step ${done ? "done" : ""} ${active ? "active" : ""}`} key={stage}><div className="step-index">{done ? "✓" : index + 1}</div><div><strong>{labels[stage]}</strong><small>{done ? "已完成" : active ? "正在处理" : "等待开始"}</small></div>{index < stages.length - 1 && <span className="step-connector" />}</div>; })}</div><div className="task-details"><div><span>当前步骤</span><strong>{workflow?.current_step ? labels[workflow.current_step] ?? workflow.current_step : "—"}</strong></div><div><span>已完成步骤</span><strong>{workflow?.checkpoint?.completed_steps?.length ?? 0} / 3</strong></div><div><span>Workflow ID</span><strong className="mono">{workflow?.id ? `${workflow.id.slice(0, 8)}…` : "—"}</strong></div></div></div>
          <form className="composer" onSubmit={submit}><div className="composer-heading"><div><span className="section-kicker">NEW TASK</span><h2>提交协作任务</h2></div><span>自动按固定三步流水线执行</span></div><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="输入任务内容，例如：分析一篇技术文章并生成结构化报告" disabled={!session || session.status === "paused"} /><div className="composer-actions"><span>{session?.status === "paused" ? "会话已暂停，恢复后可提交" : "任务提交后可在上方查看实时进度"}</span><button className="primary-button" disabled={sending || !content.trim() || session?.status === "paused"}>{sending ? "提交中" : "提交任务"}<b>↗</b></button></div></form>
        </section>
        <aside className="right-column"><section className="side-section"><div className="section-heading compact"><div><span className="section-kicker">AGENT TEAM</span><h2>团队成员</h2></div><span className="count-label">{agents.length} 人</span></div><div className="agent-table">{agents.map((agent, index) => <div className="agent-row" key={agent.id}><span className={`agent-number n${index + 1}`}>{String(index + 1).padStart(2, "0")}</span><div className="agent-info"><strong>{agent.name}</strong><span>{agent.role} · {agent.model}</span></div><span className="idle-state"><i />{agent.status === "idle" ? "空闲" : agent.status}</span></div>)}</div></section><section className="side-section history-section"><div className="section-heading compact"><div><span className="section-kicker">MESSAGE LOG</span><h2>消息记录</h2></div><span className="count-label">{messages.length} 条</span></div><div className="message-log">{messages.length === 0 ? <p className="empty-state">提交任务后，消息会显示在这里。</p> : messages.map((message) => <div className="log-row" key={message.id}><span className={`log-marker ${message.role}`} /><div><strong>{message.role === "user" ? "你" : message.role === "assistant" ? "Agent" : message.role}</strong><p>{message.content}</p></div><time>{formatTime(message.created_at)}</time></div>)}</div></section></aside>
      </main>
      <footer className="page-footer"><span>Multi-Agent Collaboration Platform</span><span>API /api/v1 · 自动刷新 2 秒</span></footer>
    </div>
  </div>;
}

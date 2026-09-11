import type { Agent, DataPage, Message, MessageAccepted, Metric, Provider, Session, Tool, ToolCall, Workflow } from "../types/api";

const json = async <T>(input: RequestInfo, init?: RequestInit): Promise<T> => {
  const response = await fetch(input, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.message ?? `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
};

export const api = {
  createSession: (userId = "demo-user") =>
    json<Session>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }),
  getSession: (id: string) => json<Session>(`/api/v1/sessions/${id}`),
 getAgents: async () => (await json<{ items: Agent[] }>("/api/v1/agents")).items,
  getProviders: async () => (await json<{ items: Provider[] }>("/api/v1/providers")).items,
  getTools: (page = 1) => json<DataPage<Tool>>(`/api/v1/tools?page=${page}&page_size=20`),
  getMetrics: (page = 1, workflowId?: string) => json<DataPage<Metric>>(`/api/v1/metrics?page=${page}&page_size=20${workflowId ? '&workflow_id=' + encodeURIComponent(workflowId) : ''}`),
  getToolCalls: (id: string, page = 1) => json<DataPage<ToolCall>>(`/api/v1/workflows/${encodeURIComponent(id)}/tool-calls?page=${page}&page_size=20`),
  getAgent: (id: string) => json<Agent>(`/api/v1/agents/${id}`),
  getMessages: async (id: string) =>
    (await json<{ items: Message[] }>(`/api/v1/sessions/${id}/messages?page=1&page_size=100`)).items,
  sendMessage: (id: string, content: string) =>
    json<MessageAccepted>(`/api/v1/sessions/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  getWorkflow: (id: string) => json<Workflow>(`/api/v1/workflows/${id}`),
  pauseSession: (id: string) =>
    json<{ session: Session; workflow: Workflow | null }>(`/api/v1/sessions/${id}/pause`, { method: "POST" }),
  resumeSession: (id: string) =>
    json<{ session: Session; workflow: Workflow | null }>(`/api/v1/sessions/${id}/resume`, { method: "POST" }),
};

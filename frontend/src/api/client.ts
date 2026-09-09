import type { Agent, Message, MessageAccepted, Session, Workflow } from "../types/api";

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

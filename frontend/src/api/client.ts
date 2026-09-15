import type {
  Agent,
  AgentConfigList,
  AgentConfigUpdate,
  AgentRegistryCreate,
  DataPage,
  McpCompactToolList,
  McpDiscovery,
  McpServer,
  McpServerCreate,
  McpServerList,
  McpServerUpdate,
  Message,
  MessageAccepted,
  Metric,
  ModelBatchImportRequest,
  ModelBatchImportResult,
  ModelDiscovery,
  ModelRegistry,
  ModelRegistryCreate,
  ModelRegistryList,
  ModelRegistryUpdate,
  Provider,
  ProviderConfig,
  ProviderConfigUpdate,
  ProviderPresetCatalog,
  ProviderRegistry,
  ProviderRegistryCreate,
  ProviderRegistryDetail,
  ProviderRegistryList,
  ProviderRegistryUpdate,
  Session,
  SessionSummary,
  Tool,
  ToolCall,
  Workflow,
} from "../types/api";

/** 带 HTTP 状态与错误码的接口错误，便于区分 404（未登记）/ 409（重复）/ 422（取值）。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

type ErrorPayload = {
  code?: unknown;
  message?: unknown;
  detail?: unknown;
};

/**
 * 把错误响应体归一成一句可读文案。
 *
 * 业务异常是 `{code, message, request_id}`（doc/api.md §1）；框架参数校验是
 * `{detail: [...]}`。两者都要能读懂，因此按 `message` → `detail` 顺序取值，
 * 数组形式的 `detail` 取首条并拼上字段路径。
 */
const toApiError = (payload: ErrorPayload, status: number): ApiError => {
  let message = `请求失败（${status}）`;
  let code: string | null = typeof payload.code === "string" ? payload.code : null;

  if (typeof payload.message === "string" && payload.message) {
    message = payload.message;
  } else if (typeof payload.detail === "string" && payload.detail) {
    message = payload.detail;
  } else if (Array.isArray(payload.detail) && payload.detail.length) {
    const first = payload.detail[0] as { loc?: unknown[]; msg?: unknown } | undefined;
    const path = Array.isArray(first?.loc) ? first.loc.slice(1).join(".") : "";
    const reason = typeof first?.msg === "string" ? first.msg : "参数校验失败";
    message = path ? `${path}：${reason}` : reason;
    code = code ?? "VALIDATION_ERROR";
  }

  return new ApiError(message, status, code);
};

/**
 * 统一的请求出口：只负责发请求与把非 2xx 转成 `ApiError`。
 * 解析交给调用方——删除类接口返回 `204`，没有正文可读。
 */
const send = async (input: RequestInfo, init?: RequestInit): Promise<Response> => {
  const response = await fetch(input, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw toApiError(payload ?? {}, response.status);
  }
  return response;
};

const json = async <T>(input: RequestInfo, init?: RequestInit): Promise<T> =>
  (await send(input, init)).json() as Promise<T>;

/** `204 No Content`：调用方不关心正文，只关心「成功了」。 */
const noContent = async (input: RequestInfo, init?: RequestInit): Promise<void> => {
  await send(input, init);
};

const body = (value: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(value),
});

const query = (params: Record<string, string | number | boolean | undefined | null>): string => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
};

export const api = {
  createSession: (userId = "demo-user") =>
    json<Session>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }),
  getSession: (id: string) => json<Session>(`/api/v1/sessions/${id}`),
  /** 历史会话分页列表（`doc/api.md` §5.13），每项含摘要字段。 */
  listSessions: (page = 1, pageSize = 20) =>
    json<DataPage<SessionSummary>>(`/api/v1/sessions${query({ page, page_size: pageSize })}`),
  /** 删除会话及其关联数据（`doc/api.md` §5.14）。 */
  deleteSession: (id: string) =>
    noContent(`/api/v1/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getAgents: async () => (await json<{ items: Agent[] }>("/api/v1/agents")).items,
  getProviders: async () => (await json<{ items: Provider[] }>("/api/v1/providers")).items,
  getTools: (page = 1, pageSize = 20) =>
    json<DataPage<Tool>>(`/api/v1/tools${query({ page, page_size: pageSize })}`),
  getMetrics: (page = 1, workflowId?: string) =>
    json<DataPage<Metric>>(
      `/api/v1/metrics?page=${page}&page_size=20${workflowId ? "&workflow_id=" + encodeURIComponent(workflowId) : ""}`,
    ),
  getToolCalls: (id: string, page = 1) =>
    json<DataPage<ToolCall>>(`/api/v1/workflows/${encodeURIComponent(id)}/tool-calls?page=${page}&page_size=20`),
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

  /* ---------------------------------------------------------------------- */
  /* §5.8 默认路由（legacy 五列 + default_llm_model_id）                      */
  /* ---------------------------------------------------------------------- */

  getProviderConfig: () => json<ProviderConfig>("/api/v1/config/provider"),
  updateProviderConfig: (update: ProviderConfigUpdate) =>
    json<ProviderConfig>("/api/v1/config/provider", {
      method: "PUT",
      body: JSON.stringify(update),
    }),

  /* ---------------------------------------------------------------------- */
  /* §5.12 Provider 预设目录                                                 */
  /* ---------------------------------------------------------------------- */

  getProviderPresets: () => json<ProviderPresetCatalog>("/api/v1/config/provider-presets"),

  /* ---------------------------------------------------------------------- */
  /* §5.9 Provider 注册表                                                    */
  /* ---------------------------------------------------------------------- */

  listProviderRegistry: (enabled?: boolean) =>
    json<ProviderRegistryList>(`/api/v1/config/providers${query({ enabled })}`),
  createProviderRegistry: (payload: ProviderRegistryCreate) =>
    json<ProviderRegistry>("/api/v1/config/providers", body(payload)),
  getProviderRegistry: (id: string) =>
    json<ProviderRegistryDetail>(`/api/v1/config/providers/${encodeURIComponent(id)}`),
  patchProviderRegistry: (id: string, payload: ProviderRegistryUpdate) =>
    json<ProviderRegistry>(`/api/v1/config/providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  /** `force=true` 才级联删除；否则仍有启用模型时后端返回 `409 PROVIDER_IN_USE`。 */
  deleteProviderRegistry: (id: string, force = false) =>
    noContent(`/api/v1/config/providers/${encodeURIComponent(id)}${query({ force: force || undefined })}`, {
      method: "DELETE",
    }),

  /* ---------------------------------------------------------------------- */
  /* §5.10 模型注册表与批量引入                                               */
  /* ---------------------------------------------------------------------- */

  /** 服务端出站拉取远端模型清单；**不写库**，需由用户显式触发。 */
  discoverProviderModels: (providerId: string) =>
    json<ModelDiscovery>(`/api/v1/config/providers/${encodeURIComponent(providerId)}/models/discover`),
  listModelRegistry: (providerId?: string, enabled?: boolean) =>
    json<ModelRegistryList>(`/api/v1/config/models${query({ provider_id: providerId, enabled })}`),
  createModelRegistry: (payload: ModelRegistryCreate) =>
    json<ModelRegistry>("/api/v1/config/models", body(payload)),
  /** 已存在的 `(provider_id, model)` 计入 `skipped`，重复提交幂等。 */
  batchImportModels: (payload: ModelBatchImportRequest) =>
    json<ModelBatchImportResult>("/api/v1/config/models/batch", body(payload)),
  patchModelRegistry: (id: string, payload: ModelRegistryUpdate) =>
    json<ModelRegistry>(`/api/v1/config/models/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteModelRegistry: (id: string) =>
    noContent(`/api/v1/config/models/${encodeURIComponent(id)}`, { method: "DELETE" }),

  /* ---------------------------------------------------------------------- */
  /* §5.11 MCP Server 注册表与紧凑工具目录                                    */
  /* ---------------------------------------------------------------------- */

  listMcpServers: () => json<McpServerList>("/api/v1/config/mcp/servers"),
  createMcpServer: (payload: McpServerCreate) =>
    json<McpServer>("/api/v1/config/mcp/servers", body(payload)),
  getMcpServer: (id: string) => json<McpServer>(`/api/v1/config/mcp/servers/${encodeURIComponent(id)}`),
  patchMcpServer: (id: string, payload: McpServerUpdate) =>
    json<McpServer>(`/api/v1/config/mcp/servers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteMcpServer: (id: string) =>
    noContent(`/api/v1/config/mcp/servers/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** 建连、握手、缓存工具目录；只写 `discovered`，不改 `enabled`。 */
  discoverMcpServer: (id: string) =>
    json<McpDiscovery>(`/api/v1/config/mcp/servers/${encodeURIComponent(id)}/discover`, { method: "POST" }),
  /** 紧凑目录：不含 `input_schema`；完整 Schema 走 `getTools`。 */
  listMcpCompactTools: () => json<McpCompactToolList>("/api/v1/config/mcp/tools"),

  /* ---------------------------------------------------------------------- */
  /* §5.7 Agent 角色绑定与调参                                                */
  /* ---------------------------------------------------------------------- */

  listAgentConfigs: () => json<AgentConfigList>("/api/v1/config/agents"),
  patchAgentConfig: (id: string, payload: AgentConfigUpdate) =>
    json<Agent>(`/api/v1/config/agents/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  createAgentRegistry: (payload: AgentRegistryCreate) =>
    json<Agent>("/api/v1/config/agents", body(payload)),
  deleteAgentRegistry: (id: string) =>
    noContent(`/api/v1/config/agents/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};

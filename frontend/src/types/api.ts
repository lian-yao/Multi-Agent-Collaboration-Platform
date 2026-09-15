/**
 * 与后端 Pydantic / OpenAPI 对齐的前端类型（`doc/api.md` §4–§5）。
 *
 * 约定：
 * - 后端时间统一是 ISO 8601 字符串，前端不做 Date 转换后再存回。
 * - 「可清除」字段用 `T | null`：`PATCH` / `PUT` 省略 = 不改动，显式 `null` = 清除覆盖。
 * - `api_key` 只写不回读，所以响应里只有 `api_key_configured`。
 */

export interface Message {
  id: string;
  session_id: string;
  role: string;
  content: string;
  agent_run_id: string | null;
  status: string;
  created_at: string;
}

export interface Session {
  id: string;
  user_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

/** 历史会话列表项（`doc/api.md` §5.13）：会话元信息 + 首条消息摘要 + 最近执行终态。 */
export interface SessionSummary extends Session {
  title: string;
  latest_workflow_status: string | null;
  latest_workflow_id: string | null;
}

export type WorkflowStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface Workflow {
  id: string;
  session_id: string | null;
  agent_run_id: string | null;
  status: WorkflowStatus;
  current_step: string | null;
  checkpoint: {
    status?: string;
    current_step?: string | null;
    completed_steps?: string[];
    updated_at?: string;
  } | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

/* -------------------------------------------------------------------------- */
/* ADR-017 枚举：与 app/core/model_registry.py 的常量表逐字对应                 */
/* -------------------------------------------------------------------------- */

export const REASONING_TYPES = ["none", "openai", "gemini", "anthropic"] as const;
export type ReasoningType = (typeof REASONING_TYPES)[number];

export const CUSTOM_PARAMETER_TYPES = ["text", "number", "boolean", "json"] as const;
export type CustomParameterType = (typeof CUSTOM_PARAMETER_TYPES)[number];

export const MCP_TRANSPORTS = ["stdio", "http", "sse", "ws"] as const;
export type McpTransport = (typeof MCP_TRANSPORTS)[number];

export const API_TYPES = [
  "openai-compatible",
  "openai-responses",
  "anthropic",
  "gemini",
  "amazon-bedrock",
] as const;
export type ApiType = (typeof API_TYPES)[number];

export interface CustomParameter {
  key: string;
  value: string;
  type: CustomParameterType;
}

/* -------------------------------------------------------------------------- */
/* §5.1–§5.6 会话 / 工作流 / Agent / 工具 / 指标                                 */
/* -------------------------------------------------------------------------- */

export interface Agent {
  id: string;
  name: string;
  role: string;
  model: string;
  provider: string;
  /** 注册表 Provider 的显示名；未绑定或未登记时为 `null`。 */
  provider_name: string | null;
  /** 绑定的 `llm_models.id`；`null` = 未绑定，走默认路由 / 环境配置。 */
  llm_model_id: string | null;
  temperature: number;
  top_p: number | null;
  max_output_tokens: number | null;
  reasoning_type: string;
  status: string;
  /** 当前**显式覆盖**的字段名（未列出的字段来自环境配置或默认路由）。 */
  override_keys: string[];
  /** 内置流水线角色（不可删除）；`false` = 自定义角色。 */
  builtin: boolean;
  description: string | null;
  enabled: boolean;
}

export interface Provider {
  id: string;
  name: string;
  model: string;
  base_url: string | null;
  status: string;
  temperature: number;
}

export interface Tool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  status: string;
}

export interface Metric {
  id?: number;
  metric_name: string;
  value: number;
  labels: Record<string, unknown>;
  recorded_at: string;
}

export interface ToolCall {
  id: string;
  run_id: string;
  workflow_run_id: string | null;
  tool_name: string;
  input: Record<string, unknown>;
  output: unknown;
  status: "running" | "succeeded" | "failed";
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageAccepted {
  message_id: string;
  session_id: string;
  agent_run_id: string;
  workflow_id: string;
  status: string;
}

/** 三个只读列表接口（工具 / 指标 / 调用）共用的分页外壳。 */
export interface DataPage<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  availability: "available" | "not_integrated";
}

/* -------------------------------------------------------------------------- */
/* §5.7 Agent 角色绑定与调参                                                    */
/* -------------------------------------------------------------------------- */

/** `GET /api/v1/config/agents` 的可选模型条目（只含 `enabled=true`）。 */
export interface AvailableModel {
  id: string;
  provider_id: string;
  model: string;
  name: string | null;
  enabled: boolean;
}

export interface AgentConfigList {
  items: Agent[];
  available_models: AvailableModel[];
}

/** 省略的键不提交；显式 `null` = 清除该字段的覆盖。 */
export interface AgentConfigUpdate {
  llm_model_id?: string | null;
  model?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  max_output_tokens?: number | null;
  reasoning_type?: ReasoningType | null;
}

/** `POST /api/v1/config/agents` 的请求体：新建自定义角色。 */
export interface AgentRegistryCreate {
  id: string;
  name: string;
  role: string;
  description?: string | null;
  system_prompt?: string | null;
  enabled?: boolean;
}

/* -------------------------------------------------------------------------- */
/* §5.8 默认路由（legacy 五列 + default_llm_model_id）                          */
/* -------------------------------------------------------------------------- */

export interface ProviderConfig {
  provider: string;
  model: string;
  base_url: string | null;
  temperature: number;
  top_p: number | null;
  max_tokens: number | null;
  api_key_configured: boolean;
  default_llm_model_id: string | null;
  llm_model_id: string | null;
  provider_name: string | null;
  preset_type: string;
  api_type: string;
  reasoning_type: string;
  updated_by: string | null;
  updated_at: string | null;
}

export interface ProviderConfigUpdate {
  provider?: string;
  model?: string | null;
  base_url?: string | null;
  /** 省略 = 不修改；显式 `null` = 清除已存密钥、回退环境变量。 */
  api_key?: string | null;
  temperature?: number | null;
  default_llm_model_id?: string | null;
}

/* -------------------------------------------------------------------------- */
/* §5.9 Provider 注册表                                                        */
/* -------------------------------------------------------------------------- */

export interface ProviderRegistry {
  id: string;
  name: string;
  preset_type: string;
  api_type: string;
  base_url: string | null;
  api_key_configured: boolean;
  custom_headers: Record<string, string>;
  additional_settings: Record<string, unknown>;
  enabled: boolean;
  model_count: number;
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface ProviderRegistryDetail extends ProviderRegistry {
  models: ModelRegistry[];
}

export interface ProviderRegistryList {
  items: ProviderRegistry[];
  total: number;
}

export interface ProviderRegistryCreate {
  id: string;
  name: string;
  preset_type: string;
  api_type?: string;
  base_url?: string | null;
  api_key?: string | null;
  custom_headers?: Record<string, string>;
  additional_settings?: Record<string, unknown>;
  enabled?: boolean;
}

/** `id` 不可改；`api_key` 空串 = 不修改，显式 `null` = 清除。 */
export interface ProviderRegistryUpdate {
  name?: string;
  preset_type?: string;
  api_type?: string;
  base_url?: string | null;
  api_key?: string | null;
  custom_headers?: Record<string, string> | null;
  additional_settings?: Record<string, unknown> | null;
  enabled?: boolean;
}

/* -------------------------------------------------------------------------- */
/* §5.10 模型注册表与批量引入                                                   */
/* -------------------------------------------------------------------------- */

export interface ModelRegistry {
  id: string;
  provider_id: string;
  model: string;
  name: string | null;
  enabled: boolean;
  reasoning_type: string;
  temperature: number | null;
  top_p: number | null;
  max_context_tokens: number | null;
  max_output_tokens: number | null;
  custom_parameters: CustomParameter[];
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface ModelRegistryList {
  items: ModelRegistry[];
  total: number;
}

export interface ModelRegistryCreate {
  id?: string;
  provider_id: string;
  model: string;
  name?: string | null;
  enabled?: boolean;
  reasoning_type?: ReasoningType;
  temperature?: number | null;
  top_p?: number | null;
  max_context_tokens?: number | null;
  max_output_tokens?: number | null;
  custom_parameters?: CustomParameter[];
}

/** `provider_id` 不在此接口范围（换 Provider 等于换条目）。 */
export interface ModelRegistryUpdate {
  model?: string;
  name?: string | null;
  enabled?: boolean;
  reasoning_type?: ReasoningType;
  temperature?: number | null;
  top_p?: number | null;
  max_context_tokens?: number | null;
  max_output_tokens?: number | null;
  custom_parameters?: CustomParameter[];
}

export interface ModelBatchImportRequest {
  provider_id: string;
  models: string[];
  name_prefix?: string;
  enabled?: boolean;
  /** 只给这一批新条目设共同默认值；已存在条目只计入 `skipped`。 */
  defaults?: {
    temperature?: number | null;
    top_p?: number | null;
    max_context_tokens?: number | null;
    max_output_tokens?: number | null;
    reasoning_type?: ReasoningType;
  };
}

export interface ModelBatchImportSkipped {
  model: string;
  reason: string;
}

export interface ModelBatchImportResult {
  created: string[];
  skipped: ModelBatchImportSkipped[];
  total_requested: number;
  provider_id: string;
}

export interface ModelDiscoveryItem {
  id: string;
  name: string;
  owned_by: string | null;
}

export interface ModelDiscovery {
  provider_id: string;
  source: string;
  items: ModelDiscoveryItem[];
  /** 该 Provider 下**已经登记**的模型名，供导入前去重。 */
  existing: string[];
  total: number;
}

/* -------------------------------------------------------------------------- */
/* §5.11 MCP Server 注册表与紧凑工具目录                                        */
/* -------------------------------------------------------------------------- */

export interface McpToolOption {
  disabled?: boolean;
  allowAutoExecution?: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  transport: string;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  cwd: string | null;
  url: string | null;
  headers: Record<string, string>;
  enabled: boolean;
  tool_options: Record<string, McpToolOption>;
  tool_count: number;
  discovered_at: string | null;
  server_info: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface McpServerList {
  items: McpServer[];
  total: number;
}

export interface McpServerCreate {
  id: string;
  name: string;
  transport: McpTransport;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  url?: string | null;
  headers?: Record<string, string>;
  enabled?: boolean;
  tool_options?: Record<string, McpToolOption>;
}

export interface McpServerUpdate {
  name?: string;
  transport?: McpTransport;
  command?: string | null;
  args?: string[] | null;
  env?: Record<string, string> | null;
  cwd?: string | null;
  url?: string | null;
  headers?: Record<string, string> | null;
  enabled?: boolean;
  tool_options?: Record<string, McpToolOption> | null;
}

export interface McpDiscoveryTool {
  name: string;
  description: string;
}

export interface McpDiscovery {
  server_id: string;
  server_info: Record<string, unknown> | null;
  tools: McpDiscoveryTool[];
  total: number;
  discovered_at: string | null;
}

/** 紧凑工具条目：**不含** `input_schema`（体积大且不影响开关判断）。 */
export interface McpCompactTool {
  server_id: string;
  server_name: string;
  enabled: boolean;
  name: string;
  description: string;
  tool_enabled: boolean;
  /** 最近一次发现结果里是否存在该工具（配置的函数，不是实时连接状态）。 */
  available: boolean;
}

export interface McpCompactServer {
  id: string;
  name: string;
  transport: string;
  enabled: boolean;
  tool_count: number;
  discovered_at: string | null;
}

export interface McpCompactToolList {
  items: McpCompactTool[];
  total: number;
  servers: McpCompactServer[];
}

/* -------------------------------------------------------------------------- */
/* §5.12 Provider 预设目录                                                     */
/* -------------------------------------------------------------------------- */

export interface ProviderPreset {
  preset_type: string;
  label: string;
  /** 无 logo 时的文字标记。 */
  monogram: string | null;
  /** 前端配色 token 名（`blue` / `indigo` / `purple` / `rose` / `amber` / …）。 */
  tint: string | null;
  category: string;
  default_api_type: string;
  supported_api_types: string[];
  default_base_url: string;
  requires_api_key: boolean;
  api_key_url: string | null;
  /** `false` 的预设（如 `amazon-bedrock`）不支持 §5.10 的远端发现。 */
  supports_model_discovery: boolean;
}

export interface ProviderPresetCategory {
  id: string;
  label: string;
}

export interface ProviderPresetCatalog {
  items: ProviderPreset[];
  categories: ProviderPresetCategory[];
}

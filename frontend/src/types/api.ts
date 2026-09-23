/**
 * 与后端 Pydantic / OpenAPI 对齐的前端类型（`doc/api.md` §4–§5）。
 *
 * 约定：
 * - 后端时间统一是 ISO 8601 字符串，前端不做 Date 转换后再存回。
 * - 「可清除」字段用 `T | null`：`PATCH` / `PUT` 省略 = 不改动，显式 `null` = 清除覆盖。
 * - `api_key` 只写不回读，所以响应里只有 `api_key_configured`。
 */

export type AttachmentKind = "image" | "text" | "document" | "unsupported";

/**
 * 附件在「能不能被模型用上」这件事上的状态。
 *
 * `failed` 是**上传成功但正文没解析出来**（扫描版 PDF、CID 字体等）：
 * 附件仍然在，但内容取不出来，界面必须显式标注，否则用户会以为它被用上了。
 */
export type AttachmentStatus = "ready" | "failed" | "unsupported";

/** 已登记的附件（`doc/api.md` §5.16，ADR-021 / ADR-024）。 */
export interface Attachment {
  id: string;
  session_id: string | null;
  message_id: string | null;
  name: string;
  mime: string;
  size_bytes: number;
  kind: AttachmentKind;
  status: AttachmentStatus;
  error: string | null;
  /** 原件字节是否还在库里（ADR-024）。`false` 只出现在该策略之前落库的附件上，
   *  此时不给「打开原件」入口——界面上不该出现必然 404 的链接。 */
  has_original: boolean;
  created_at: string;
}

/** 编排模式（ADR-019）：`static` 固定三步链路，`dynamic` 由规划节点按任务分配角色。 */
export type OrchestrationMode = "static" | "dynamic";

export interface Message {
  id: string;
  session_id: string;
  role: string;
  content: string;
  agent_run_id: string | null;
  status: string;
  created_at: string;
  attachments: Attachment[];
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

/** 动态编排的计划步骤摘要（`app/orchestration/dynamic_graph.py::dynamic_checkpoint_summary`）。 */
export interface PlanStepSummary {
  id: string;
  /** 角色 id（collector / analyst / reporter），不是阶段名。 */
  role: string;
  depends_on: string[];
  status: "pending" | "completed" | "failed" | "skipped";
}

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
    /** 编排模式（ADR-019）：静态链路不写这个字段，动态链路为 `"dynamic"`。 */
    mode?: string;
    /** `llm` 表示规划节点真的产出了计划，`fallback` 表示降级到固定三步。 */
    plan_source?: string;
    /** 动态链路才有的协作计划；静态链路下为 undefined。 */
    plan?: PlanStepSummary[];
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
  attachments: Attachment[];
  /**
   * 请求里带了、但没能挂上这条消息的附件 id（不存在 / 已被别的消息挂走）。
   *
   * 单独一个字段而不是并进错误码：消息本身是发成功的，附件缺一个是**部分失败**，
   * 报成 4xx 会让前端把已经发出去的消息当成没发出去。
   */
  unattached_attachment_ids: string[];
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
/* §5.17 阶段执行轨迹（执行台卡片弹窗的数据源）                                   */
/* -------------------------------------------------------------------------- */

/**
 * 阶段轨迹里的一次工具调用。
 *
 * `input` / `output` 超长时服务端换成 `{truncated: true, bytes, preview}`——
 * 界面据此显示「已截断」，而不是把一段被剪掉的内容当成全部。
 */
export interface StageToolCall {
  call_id: string;
  tool_name: string;
  status: "running" | "succeeded" | "failed";
  input: unknown;
  output: unknown;
  error: string | null;
}

/** 单个 Agent 阶段的执行轨迹；`reason` 与「有轨迹」互斥。 */
export interface StageTraceItem {
  stage: string;
  role: string;
  /** 本阶段收到的上游正文；根阶段为 `null`（它收到的就是原始任务）。 */
  input: string | null;
  /** 上游阶段 id，用于说明「输入来自哪一步」。 */
  input_from: string | null;
  /** 本阶段产出正文。 */
  output: string | null;
  tool_calls: StageToolCall[];
  truncated: boolean;
  /**
   * 没有轨迹时的**具体**原因（还没轮到 / 正在跑 / 状态已被清理 / 载荷损坏）。
   * 四种原因指向四种不同的下一步动作，界面必须原样显示，不要换成一句「暂无数据」。
   */
  reason: string | null;
}

export interface WorkflowStageTrace {
  workflow_id: string;
  mode: "static" | "dynamic";
  task: string | null;
  availability: "available" | "not_integrated";
  /** 整条链路都没有轨迹时的原因（目前只有动态编排会走到这里）。 */
  reason: string | null;
  items: StageTraceItem[];
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

/* -------------------------------------------------------------------------- */
/* §5.15 执行边界（沙箱状态，只读）                                            */
/* -------------------------------------------------------------------------- */

export interface SandboxLimits {
  timeout_seconds: number;
  memory_limit: string;
  cpu_limit: number;
  pids_limit: number;
  network_enabled: boolean;
  output_limit_chars: number;
  max_code_chars: number;
}

/**
 * 敏感工具执行边界。**只读**：限额属部署期安全边界，后端没有写接口，
 * 前端也不提供编辑入口。
 */
export interface SandboxStatus {
  backend: string;
  image: string;
  available: boolean;
  /** 不可用时的可读原因；可用时为 `null`。 */
  reason: string | null;
  limits: SandboxLimits;
}

/* -------------------------------------------------------------------------- */
/* §5.19 工作区（work_dir）                                                    */
/* -------------------------------------------------------------------------- */

export type WorkspaceMode = "read_only" | "workspace_write";

export interface WorkspaceQuota {
  max_file_bytes: number;
  max_total_bytes: number;
  max_entries: number;
}

/**
 * 用量视图。工作区根被挪走或目录被删时 `available=false` 并给出 `reason`——
 * 界面要如实展示「用量读不到」，不要显示成 0。
 */
export interface WorkspaceUsage {
  available: boolean;
  total_bytes?: number;
  entries?: number;
  truncated?: boolean;
  scan_limit?: number | null;
  reason?: string | null;
}

export interface Workspace {
  id: string;
  session_id: string | null;
  /** **相对**工作区根的路径；空串表示根本身。界面上不要拼成宿主绝对路径。 */
  path: string;
  mode: WorkspaceMode;
  name: string | null;
  quota: WorkspaceQuota;
  usage: WorkspaceUsage | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
}

export interface WorkspaceList {
  items: Workspace[];
  total: number;
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  kind: "file" | "dir" | "symlink" | "other";
  /** 指向工作区之外的符号链接：要标出来，且不可展开（`doc/api.md` §7.1）。 */
  outside: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  children?: WorkspaceEntry[] | null;
}

export interface WorkspaceTree {
  workspace_id: string;
  path: string;
  depth: number;
  entries: WorkspaceEntry[];
  truncated: boolean;
  limit: number;
}

/**
 * 工作区**根**的目录树（§5.19）：与 `WorkspaceTree` 同形，但**没有** `workspace_id`
 * ——根不是一条登记。供「选择文件夹位置」在**登记之前**浏览根内的子目录。
 */
export interface WorkspaceRootTree {
  path: string;
  depth: number;
  entries: WorkspaceEntry[];
  truncated: boolean;
  limit: number;
}

export interface WorkspaceCreate {
  session_id?: string | null;
  path?: string | null;
  mode?: WorkspaceMode;
  name?: string | null;
}

export interface WorkspacePatch {
  mode?: WorkspaceMode;
  name?: string | null;
}

/**
 * 一个待导入文件：**相对路径 + base64 内容**。
 *
 * 浏览器不把宿主绝对路径交给后端（`<input type="file" webkitdirectory>` 只给相对路径与
 * 内容），所以「选择文件夹」这条路是**导入一份副本**，不是让 Agent 直接操作本机那个目录。
 */
export interface WorkspaceImportFile {
  path: string;
  content_base64: string;
}

export interface WorkspaceImportItem {
  path: string;
  status: "imported" | "skipped" | "failed";
  reason?: string | null;
  size_bytes?: number | null;
  created_dirs?: string[] | null;
}

export interface WorkspaceImportResult {
  workspace_id: string;
  imported: number;
  skipped: number;
  failed: number;
  imported_bytes: number;
  items: WorkspaceImportItem[];
  usage?: WorkspaceUsage | null;
}

/* -------------------------------------------------------------------------- */
/* §5.20 工作区审批（覆盖 / 删除）                                             */
/* -------------------------------------------------------------------------- */

export type ApprovalKind = "overwrite" | "delete";
export type ApprovalStatus = "pending" | "approved" | "denied" | "expired" | "consumed";

export interface Approval {
  id: string;
  workspace_id: string | null;
  session_id: string | null;
  run_id: string | null;
  kind: ApprovalKind;
  /** 工作区相对路径。 */
  target: string;
  /** 平台生成的说明——不照抄模型输出（提示注入会经由审批卡片影响人）。 */
  reason: string | null;
  status: ApprovalStatus;
  payload: Record<string, unknown>;
  decided_by: string | null;
  requested_at: string | null;
  decided_at: string | null;
}

export interface ApprovalList {
  items: Approval[];
  total: number;
  /** 待决策条数，供角标使用。 */
  pending: number;
}

export type ApprovalDecision = "approved" | "denied";

/* -------------------------------------------------------------------------- */
/* §5.21 出网策略（只读）                                                      */
/* -------------------------------------------------------------------------- */

/**
 * 出网策略的只读投影。`blocked` 是**进程内**计数：工具调用发生在 worker 进程，
 * API 进程里通常是空的——界面不能把 0 说成「没有被拦过」（`doc/api.md` §5.21）。
 */
export interface EgressStatus {
  mode: "public_only" | "allowlist";
  allow_hosts: string[];
  deny_hosts: string[];
  internal_hosts: string[];
  allowed_ports: number[];
  model_exempt: boolean;
  max_redirects: number;
  blocked: Record<string, number>;
}

# REST API 契约（开发基线）

> 本文档是当前后端 REST 接口的事实契约。已实现接口以 `app/api/main.py` 的 FastAPI 路由为准；规划接口单独标注“未实现”，在代码落地前不得由前端假定可用。

## 1. 通用约定

- 基础路径：`/api/v1`；健康检查为 `/health`；Prometheus 文本指标端点为 `/metrics`（见 §5.6）。
- 请求与响应使用 `application/json`。
- 时间字段为 ISO 8601 UTC（带 `Z` 或明确时区偏移）。
- ID 当前以 UUID 字符串返回；Agent 角色 ID 为稳定字符串。
- 分页参数：`page` 从 1 开始，默认 `1`；`page_size` 默认 `20`，范围 `1–100`。
- 客户端通过 `GET /api/v1/workflows/{workflow_id}` 轮询异步任务，当前未提供 SSE/WebSocket。

### 错误响应

业务异常统一返回：

```json
{
  "code": "SESSION_NOT_FOUND",
  "message": "会话不存在",
  "request_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

请求头 `X-Request-ID` 存在时原样返回，否则服务端生成 UUID。FastAPI/Pydantic 参数校验错误使用框架默认 `422` 响应，后续如需统一为 `VALIDATION_ERROR`，必须同步修改实现与本文件。

| HTTP | code | 当前状态 | 含义 |
| --- | --- | --- | --- |
| 404 | `SESSION_NOT_FOUND` | 已实现 | 会话不存在 |
| 404 | `WORKFLOW_NOT_FOUND` | 已实现 | Workflow 不存在 |
| 409 | `SESSION_PAUSED` | 已实现 | 会话已暂停，不能接收新消息 |
| 409 | `WORKFLOW_NOT_PAUSABLE` | 已实现 | Workflow 当前状态不允许暂停或恢复 |
| 500 | `INTERNAL_ERROR` | 已实现 | Workflow 调度失败等内部错误 |
| 404 | `AGENT_NOT_FOUND` | 已实现 | 查询的 Agent 角色不存在 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 已实现 | 审计或指标数据源读取失败 |
| 404 | `PROVIDER_NOT_FOUND` | 已实现 | Provider 条目不存在（§5.9） |
| 409 | `PROVIDER_IN_USE` | 已实现 | Provider 仍被启用中的模型引用，不能删除（§5.9） |
| 404 | `MODEL_NOT_FOUND` | 已实现 | 模型条目不存在（§5.10） |
| 404 | `MCP_SERVER_NOT_FOUND` | 已实现 | MCP Server 条目不存在（§5.11） |
| 502 | `PROVIDER_DISCOVERY_FAILED` | 已实现 | 远端模型发现失败（网络/凭据/响应格式，§5.10） |
| 502 | `MCP_DISCOVERY_FAILED` | 已实现 | MCP Server 发现失败（连接/握手/列工具，§5.11） |
| 404 | `TOOL_NOT_FOUND` | 规划 | 工具不存在；当前没有工具路由 |

## 2. 核心对象与状态

### Session

```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "user_id": "demo-user",
  "status": "active",
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:00:00Z"
}
```

`status`：`active` ⇄ `paused`。暂停会话不能提交新消息；没有运行中 Workflow 时也可以暂停会话。

### Message

```json
{
  "id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "role": "user",
  "content": "帮我分析这篇技术文章",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "status": "queued",
  "created_at": "2026-09-07T08:00:00Z"
}
```

`role`：`user` / `assistant` / `system` / `tool`。`status`：`queued` → `running` → `completed`，或转为 `failed`。

### AgentRun

一次团队任务执行记录。当前消息入口创建 AgentRun 时 `agent_id` 为空，由编排层自动分工；数据库模型预留 `agent_id` 关联具体 Agent。

状态：`queued` → `running` ⇄ `paused` → `completed` / `failed`。

### WorkflowRun

```json
{
  "id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "status": "running",
  "current_step": "collect",
  "checkpoint": {
    "completed_steps": [],
    "current_step": "collect"
  },
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:01:00Z",
  "completed_at": null
}
```

状态：`pending` → `running` ⇄ `paused` → `completed` / `failed` / `cancelled`。`checkpoint` 和 `current_step` 用于前端展示与恢复进度。

### Agent

当前 `/api/v1/agents` 返回固定的三角色演示团队，角色 ID 与 Workflow 阶段对应：

| id | name | role | 阶段 |
| --- | --- | --- | --- |
| `collector` | 信息收集 Agent | `collector` | `collect` |
| `analyst` | 数据分析 Agent | `analyst` | `analyze` |
| `reporter` | 报告生成 Agent | `reporter` | `report` |

列表与详情返回**生效配置**：先取数据库 `agent_configs` 里该角色的覆盖值，未覆盖的字段回退 API 进程的 `AGENT_*` 环境配置（见 §5.7）。`status=idle` 为静态角色状态，不代表模型服务健康；运行状态由 Workflow 展示。`agent_configs` 读取失败时回退环境配置并记日志，不影响列表可用性。

## 3. 执行语义

团队任务唯一入口是发送会话消息：

1. 服务端校验 Session 状态。
2. 持久化 Message、AgentRun、WorkflowRun。
3. 按编排模式调度 Dapr Workflow，返回 `202 Accepted`。
4. 客户端轮询 Workflow，并重新读取消息列表获取最终回复。

### 3.1 编排模式

请求体字段：`content`（必填）与 `orchestration_mode`（可选）：

```json
{ "content": "分析一篇技术文章的核心要点", "orchestration_mode": "dynamic" }
```

| 模式 | 工作流 | 流程 |
| --- | --- | --- |
| `static`（默认） | `agent_pipeline` | 固定三步 `collect → analyze → report` |
| `dynamic` | `agent_dynamic` | 规划节点按任务产出计划，再按依赖就绪度逐步执行 |

`orchestration_mode` 只覆盖**这一次**执行；省略时用服务端 `AGENT_ORCHESTRATION_MODE`。
细节见 `doc/orchestration.md`，决策见 ADR-019。

当前仍不接受 `decision_agent_id`、Agent 列表、工具列表等字段。前端**不再提供**主决策 Agent
选择器：它只改前端 state、后端不接受该字段，属于「选了也不生效」的假选择，已于 ADR-018 移除。
参与哪些 Agent 由编排层决定，不由用户指定——`orchestration_mode` 选的是**编排方式**，
不是「参与哪些 Agent」。

## 4. 已实现接口

### 4.1 健康检查

`GET /health`

响应 `200`：

```json
{ "status": "ok" }
```

### 4.2 创建会话

`POST /api/v1/sessions`

请求：`{ "user_id": "demo-user" }`，`user_id` 可为空。响应 `201`，返回 Session。

**调用时机（会话生命周期）**——本接口是「把一条对话落到库里」的显式动作，前端**只在提交
首条消息时**调用它（配合 §4.4，见 §7）。在此之前工作台处于**草稿态**：`session = null`，
界面就是「新对话」模板。下面这些动作**都不得**调用本接口：

- 应用初始化 / 刷新页面；
- 点侧栏「新建任务」；
- 删除当前会话后的回退。

理由：会话在没有消息与 Workflow 时对用户零价值，而每次刷新都建一个会让历史列表被
`（暂无消息）` 的空数据淹没（实测一次开发期积累 108 条空会话 vs 19 条真实会话）。
草稿态下首条消息提交失败时，前端会尽力 `DELETE` 掉刚建的会话，不让它退化成空数据。

### 4.3 查询会话

`GET /api/v1/sessions/{session_id}`

响应 `200` 返回 Session；不存在返回 `404 SESSION_NOT_FOUND`。

### 4.4 发送消息并调度 Workflow

`POST /api/v1/sessions/{session_id}/messages`

请求体：

```json
{
  "content": "对比两种方案的实测数据并生成报告",
  "orchestration_mode": "dynamic",
  "attachment_ids": ["b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e"]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `content` | 否 | 用户任务。**允许为空**，但此时必须有 `attachment_ids`（见下） |
| `attachment_ids` | 否 | 先经 `POST /api/v1/attachments` 登记拿到的附件 id，最多 4 个（§5.16） |
| `orchestration_mode` | 否 | 单次执行的编排模式覆盖，`static` / `dynamic`；省略时用服务端 `AGENT_ORCHESTRATION_MODE`（默认 `static`）。见 §3.1 与 `doc/orchestration.md` |

`content` 与 `attachment_ids` **不能同时为空**（由模型校验器拦成 `422`）。放开 `content` 的
`min_length=1` 是为了支持「只发一张截图、不打字」这种最常见的多模态用法（ADR-021）。

非法 `orchestration_mode`（如 `"autonomous"`）由 `Literal` 校验拦成 `422`，**不静默退回 `static`**——
「选了动态却悄悄变成固定流程」比直接报错更难排查。

响应 `202`：

```json
{
  "message_id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "workflow_id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "status": "pending",
  "attachments": [
    {
      "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
      "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "message_id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
      "name": "架构草图.png",
      "mime": "image/png",
      "size_bytes": 184320,
      "kind": "image",
      "status": "ready",
      "error": null,
      "created_at": "2026-09-17T10:00:00Z"
    }
  ],
  "unattached_attachment_ids": []
}
```

接口内部在调度前会将持久化 Workflow 和 AgentRun 更新为 `running`；返回体保留 `pending` 作为接收状态。会话暂停时返回 `409 SESSION_PAUSED`。

`unattached_attachment_ids` 是**部分失败**的专用出口：请求里带了、但没能挂上这条消息的附件 id
（不存在 / 已被别的消息挂走）。**不并进错误码**——消息本身是发成功的，把它报成 `4xx` 会让前端把
已经发出去的消息当成没发出去。

调度到哪个工作流由编排模式决定（ADR-019）：`static` → `agent_pipeline`，
`dynamic` → `agent_dynamic`；非法值一律退回 `static`。响应体不返回本次使用的模式，
前端要展示请从 `GET /workflows/{id}` 的 `checkpoint` 读（动态模式带 `"mode": "dynamic"`）。

### 4.5 查询会话消息

完成任务的报告由终态活动追加为 assistant 消息（ADR-008）；失败不追加报告。生产存储按最新消息分页，再在页内按时间升序返回。前端读取第一页最近 100 条，并在终态延迟补刷一次；完整历史加载尚未实现。

每条消息带 `attachments`（该消息的附件列表，同 §4.4 的 `attachments` 结构；无附件时为空数组）。
它由 `list_attachments_for_messages` 一次性成组取回，**不是逐条消息一次查询**——否则一页 100 条
就是 100 次往返。

`GET /api/v1/sessions/{session_id}/messages?page=1&page_size=20`

响应 `200`：

```json
{ "items": [], "page": 1, "page_size": 20, "total": 0 }
```

`content` 为空 + `attachments` 非空是合法的（「只发一张截图」），前端在 `content` 为空时
不渲染空的正文段落。

### 4.6 暂停会话

`POST /api/v1/sessions/{session_id}/pause`

- 会话已暂停：幂等返回当前 Session。
- 有运行中 Workflow：请求 Dapr 暂停，成功后更新 Workflow 与 AgentRun 为 `paused`。
- 无运行中 Workflow：仅更新 Session。

响应 `200`：`{ "session": Session, "workflow": Workflow | null }`。

### 4.7 恢复会话

`POST /api/v1/sessions/{session_id}/resume`

恢复暂停的 Workflow（如有）与 Session，响应结构同暂停接口。

### 4.8 查询 Workflow

`GET /api/v1/workflows/{workflow_id}`

响应 `200` 返回 WorkflowRun；不存在返回 `404 WORKFLOW_NOT_FOUND`。

### 4.9 查询 Agent 团队

`GET /api/v1/agents`

响应 `200`：

```json
{
  "items": [
    {
      "id": "collector",
      "name": "信息收集 Agent",
      "role": "collector",
      "model": "qwen2.5-coder:7b",
      "status": "idle"
    }
  ]
}
```

当前固定返回三个角色，不支持通过 API 新增、删除或修改团队成员。

## 5. D7–D8 接口

以下只读接口为 Web 控制台提供 Provider、Agent、工具和指标展示。Tools、Tool Calls、Metrics 的分页响应统一为 `{items, page, page_size, total, availability}`。`availability` 为 `available`（数据源可读取，包括零条记录）或 `not_integrated`（注册表/表未接入）；后者返回空数组，不能理解为成功执行了零次调用。数据库连接或查询失败返回 `503 DATA_SOURCE_UNAVAILABLE`，不吞掉错误伪装为空数据。分页约束同 §1。

### 5.1 查询 Provider

GET /api/v1/providers

响应为 `{items: Provider[]}`，当前只返回选中的 Provider。字段：id、name、model、base_url（可空）、status、temperature。数据来自运行期 Provider 配置（§5.8）与环境配置 `AGENT_*` 的合并结果；status 为 configured 或 missing_model，不代表模型服务可达（凭据是否配置也不反映在该字段上）。base_url 去除用户信息、query、fragment；不返回密钥，不主动探测模型端点。

### 5.2 查询 Agent 详情

GET /api/v1/agents/{agent_id}

响应字段为 id、name、role、model、provider、temperature、status。未知角色返回 404 AGENT_NOT_FOUND。字段含义同 §4.9：model 与 temperature 为「数据库覆盖 + 环境配置回退」后的生效值，provider 始终来自 API 进程环境配置（不通过 API 修改）。

### 5.3 查询工具目录

GET /api/v1/tools?page=1&page_size=20

item 字段为 name、description、input_schema（JSON 对象）、status。数据来自 C 的注册表目录（`app/mcp/registry.py::tool_catalog()`，见 ADR-012），返回 calculator / web_search / code_execution / sql_query 四项，status 为 available；不会把规划中的工具当成已注册工具。

API 进程默认按 `MCP_TRANSPORT` 注入该目录（`InspectionStore(tool_catalog=tool_catalog)`）。注册表构建或读取失败返回 `503 DATA_SOURCE_UNAVAILABLE`，不吞掉错误伪装成空目录；只有显式构造为「未注入目录」的读取器才返回 availability=not_integrated。该接口只列目录，不探测每个工具的运行期可用性（例如沙箱后端是否可连）。

### 5.4 查询 Workflow 工具调用

GET /api/v1/workflows/{workflow_id}/tool-calls?page=1&page_size=20

item 字段为 id、run_id、workflow_run_id（可空）、tool_name、input（JSON 对象）、output（JSON 或 null）、status（running/succeeded/failed）、error（可空）、created_at、updated_at。读取 doc/data-model.md 的 tool_calls 表，按 created_at、id 升序分页，仅返回 workflow_run_id 匹配记录，或 workflow_run_id 为空且 run_id 匹配该 Workflow 的记录。Workflow 不存在返回 404 WORKFLOW_NOT_FOUND；表不存在返回 not_integrated。API 不创建表、不写审计记录。

### 5.5 查询执行指标

GET /api/v1/metrics?page=1&page_size=20

item 字段为 metric_name、value（有限数值）、labels（JSON 对象）、recorded_at。读取 doc/data-model.md 的 metrics 表，按 recorded_at、id 降序分页。可选 workflow_id 查询参数按 labels.workflow_id 严格过滤，未知 Workflow 返回 404；无参数时展示全局采样记录。表不存在返回 not_integrated。

`metrics` 表由 `app/core/checkpoint.py::MetricRecord` 定义，随 `init_checkpoint_schema()` 在 backend 进程启动时创建（见 `doc/data-model.md` §3）；从未启动过后端的空库会返回 `not_integrated`，这表示没建表，不等于“没有采样”。

指标展示保留原始 metric_name、labels、采样时间，不累加分页中可能重复的采样值。Token 名称交接约定为 input_tokens/output_tokens/total_tokens，数值 0 显示为 0，缺少采样显示“暂无采样”；比率和耗时由采集方定义后写入，前端不估算。C 负责采集、去重和 labels.workflow_id（可选 agent_id/model）关联，B 负责建表与审计写入，D 只负责读取和呈现；此约定需 A/B/C 联调验收。

前端入口：工作台「任务记录」页（`frontend/src/Inspection.tsx::RuntimeSampling`）。它是**观测**而不是配置，因此不放在「工具与配置」页。有 Workflow 时按 `workflow_id` 过滤并在未到终态时轮询，终态停止；没有 Workflow 时退回全局采样。`labels` 以一排 `键 = 值` 小标签渲染，不展开成 JSON 块。

### 5.6 Prometheus 文本指标

`GET /metrics`

`200`，`Content-Type` 为 prometheus_client 0.26.0 的 `CONTENT_TYPE_LATEST`，当前取值 `text/plain; version=1.0.0; charset=utf-8`（跟随依赖版本，前端与抓取配置不应硬编码版本号），响应体为进程内 Prometheus 注册表的文本格式，供 Prometheus 按实例抓取。该端点不在 `/api/v1` 下，与 `/health` 同级，不属于 JSON 契约，错误响应也不使用 §1 的统一错误体。

- 指标名与 §5.5 的 `metrics` 表采样同名同标签（`macp_tool_calls_total`、`macp_tool_duration_seconds`、`macp_stage_duration_seconds`、`macp_llm_tokens_total`、`macp_workflow_runs_total`、`macp_metrics_buffer_samples`），便于与表内采样交叉核对。
- 只读进程内注册表：不连接数据库、不写审计、不因 `metrics` 表缺失而失败。API 进程（`uvicorn` 托管 `app.api.main:app`）与 Workflow Worker 是同一进程时指标合并在一处；多副本部署按实例分别抓取。
- 该端点只反映本进程观测到的调用；无任何调用时返回空的指标族（仅 HELP/TYPE 行）。

### 5.7 Agent 角色目录与配置（热更新）

`GET /api/v1/config/agents`、`POST /api/v1/config/agents`、
`PATCH /api/v1/config/agents/{agent_id}`、`DELETE /api/v1/config/agents/{agent_id}`

修改某个角色的模型绑定与调参覆盖值，**无需重启进程**：下一次阶段执行即按新配置建模
（见 `doc/decisions/013-agent-config-hot-update.md`、`doc/decisions/017-multi-provider-model-registry.md`）。

角色目录来自 `agent_registry` 表（ADR-017 的 `chatModels` 同构扩展）：三个内置流水线角色
（collector / analyst / reporter）作为 `builtin=true` 的种子数据，**不可删除**；自定义角色
（`builtin=false`）可自由增删启停。自定义角色暂不接入固定三步流水线，仅作为可绑定模型的
配置单元存在（意图路由属后续架构演进）。

`GET /api/v1/config/agents` 返回全部角色的生效配置与可选模型清单，供「Agent 团队」页
（`frontend/src/config/AgentPanel.tsx`）一次加载；`PATCH` 只提交被改动的字段：

```json
{
  "items": [
    {
      "id": "collector",
      "name": "信息收集 Agent",
      "role": "collector",
      "model": "qwen2.5-coder:7b",
      "provider": "ollama",
      "provider_name": "Ollama（本地）",
      "llm_model_id": null,
      "temperature": 0.3,
      "top_p": null,
      "max_output_tokens": null,
      "reasoning_type": "none",
      "status": "idle",
      "override_keys": ["temperature"],
      "builtin": true,
      "description": "收集、检索并整理任务主题相关的事实与要点。",
      "enabled": true
    }
  ],
  "available_models": [
    { "id": "m-1", "provider_id": "gateway-main", "model": "gpt-4o-mini", "name": "GPT-4o mini", "enabled": true }
  ]
}
```

`override_keys` 列出该角色**当前被覆盖**的字段名（未列出的字段来自环境配置或默认路由），
供前端区分「显式覆盖」与「回退值」。`available_models` 只含 `enabled=true` 的模型条目，
按 `provider_id`、`model` 升序。`builtin` / `description` / `enabled` 来自角色目录。

`POST /api/v1/config/agents` 登记自定义角色（请求体）：

```json
{
  "id": "summarizer",
  "name": "摘要 Agent",
  "role": "summarizer",
  "description": "收集、归纳并输出摘要。",
  "enabled": true
}
```

- `id`：1–50 字符，`^[A-Za-z0-9._-]+$`；不可与内置角色 id 冲突，重复登记返回 `409`。
- `name`：1–100 字符；`role`：1–50 字符；`description` 可选（≤ 1000 字符）。
- `system_prompt` 可选（≤ 8000 字符），本期前端暂不暴露编辑入口。

`DELETE /api/v1/config/agents/{agent_id}` 删除自定义角色（连同其 `agent_configs` 覆盖行）：
内置角色返回 `409 AGENT_BUILTIN`，不存在的角色返回 `404 AGENT_NOT_FOUND`。

`PATCH` 请求体（所有字段都可选，但至少要给一个）：

```json
{
  "llm_model_id": "m-1",
  "model": "qwen2.5-coder:7b",
  "temperature": 0.3,
  "top_p": 0.9,
  "max_output_tokens": 2048,
  "reasoning_type": "openai"
}
```

- 字段省略 = 不改动该字段；显式传 `null` = 清除该字段的覆盖，回退下一层配置。
- `llm_model_id`：1–80 字符，必须指向存在的 `llm_models.id`，否则 `422`。
- `model`：1–200 字符，去除首尾空白后不能为空。
- `temperature`：`0.0`–`2.0`（闭区间）。
- `top_p`：`0.0`–`1.0`（闭区间）。
- `max_output_tokens`：整数且 ≥ 1。
- `reasoning_type`：`none` / `openai` / `gemini` / `anthropic`。
- `provider` 不在本接口范围：它涉及 base_url 与凭据，由 §5.8 / §5.9 管理；
  `llm_model_id` 已经间接决定 Provider。

权限边界：**本接口不鉴权**（ADR-015，2026-09-15 起）：任何能访问该 API 的调用方都可以写入。部署时必须把 API 限制在本机或可信内网，不要直接暴露到公网（见 `doc/deployment.md`）。

响应 `200` 返回与 §5.2 同构的 Agent 对象（生效配置，额外含 `provider_name` / `llm_model_id` /
`top_p` / `max_output_tokens` / `reasoning_type` / `override_keys`）。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `AGENT_NOT_FOUND` | 角色不存在 |
| 409 | `VALIDATION_ERROR` | `POST` 时 id 与内置角色冲突或已存在 |
| 409 | `AGENT_BUILTIN` | `DELETE` 目标是内置流水线角色 |
| 422 | 框架默认或 `VALIDATION_ERROR` | Pydantic 校验失败（空 body、非法 temperature/top_p、超长 model、空白 model、未知 llm_model_id、未知 reasoning_type、非法 id 格式） |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 覆盖值写入失败（写操作必须显式失败，不回退、不静默成功） |

持久化：覆盖值写入 `agent_configs` 表、角色目录写入 `agent_registry` 表
（`doc/data-model.md` §3），覆盖表只存被覆盖的字段。
审计：每次成功写入产生结构化日志 `event=config.agent.updated`（含 `agent_id`、`actor`、
`before`/`after`、`request_id`），表内同时记录 `updated_by` / `updated_at`；
`updated_by` 取请求头 `X-Request-ID`，缺省为空。

并发与顺序：接口是「最后写入者生效」，不提供乐观锁或版本号；覆盖值按角色粒度，互不影响。

解析优先级（低 → 高，逐字段回退，ADR-017 §2）：
`AGENT_*` 环境配置 → `provider_configs.default_llm_model_id` 指向的模型条目 →
`provider_configs` 的 legacy 五列 → 本表的角色覆盖。`llm_model_id` 悬空（指向已删除的条目）
时按未绑定处理，不报错。

### 5.8 读取与修改模型 Provider 配置

`GET /api/v1/config/provider`、`PUT /api/v1/config/provider`

读取生效的模型 Provider 配置，或写入运行期覆盖值（**无需重启进程**，与 §5.7 同构，见 ADR-014）。

`GET` 响应（`api_key` 永不回传）：

```json
{
  "provider": "openai",
  "model": "gpt-4o-mini",
  "base_url": "https://api.example.com/v1",
  "temperature": 0.2,
  "default_llm_model_id": "gateway-main:gpt-4o-mini",
  "api_key_configured": true,
  "updated_by": "req-7f3",
  "updated_at": "2026-09-15T08:00:00Z"
}
```

- 字段是「存储配置 → 环境配置」合并后的生效值；`base_url` 为空表示使用提供方官方端点。
- `default_llm_model_id` 为默认模型路由（ADR-017）：非空时该 id 指向的 `llm_models` 条目及其
  Provider 决定实际端点、凭据与特化参数，优先级高于本表 `provider` / `model` / `base_url` /
  `api_key` / `temperature` 五个 legacy 列。指向的条目已被删除时按未设置处理并记一次警告。
- `api_key_configured` 只表示是否已有可用凭据（存储值或环境变量），**不代表凭据有效**。
- `updated_*` 反映最近一次通过本接口写入的时间与来源；从未写入过时为 `null`。
- `base_url` 去掉用户信息、query、fragment 后再返回，不返回密钥。

`PUT` 请求体（六个字段都可选，但至少要给一个）：

```json
{
  "provider": "openai",
  "model": "gpt-4o-mini",
  "base_url": "https://api.example.com/v1",
  "api_key": "sk-...",
  "temperature": 0.2,
  "default_llm_model_id": "gateway-main:gpt-4o-mini"
}
```

- 字段省略 = 不改动该字段；显式传 `null` = 清除该字段的覆盖，回退环境配置。
- `provider`：`openai` 或 `ollama`。
- `model`：1–200 字符，去除首尾空白后不能为空。
- `base_url`：不超过 500 字符；必须是 `http`/`https` URL；显式空串按 `null`（清除覆盖）处理。
- `api_key`：1–500 字符；**只写入、不回读**，响应与日志都不含原值。
- `temperature`：`0.0`–`2.0`（闭区间）。
- `default_llm_model_id`：1–80 字符，必须指向存在的 `llm_models.id`，否则 `422`。

权限边界：**本接口不鉴权**（ADR-015，规则与 §5.7 一致）：`GET` 与 `PUT` 都可匿名调用，部署边界要求见 §5.7。

响应：两者都返回上面的 `GET` 结构（`PUT` 返回写入后的生效值，同样不含密钥）。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 422 | 框架默认或 `VALIDATION_ERROR` | Pydantic 校验失败或取值非法（空 body、非法 provider、超长字段、非 http(s) base_url、temperature 越界） |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 覆盖值写入失败（写操作必须显式失败，不回退、不静默成功） |

持久化与一致性：覆盖值写入 `provider_configs`（单行，`doc/data-model.md` §3）；**PostgreSQL 为事实源**，写入成功后把同一份配置镜像到 Redis `provider:config`（`doc/data-model.md` §4）。Redis 写失败只记警告、不影响响应，读取时未命中会回源 PostgreSQL 并回填；Redis 与 PostgreSQL 都不可用时回退环境配置并记警告，读接口不因此失败。加密不在本期范围，`api_key` 以明文存储在事实源与镜像中，访问边界由数据库权限与部署网络保证（ADR-014、ADR-015）。

审计：每次成功写入产生结构化日志 `event=config.provider.updated`（含 `actor`、变更字段名、`before`/`after` 的**脱敏**快照——`api_key` 只记 `set`/`unset`）。

模型构造语义：合并后的配置在阶段活动执行时解析，因此 `PUT` 后的下一次任务即生效；`provider=openai` 而缺少 `model` 或凭据时，模型构造抛出明确错误并让任务失败，**不静默回退到其他提供方**。

前端入口：工作台「工具与配置」页的「默认路由」分区（`frontend/src/config/DefaultRoutePanel.tsx`）读写本接口。页面读取生效值并显示凭据是否配置；提交时只发送被改动的字段，清空某项并按保存 = 清除该覆盖（回退环境配置），凭据输入框留空表示不修改；「清除覆盖并回退环境配置」一次性清除 `default_llm_model_id`/`model`/`base_url`/`api_key`/`temperature`。响应不含密钥，因此页面无法回显密钥原值。

### 5.9 模型 Provider 注册表

`GET /api/v1/config/providers`、`POST /api/v1/config/providers`、
`GET|PATCH|DELETE /api/v1/config/providers/{provider_id}`

多 Provider 注册表（ADR-017）：一次登记多个端点与凭据，供 §5.10 的模型条目引用。
`api_key` 只写不回读。

`GET /api/v1/config/providers` 响应：

```json
{
  "items": [
    {
      "id": "gateway-main",
      "name": "自建网关",
      "preset_type": "openai-compatible",
      "api_type": "openai-compatible",
      "base_url": "http://localhost:3000/v1",
      "api_key_configured": true,
      "custom_headers": {},
      "additional_settings": {},
      "enabled": true,
      "model_count": 42,
      "created_at": "2026-09-15T08:00:00Z",
      "updated_at": "2026-09-15T08:00:00Z",
      "updated_by": null
    }
  ],
  "total": 1
}
```

`POST` 请求体：

```json
{
  "id": "gateway-main",
  "name": "自建网关",
  "preset_type": "openai-compatible",
  "api_type": "openai-compatible",
  "base_url": "http://localhost:3000/v1",
  "api_key": "qc-...",
  "custom_headers": {},
  "additional_settings": {},
  "enabled": true
}
```

- `id`：`POST` 必填，1–50 字符，只允许 `A-Za-z0-9._-`，且不能与已有条目重复（重复返回 `409` 沿用
  `VALIDATION_ERROR`）。`PATCH` 不接受 `id`。
- `name`：1–100 字符。
- `preset_type`：必须是 §5.12 预设目录中的 key。
- `api_type`：必须是 `openai-compatible` / `openai-responses` / `anthropic` / `gemini` /
  `amazon-bedrock`；省略时按 `preset_type` 取默认值。
- `base_url`：不超过 500 字符，`http`/`https`；可留空表示用预设默认端点。
- `api_key`：不超过 500 字符。`PATCH` 时**空串视为不修改**，显式 `null` 清除凭据。
- `custom_headers`：`{key: value}` 字符串映射，最多 20 项，值不超过 500 字符。
- `additional_settings`：任意 JSON 对象，供协议族专属配置预留。

`PATCH` 语义与 §5.7 一致：字段省略 = 不改动，显式 `null` = 清除（回退预设默认值）。

`DELETE` 响应 `204`（无正文）。删除会**级联删除**该 Provider 下的全部模型条目；
若有 `agent_configs.llm_model_id` 或 `provider_configs.default_llm_model_id` 仍指向被删条目，
这些引用退化为未绑定（不报错，见 §5.7）。为降低误删风险，`DELETE` 默认拒绝仍存在
`enabled=true` 模型条目的 Provider，返回 `409 PROVIDER_IN_USE`；
带查询参数 `?force=true` 时强制级联删除。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `PROVIDER_NOT_FOUND` | 条目不存在（`GET`/`PATCH`/`DELETE`） |
| 409 | `PROVIDER_IN_USE` | 仍有启用的模型引用该 Provider 且未传 `force=true` |
| 422 | 框架默认或 `VALIDATION_ERROR` | 校验失败（重复 id、非法 preset/api type、非法 base_url 等） |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储读写失败 |

审计：创建/修改/删除各产生 `event=config.provider_registry.created|updated|deleted`，
含 `id`、变更字段名与脱敏快照（`api_key` 只记 `set`/`unset`）。

### 5.10 模型注册表与批量引入

`GET /api/v1/config/models`、`POST /api/v1/config/models`、`POST /api/v1/config/models/batch`、
`PATCH|DELETE /api/v1/config/models/{model_id}`、
`GET /api/v1/config/providers/{provider_id}/models/discover`

模型条目属于某个 Provider，承载**特化调参**（ADR-017）。

`GET /api/v1/config/models?provider_id=&enabled=` 响应：

```json
{
  "items": [
    {
      "id": "gateway-main:gpt-4o-mini",
      "provider_id": "gateway-main",
      "model": "gpt-4o-mini",
      "name": "GPT-4o mini",
      "enabled": true,
      "reasoning_type": "none",
      "temperature": 0.2,
      "top_p": null,
      "max_context_tokens": 128000,
      "max_output_tokens": 4096,
      "custom_parameters": [],
      "modalities": ["text", "vision"],
      "created_at": "2026-09-15T08:00:00Z",
      "updated_at": "2026-09-15T08:00:00Z",
      "updated_by": null
    }
  ],
  "total": 1
}
```

`POST /api/v1/config/models`（新增单条）请求体：

```json
{
  "provider_id": "gateway-main",
  "model": "gpt-4o-mini",
  "name": "GPT-4o mini",
  "enabled": true,
  "reasoning_type": "none",
  "temperature": 0.2,
  "top_p": 0.9,
  "max_context_tokens": 128000,
  "max_output_tokens": 4096,
  "custom_parameters": [{ "key": "thinking_budget", "value": "2048", "type": "number" }],
  "modalities": ["text", "vision"]
}
```

- `id` 可省略，缺省由 `provider_id` + `model` 派生为 `{provider_id}:{model}`；
  显式提供时 1–80 字符且不能重复。
- `provider_id` 必须指向存在的 Provider，否则 `404 PROVIDER_NOT_FOUND`。
- `(provider_id, model)` 唯一；重复创建返回 `409`。
- `reasoning_type`：`none` / `openai` / `gemini` / `anthropic`。
- `temperature` `0.0`–`2.0`；`top_p` `0.0`–`1.0`；两个 token 上限为 ≥1 的整数。
- `custom_parameters`：最多 32 项，每项 `{key, value, type}`，`type` ∈
  `text` / `number` / `boolean` / `json`；`key` 1–100 字符且同条目内唯一。
- `modalities`：`text` / `vision` / `pdf` 的子集，去重。

`POST /api/v1/config/models/batch`（**批量引入**）请求体：

```json
{
  "provider_id": "gateway-main",
  "models": ["gpt-4o-mini", "gpt-4o", "deepseek-chat"],
  "name_prefix": "",
  "enabled": true,
  "defaults": { "temperature": 0.2, "max_output_tokens": 4096, "modalities": ["text"] }
}
```

响应 `200`：

```json
{
  "created": ["gateway-main:gpt-4o-mini", "gateway-main:deepseek-chat"],
  "skipped": [{ "model": "gpt-4o", "reason": "already_exists" }],
  "total_requested": 3,
  "provider_id": "gateway-main"
}
```

- `models` 去空白、去重后逐条插入；`(provider_id, model)` 已存在时计入 `skipped`
  而**不**报错，因此重复提交天然幂等。
- 单次上限 200 条，超出返回 `422`。
- `defaults` 只接受 `temperature` / `top_p` / `max_context_tokens` / `max_output_tokens` /
  `reasoning_type` / `modalities`，用于给这一批新条目设共同默认值；单项仍可在导入后
  `PATCH` 调整（与参考实现「批量添加使用默认参数，可在添加后单独调整」一致）。
- `defaults` 里的字段**不会**覆盖已存在条目（已存在条目只计入 `skipped`）。

`GET /api/v1/config/providers/{provider_id}/models/discover`（**远端发现**，批量引入的数据源）：

```json
{
  "provider_id": "gateway-main",
  "source": "remote",
  "items": [{ "id": "gpt-4o-mini", "name": "gpt-4o-mini", "owned_by": "openai" }],
  "existing": ["gpt-4o-mini"],
  "total": 1
}
```

- 由**服务端**发起请求，避免浏览器直连触发 CORS 与凭据外泄。
- 按 `api_type` 选择探测路径：
  `openai-compatible` / `openai-responses` → `{base_url}/models`（失败时回退
  `{base_url}/v1/models`）；`anthropic` → `{base_url}/v1/models`；
  `gemini` → `{base_url}/v1beta/models`；`ollama` 预设 → `{base_url}/api/tags`。
- `amazon-bedrock` 不支持发现，返回 `502 PROVIDER_DISCOVERY_FAILED` 并在 `message` 说明原因。
- `existing` 列出该 Provider 下**已经登记**的模型名，供前端在批量导入前提示去重。
- 超时 10 秒、响应体积上限 2 MiB；失败统一返回 `502 PROVIDER_DISCOVERY_FAILED`，
  `message` 不含凭据。该接口不写数据库。

`PATCH /api/v1/config/models/{model_id}`：字段省略 = 不改动，显式 `null` = 清除该字段
（回到「未设置」而非 Provider 默认值）。`provider_id` 不可通过 `PATCH` 修改。

`model_id` 由 `{provider_id}:{model}` 派生，model 名常含 `/`（如 `BAAI/bge-m3`），
路由按 `{model_id:path}` 匹配——客户端把 id 整体 `encodeURIComponent` 后拼进路径即可，
`%2F` 会被服务端正确解析为 id 的一部分（普通单段路由会 404，此为 2026-09-16 修复）。

`DELETE /api/v1/config/models/{model_id}` 响应 `204`。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `PROVIDER_NOT_FOUND` | `provider_id` 不存在 |
| 404 | `MODEL_NOT_FOUND` | 模型条目不存在 |
| 409 | `VALIDATION_ERROR` | 同类条目已存在（`model` 或 `id` 重复） |
| 422 | 框架默认或 `VALIDATION_ERROR` | 取值非法、批量超过 200 条 |
| 502 | `PROVIDER_DISCOVERY_FAILED` | 远端发现失败 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储读写失败 |

审计：`event=config.model.created|updated|deleted|batch_imported`，
批量导入额外记 `created` / `skipped` 数量。

### 5.11 MCP Server 注册表与工具目录

`GET|POST /api/v1/config/mcp/servers`、`GET|PATCH|DELETE /api/v1/config/mcp/servers/{server_id}`、
`POST /api/v1/config/mcp/servers/{server_id}/discover`、`GET /api/v1/config/mcp/tools`

多 Server 注册表（ADR-017）。三种以上传输方式由条目自身的 `transport` 决定，
不再只依赖全局 `MCP_TRANSPORT`（后者仍是**编排层**默认传输，见 §5.3）。

`GET /api/v1/config/mcp/servers` 响应：

```json
{
  "items": [
    {
      "id": "filesystem",
      "name": "本地文件系统",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {},
      "cwd": null,
      "url": null,
      "headers": {},
      "enabled": true,
      "tool_options": { "read_file": { "disabled": false } },
      "tool_count": 8,
      "discovered_at": "2026-09-15T08:00:00Z",
      "server_info": { "name": "filesystem", "version": "1.0.0" },
      "created_at": "2026-09-15T08:00:00Z",
      "updated_at": "2026-09-15T08:00:00Z",
      "updated_by": null
    }
  ],
  "total": 1
}
```

- `transport`：`stdio` / `http` / `sse` / `ws`。
- `transport=stdio` 时必填 `command`（1–500 字符），`args` 为字符串数组（≤64 项），
  `env` 为 `{key: value}`，`cwd` 可选；此时 `url` 必须为空。
- `transport` 为 `http`/`sse`/`ws` 时必填 `url`（`http(s)` / `ws(s)`），可选 `headers`；
  此时 `command`/`args`/`env`/`cwd` 必须为空。
- `tool_options`：`{toolName: {disabled?: bool, allowAutoExecution?: bool}}`，
  用于按工具开关与自动执行策略。
- `tool_count` / `discovered_at` / `server_info` 来自 `discovered` 缓存；
  从未发现过时 `tool_count` 为 `0`、后两者为 `null`。

`PATCH` 语义同 §5.7（省略 = 不改动，`null` = 清除）。

`POST /api/v1/config/mcp/servers/{server_id}/discover`：按条目配置建立连接、执行握手并
`list_tools()`，把结果写入 `discovered` 缓存后返回：

```json
{
  "server_id": "filesystem",
  "server_info": { "name": "filesystem", "version": "1.0.0" },
  "tools": [{ "name": "read_file", "description": "读取文件内容" }],
  "total": 1,
  "discovered_at": "2026-09-15T08:00:00Z"
}
```

失败（连接不上、握手失败、列工具报错、超时 15 秒）返回 `502 MCP_DISCOVERY_FAILED`，
`message` 为归一化后的错误原因，不含凭据与命令全文中的敏感值。发现结果**只**写
`discovered` 缓存，不改 `enabled`。

`GET /api/v1/config/mcp/tools`：按 Server 分组的**紧凑**工具目录，供配置页渲染卡片。
**不**内联 `input_schema`——Schema 体积大且多数时候不影响「这个工具要不要开」的判断：

```json
{
  "items": [
    {
      "server_id": "filesystem",
      "server_name": "本地文件系统",
      "enabled": true,
      "name": "read_file",
      "description": "读取文件内容",
      "tool_enabled": true,
      "available": true
    }
  ],
  "total": 1,
  "servers": [{ "id": "filesystem", "name": "本地文件系统", "tool_count": 1, "enabled": true }]
}
```

- `available` 表示该工具在最近一次发现结果里存在（配置的函数，不是实时连接状态）。
- 需要完整 Schema 时走 §5.3 的 `GET /api/v1/tools`。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `MCP_SERVER_NOT_FOUND` | 条目不存在 |
| 422 | 框架默认或 `VALIDATION_ERROR` | 传输方式与参数字段不匹配、非法 URL、重复 id |
| 502 | `MCP_DISCOVERY_FAILED` | 发现失败 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储读写失败 |

### 5.12 Provider 预设目录

`GET /api/v1/config/provider-presets`

返回 §5.12 预设族目录，供前端 Provider 选择器与表单预填。不读数据库、不需要凭据。

```json
{
  "items": [
    {
      "preset_type": "deepseek",
      "label": "DeepSeek",
      "monogram": "深度",
      "tint": "blue",
      "category": "cn",
      "default_api_type": "openai-compatible",
      "supported_api_types": ["openai-compatible", "anthropic", "openai-responses"],
      "default_base_url": "https://api.deepseek.com/v1",
      "requires_api_key": true,
      "api_key_url": "https://platform.deepseek.com/api_keys",
      "supports_model_discovery": true
    }
  ],
  "categories": [
    { "id": "all", "label": "全部" },
    { "id": "main", "label": "国际主流" },
    { "id": "cn", "label": "国内" },
    { "id": "gateway", "label": "聚合网关" },
    { "id": "cloud", "label": "云托管" },
    { "id": "local", "label": "本地" }
  ]
}
```

- `tint` 是前端配色 token 名（`blue` / `indigo` / `purple` / `rose` / `amber` / `orange` /
  `teal` / `green` / `pink` / `slate` / `ink`），`monogram` 是无 logo 时的文字标记。
- `supports_model_discovery` 为 `false` 的预设（如 `amazon-bedrock`）不支持 §5.10 的远端发现。

### 5.13 历史会话列表

`GET /api/v1/sessions?page=1&page_size=20`

按 `updated_at` 倒序分页返回历史会话，供「任务记录 → 历史会话」分区展示。会话与消息
持久化在 PostgreSQL（`doc/data-model.md` §3），重启不丢失；本接口是把它们重新「捞出来」
的唯一入口。

**服务端过滤（接口层兜底）**：只返回**至少有一条 `messages` 记录**的会话；`items` 与 `total`
按同一条过滤条件计算，否则分页会错位。前端已按 §4.2 的调用时机在首条消息提交时才建会话
（刷新与「新建任务」都停留在不落库的草稿态），正常链路不会产生空会话——这条过滤是接口层
的兜底，防止绕过前端直接 `POST /sessions` 造出空行堆在历史列表里。

判定条件是「存在消息」而非「存在 `role=user` 的消息」：消息在 workflow 之前写入（§4.4），
所以**任何真正跑过任务的会话都必然命中**，不会把有 workflow 的会话误过滤掉。

每个列表项在 §4.2 Session 字段之外，额外携带两个摘要字段，省去逐会话二次请求：

```json
{
  "items": [
    {
      "id": "3f2b…",
      "user_id": "demo-user",
      "status": "active",
      "title": "帮我分析这份数据",
      "latest_workflow_status": "completed",
      "latest_workflow_id": "9c7d…",
      "created_at": "2026-09-16T02:00:00Z",
      "updated_at": "2026-09-16T02:01:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 118
}
```

- `title`：该会话**首条** `role=user` 消息的内容，截断到 60 字（超长加省略号）。服务端过滤
  已经保证列表每一项都至少有消息，所以「无消息」的回退文案在列表里实际不可达，保留它只为
  字段契约完整（消息内容为空串时回退为 `「无文本消息」`）。
- `latest_workflow_status` / `latest_workflow_id`：该会话 `created_at` 最新的一条 Workflow
  的终态与 ID；从未提交过任务时为 `null`。前端据此用 §4.8 的 `GET /workflows/{id}` 恢复执行台。

分页约定与 §4.5 一致：`page_size` 上限 100。

### 5.14 删除会话

`DELETE /api/v1/sessions/{session_id}`

删除会话及其关联数据（消息、运行记录、工具调用、观测采样），返回 `204`（无正文）。
会话不存在返回 `404 SESSION_NOT_FOUND`（与 §4.2 `GET /sessions/{id}` 一致）。

删除是**级联且不可恢复**的：

- `messages` / `agent_runs` / `workflow_runs` 按 `session_id` 一并删除；
- `tool_calls` 按该会话的 `run_id` / `workflow_run_id` 删除；
- `metrics` 按 `labels.workflow_id` 删除（尽力而为，失败不阻断主流程）。

前端在「侧栏当前任务下拉」与「任务记录 → 历史会话」两处提供删除入口，删除前必须
二次确认（一律用行内确认，禁用原生对话框，见 §7）；若删除的是当前会话，前端回退到
**草稿态**的新对话模板（`session = null`，**不**新建会话），避免工作台继续指向已删除
的会话。两处入口都必须在**删除请求成功后**重新拉取 §5.13 的列表再渲染，否则会出现
「删掉了但列表里还在」的假象（侧栏下拉原先只在展开时拉一次；记录页原先与删除并发拉取，
存在竞态）。

### 5.15 查询执行边界（沙箱状态，只读）

`GET /api/v1/config/sandbox`

响应 `200`：

```json
{
  "backend": "docker",
  "image": "python:3.12-slim",
  "available": false,
  "reason": "Docker 守护进程不可达；常见原因是 backend 容器未挂载 /var/run/docker.sock 或当前用户无权访问该套接字",
  "limits": {
    "timeout_seconds": 15,
    "memory_limit": "256m",
    "cpu_limit": 0.5,
    "pids_limit": 64,
    "network_enabled": false,
    "output_limit_chars": 4000,
    "max_code_chars": 20000
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `backend` | `docker`（容器隔离）或 `denied`（显式拒绝执行） |
| `image` | 容器隔离使用的镜像 |
| `available` | 运行期探测结果；`false` 时工具调用会以 `SandboxUnavailable` 失败 |
| `reason` | 不可用原因；可用时为 `null` |
| `limits` | 当前生效限额，逐项对应 `SandboxSettings` |

**只读，没有写接口**（`PUT`/`POST` 返回 `405`）。这些参数是部署期安全边界——做成运行时可改的
界面等于让 Web 操作者放宽自己容器的隔离；且当前 `available=false` 的原因是缺 docker.sock，
改限额不会让它变可用，那样的开关必然是个假开关。决策与理由见 ADR-020。

前端入口：「工具与配置 → 执行边界」，只展示不编辑。

### 5.16 多模态附件（上传 / 下载 / 删除）

支撑「图片理解、文档理解」（ADR-021）。附件是**一等资源**：先上传登记拿到 id，发消息时用
`attachment_ids` 引用（§4.4）。**内容不进消息体、不进编排链路**——Dapr 的 gRPC 默认限制 4 MB，
链路里流动的永远只是 id。

#### 5.16.1 上传并解析

`POST /api/v1/attachments`

```json
{
  "name": "架构草图.png",
  "mime": "image/png",
  "data_base64": "iVBORw0KGgoAAAANSUhEUg..."
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 原始文件名，服务端会做折叠与清洗（`sanitize_name`） |
| `mime` | 否 | 浏览器给的 MIME；缺失时按 `application/octet-stream` 处理 |
| `data_base64` | 是 | 文件字节的 base64（**不带** `data:` 前缀；带前缀也收） |

用 JSON + base64 而不是 multipart：`python-multipart` 当前只是 `mcp` 的传递依赖，
为一个上传接口把它提成一等依赖要动 `uv.lock`，收益不抵代价（ADR-021）。

响应 `201`：附件对象（与 §4.4 响应里的 `attachments` 同构）。

| 字段 | 说明 |
| --- | --- |
| `kind` | `image` / `text` / `document` |
| `status` | `ready`（正文已抽出）/ `failed`（登记成功但正文取不出来，`error` 说明原因） |
| `size_bytes` | 原始字节数。**注意是原文件大小**，不是抽取出来的文本长度 |
| `text_content` | **不在响应里回传**。它只在执行阶段被读进提示词（§5.16.2 对文本/文档也不提供下载） |

`failed` **仍然是登记成功的附件**（有 id、可挂消息、会出现在气泡里）。它只是没进模型上下文——
这一点必须如实显示，否则用户会以为那份扫描件被读进去了。

错误码：

| 状态 | 码 | 触发条件 |
| --- | --- | --- |
| 400 | `ATTACHMENT_INVALID_BASE64` | `data_base64` 不是合法 base64 |
| 400 | `ATTACHMENT_EMPTY` | 零字节 |
| 400 | `ATTACHMENT_TOO_LARGE` | 超过 5 MB |
| 400 | `ATTACHMENT_TYPE_UNSUPPORTED` | 扩展名不在白名单 |

校验顺序即上表顺序：先解 base64，再判空与大小，最后判类型。**不支持的类型与超限一律在上传时拒**
（`AttachmentRejected` → `400`）——收下一个永远用不上的附件，等于在界面上给用户一个「我传上去了」
的假信号。

限额（`app/attachments/spec.py`，前端 `frontend/src/workspace/attachments.ts` 持同一份口径）：
单文件 5 MB / 单条消息 4 个 / 单文件 2 万字符 / 单条消息合计 4 万字符。前端那份**只做即时反馈**，
准入判据始终在服务端。

#### 5.16.2 读取附件原始内容

`GET /api/v1/attachments/{attachment_id}/content`

**只有图片能取到字节。**

- 图片：返回原始字节，`Content-Type` 为登记的 `mime`；前端用它渲染气泡里的缩略图与「查看原图」；
- 文本 / 文档：**`404 ATTACHMENT_CONTENT_UNAVAILABLE`**。它们在 `POST` 时就已经把正文抽进
  `text_content`，原始字节按设计丢弃（省库体积，执行阶段也不必再解析一遍）；
- 解析失败的附件、不存在的 id：同样 `404`。

回 `404` 而不是空响应：空响应会被前端当成一份有效内容渲染出来，「没内容」和「内容是空的」是两件事。

响应头带 RFC 5987 的 `Content-Disposition`（中文文件名用 `filename*=UTF-8''...`，避免头部按
latin-1 编码报错）。

#### 5.16.3 删除附件

`DELETE /api/v1/attachments/{attachment_id}`

| 状态 | 码 | 情况 |
| --- | --- | --- |
| 204 | — | 未归属任何消息（`message_id IS NULL`），已删除 |
| 404 | `ATTACHMENT_NOT_FOUND` | id 不存在 |
| 409 | `ATTACHMENT_ALREADY_SENT` | 已随消息发出，不能单独删除 |

已归属消息的附件**不允许**单独删：那会让历史消息里的附件引用变成空洞，历史记录该是只读的。
删会话时由外键级联清掉（`ON DELETE CASCADE`，见 `doc/data-model.md` §3.1）。要清理请连消息一起删
（会话级联见 §5.14）。

前端在「移除 chip」与「换任务」时调用它清理未发出的附件，失败不阻断界面
（最坏情况只留一条未归属的附件行）。

## 6. 规划接口（当前未实现）

下列接口已列入设计方向，但当前 FastAPI 不提供路由，前端不得直接调用：

| 方法 | 路径 | 规划用途 |
| --- | --- | --- |
| POST | `/api/v1/agents/{agent_id}/run` | 单 Agent 调试执行 |

### 6.1 规划请求示例（不保证可用）

```json
POST /api/v1/agents/{agent_id}/run
{
  "content": "只跑收集阶段的调试输入"
}
```

该接口落地前，必须先补充 Pydantic Schema、存储写入、权限边界、审计记录和测试，并同步更新本文档。

`PATCH /api/v1/config/agents/{agent_id}` 已按上述要求实现，见 §5.7。

## 7. 前端对接约束

- 初始化顺序：只调用 `GET /agents`，**不**建会话（草稿态，见 §4.2）。会话在提交首条消息时
  由 `POST /sessions`（§4.2）接着 `POST /sessions/{id}/messages`（§4.4）连成一步完成，用户
  只感知到「发出去了一条消息」。页面刷新回到草稿态，看历史会话走 §5.13。
- 发送消息后保存 `workflow_id`，每 2 秒轮询一次 Workflow；终态为 `completed`、`failed`、`cancelled` 时停止轮询。
- Token 与调用明细由 §5 读取；区分加载、失败、未接入、无记录、有记录，运行时轮询，终态补刷。切换 Workflow 时丢弃旧请求结果；调用和指标独立失败，不能阻断会话功能。任务标题取用户消息摘要。
- Provider 配置（§5.8）已有前端入口：页面直接读写生效配置，**不需要令牌**（ADR-015）。页面只提交被改动的字段，凭据输入框留空表示不修改；由于响应不含密钥，页面不会回显凭据原值。其余只读展示继续走 §5.1 / §4.9 / §5.2 与 §5.8 的 `GET`。
- 配置页在 ADR-017 之后按「注册表优先、默认路由兜底」组织：
  1. Provider 列表走 §5.9；新建时用 §5.12 预设目录预填 `preset_type` / `api_type` / `base_url`。
  2. 模型列表走 §5.10；「批量引入」先调 `discover`（§5.10）拿到远端清单，
     再用返回的 `existing` 标出已登记项，选中后提交 `POST /api/v1/config/models/batch`。
  3. 默认模型用 §5.8 的 `PUT` 写 `default_llm_model_id`。
  4. MCP 走 §5.11；工具卡片只展示 `name` / 截断后的 `description` / 开关与可用性，
     完整 `input_schema` 收进折叠区，且默认折叠。
- **页面归属**（按「写配置 / 角色路由 / 观测」三分，避免同一接口在多个页面各写一遍）：
  1. 「工具与配置」`frontend/src/config/ConfigPage.tsx` —— 只放**写配置**的三个分区：
     Provider、默认路由、MCP 工具（上一条 1–4）。
     三个分区用页面级副路由切换（`components/PageTabs.tsx`）。版式约定：标题与副路由
     左对齐全宽，其下内容限宽 1180px 居中（`styles.css` 的
     `.config-page > :not(.page-heading):not(.ui-tabs)`）——内容拉满整行会让
     registry 详情卡在宽屏下长得离谱。
  2. 「Agent 团队」`frontend/src/App.tsx::AgentTeamPage` —— 角色 ↔ 模型绑定与参数覆盖（§5.7），
     入参 `activeAgentId` 只用于高亮当前阶段角色，不参与读写。
     页面形态：角色按**方块网格**（`config/AgentPanel.tsx`）一行多个排列，方块只承载摘要
     （名字、`role · 状态`、生效模型、Temperature、覆盖项数，以及「当前阶段」标记）；
     编辑表单在 `AgentTuningPanel` 里，点开方块后挂在网格下方，同一时刻只编辑一个角色。
     网格里不放输入项——六个输入框会把同一行的其它方块顶变形。
  3. 「任务记录」`frontend/src/records/RecordsPage.tsx` —— 执行结果的**观测**数据。
     内部再分三个副路由，分区依据是「记录产生的位置」而不是数据类型：
     `runs` 运行记录（当前会话最近一次执行的终态与检查点）、
     `calls` 工具调用（§5.5 的 tool-calls 链路）、
     `metrics` 指标采样（§5.6）。三者的体量与读取频率差很多，同页混排会互相淹没。
     容器与行渲染器在 `frontend/src/records/Inspection.tsx`
     （`Records` 统一「加载中 / 失败 / 未接入 / 无记录 / 有数据」五态，分页仅在多页时出现）。
- 全站版式与控件复用：
  - 页面级标题一律用 `.page-heading`（眉标 + `h1` + 一句话释义），左对齐、不居中。
  - 页面级副路由一律用 `components/PageTabs.tsx`（`role="tablist"`、←/→ 键盘可达、
    `width: fit-content`），卡片内的次级切换用同组件的 `variant="inline"`；
    旧 `.cfg-tabs` / `.cfg-tab` 已废弃，不要再新增。
  - 状态胶囊与状态文案唯一来源是 `components/Status.tsx`（`Status`、`statusText`、
    `toolCallStatusText`），不要在页面里各写一份映射。
  - **危险操作的行内二次确认一律用 `components/InlineConfirm.tsx`**（`InlineConfirm`
    触发式、`InlineConfirmBar` 直接渲染确认条），**全站禁止 `window.confirm` /
    `alert` / `prompt`**。原生对话框由宿主提供：内置预览的 sandbox iframe（无
    `allow-modals`）、浏览器「阻止此页面创建更多对话框」、Electron/CEF 外壳都会屏蔽它，
    被屏蔽时 `confirm()` 不弹窗、直接返回 `false`，把删除挂在返回值上的写法会静默失效
    （表现为「点了删除没反应」，2026-09-16 用户实测）。行内确认是普通 DOM，不可被屏蔽。
    删除类入口的定位与悬停显隐挂在 slot 上（`.record-run-delete-slot`、
    `.cfg-agent-delete-slot`），armed 后确认条要**原地替换**按钮、不产生位移。
    后端先拒绝、再由用户升级的场景（如 Provider 的 `PROVIDER_IN_USE` 409 → 强制级联删除）
    用 `InlineConfirmBar`，视觉与前者一致但不再套一层 armed 循环。
  - 设计令牌 `--cfg-*` 只定义在 `styles.css` 的 `:root`（唯一的全局样式表），
    另有 `config/config.css`（cfg 设计系统）与 `records/records.css`（观测卡片）。
- 远端发现（§5.10 的 `discover`、§5.11 的 `discover`）只能由**用户显式操作**触发，
  不得在页面加载时自动调用：它会向用户填写的地址发起出站请求。
- Agent 覆盖（§5.7）的前端入口：`GET /api/v1/config/agents` 一次取回全部角色与
  可选模型清单，`PATCH` 保存；表单默认不展开，点开角色方块后才渲染输入项。
- 工作台内的三块视图职责互斥，不重复渲染同一份数据（ADR-018）：
  1. **Agent 执行台**（`App.tsx` 内联，卡片类名 `dock-node cli-node`）——按 Workflow 的
     `checkpoint.completed_steps` 与 `current_step` 展示阶段状态；不得在无 Workflow 时预填
     三张 Agent 卡片。卡片点击打开**单个 Agent 的阶段详情弹窗**
     （`frontend/src/workspace/AgentStageModal.tsx`），不改变右侧侧栏内容——侧栏是任务级
     视图，不跟随单卡点击而变。
  2. **任务协作侧栏**（`App.tsx::Inspector`）——只放任务级信息：整体状态、运行时长、
     协作链路（`frontend/src/workspace/CollaborationGraph.tsx`）与用量统计
     （`frontend/src/workspace/TaskUsage.tsx`）。协作链路按**波次**表达：波内并行、波间串行；
     当前后端是固定串行流水线，每波一个节点，编排层支持 fan-out 后只需把同波阶段放进
     同一个数组。用量按采样原值展示、不累加，口径同 §5.5。
  3. **任务记录页**——逐条工具调用与采样明细的唯一入口；侧栏与弹窗只给跳转入口。
     原先工作台侧栏内嵌的 `WorkflowInspection`（调用链路 + 任务采样合体）已随 ADR-018 删除，
     记录页继续分别复用 `ToolCallRecords` / `RuntimeSampling`。

## 8. 版本与变更规则

- 文档版本：`v0.7`，更新时间：2026-09-15。
- 任何新增或修改路由，先更新本文件的“已实现接口/规划接口”和对象 Schema，再修改代码。
- 若 OpenAPI 与本文档冲突，以实际路由和响应模型为准，并在同一变更中修正文档。

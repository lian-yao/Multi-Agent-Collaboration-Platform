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

**执行期间 Agent 拿到的工具比 §5.3 的静态目录多两个**：`list_session_files` / `read_session_file`
（ADR-025）按本次会话临时绑定，让 Agent 能按需读回这条会话里的附件正文。它们**不在**
`GET /api/v1/tools` 里，因为那份目录描述的是进程级注册表；要看它们是否真被调用，读 §5.4 的
工具调用记录。此外，附件内容本身也会随消息进入提示词（只进接收原始任务的根步骤），
两条路是互补的：前者是"模型已经看见了"，后者是"模型需要时再看一遍"。

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

受理成功时该用户消息同时追加进会话记忆（Redis，TTL 7 天，可丢失；写入失败降级为无操作，
不影响本次受理与响应），供同一会话后续轮次做上下文继承；报告正文由终态活动追加进记忆。
对外字段与状态码不变，接线口径见 ADR-019。

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

**用户登记的 MCP Server 的工具现在也会出现在这里**（ADR-026，2026-09-19 起）。`RegistryServersToolRegistry`（`app/mcp/registry.py`）会把启用 Server 的**已发现**工具合成进来，所以目录的实际内容是「内置工具 + 已发现的登记工具」。两点需要注意：

- **没「发现」过的 Server 不出现在目录里**：目录取自已发现的缓存（与 §5.11 同一条语义——「目录是配置的函数」），用户要在配置页点一次「发现」。读取发生在**每次执行**的工具枚举上，因此这条路径上不做任何 IO（进程内快照，配置变更时由配置面刷新）。
- **重名时内置优先**：与内置工具同名的登记工具不进目录，服务端另记一条 `mcp.registry_tool_name_clash` 日志；`tool_options.disabled` 为真的工具同样不进目录。
- **这条合成受 `MCP_INCLUDE_REGISTERED_SERVERS` 控制**：代码默认**关闭**（`app/mcp/config.py`），
  `deploy/compose.yaml` 显式打开（默认 `true`）。关掉后本目录只剩平台内置工具，已登记的
  Server 仍能在 §5.11 的配置目录里看到——排查「登记了、也发现过了，但 Agent 看不到工具」
  时先看这个开关。

**会话级工具不在这个目录里，这是有意的**（ADR-025）。`list_session_files` / `read_session_file` 的作用域是**一次执行**（绑定当时的 `session_id`），它们由 `session_scoped_registry()` 在阶段执行前临时拼进注册表，因此不进进程级目录、也不会出现在本接口的返回里。把它们算进来会让「工具与配置」页出现两个既关不掉、又在无会话执行里不存在的开关——不给假开关是本项目反复在修的毛病。要看它们是否真的被调用，读 §5.4 的工具调用记录（会出现在 `tool_calls` 里）。调用方需要知道的唯一一件事是：**任何依赖本目录来判断「Agent 有哪些工具」的逻辑都不完整**，运行期的工具集合是「本目录 + 本次会话的会话级工具」。

### 5.4 查询 Workflow 工具调用

GET /api/v1/workflows/{workflow_id}/tool-calls?page=1&page_size=20

item 字段为 id、run_id、workflow_run_id（可空）、tool_name、input（JSON 对象）、output（JSON 或 null）、status（running/succeeded/failed）、error（可空）、created_at、updated_at。读取 doc/data-model.md 的 tool_calls 表，按 created_at、id 升序分页，仅返回 workflow_run_id 匹配记录，或 workflow_run_id 为空且 run_id 匹配该 Workflow 的记录。Workflow 不存在返回 404 WORKFLOW_NOT_FOUND；表不存在返回 not_integrated。API 不创建表、不写审计记录。

### 5.5 查询执行指标

GET /api/v1/metrics?page=1&page_size=20

item 字段为 metric_name、value（有限数值）、labels（JSON 对象）、recorded_at。读取 doc/data-model.md 的 metrics 表，按 recorded_at、id 降序分页。可选 workflow_id 查询参数按 labels.workflow_id 严格过滤，未知 Workflow 返回 404；无参数时展示全局采样记录。表不存在返回 not_integrated。

`metrics` 表由 `app/core/checkpoint.py::MetricRecord` 定义，随 `init_checkpoint_schema()` 在 backend 进程启动时创建（见 `doc/data-model.md` §3）；从未启动过后端的空库会返回 `not_integrated`，这表示没建表，不等于“没有采样”。

指标展示保留原始 metric_name、labels、采样时间，不累加分页中可能重复的采样值。Token 名称交接约定为 input_tokens/output_tokens/total_tokens，数值 0 显示为 0，缺少采样显示“暂无采样”；比率和耗时由采集方定义后写入，前端不估算。C 负责采集、去重和 labels.workflow_id 关联，B 负责建表与审计写入，D 只负责读取和呈现；此约定需 A/B/C 联调验收。

**实际标签集**（`app/observability/context.py::ObservationContext`）：阶段级标签由编排层通过
`observed_stage(...)` 写入，static 与 dynamic 两条链路**当前都只传 `role`、不传 `agent_id`**
（`app/orchestration/pipeline_graph.py`、`dynamic_graph.py`），所以落地采样上的标签是
`workflow_id` / `stage` / `role`（Token 采样另有 `model`，阶段采样另有 `status`）。
`role` 与 §4.9 Agent 目录的 `id` 同值（collector / analyst / reporter），是当前**唯一**能把
Token 归到具体 Agent 的标签；`agent_id` 在采样里目前不存在。因此前端的归集顺序固定为
**`agent_id` → `role` → `model` → 任务级**：只认 `agent_id` 会让三个 Agent 的 Token 全部退到
`model` 一层、被合并成一个分组，界面上看就是「Token 没有分到 Agent 头上」（2026-09-21 实测）。
采样里出现 `role` 却不用它，比少一个字段更糟——数据在，是读法错了。

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
  `command` 在**建立连接时**解析：含路径分隔符按原值使用；裸名（如 `python`、`npx`）
  经 `PATH` 解析成绝对路径后再启动子进程。解析不到时不算配置非法（创建仍返回 201），
  而是「这条 Server 在当前环境连不上」：`discover` 回 502，合并进工具目录时跳过并记
  `registry.server_skipped`。**容器部署请写绝对路径**（如 `/app/.venv/bin/python`）——
  裸名能否解析取决于启动 backend 那个进程的 `PATH`，写绝对路径才不会随启动方式漂移。
- `transport` 为 `http`/`sse`/`ws` 时必填 `url`（`http(s)` / `ws(s)`），可选 `headers`；
  此时 `command`/`args`/`env`/`cwd` 必须为空。
- `tool_options`：`{toolName: {disabled?: bool, allowAutoExecution?: bool}}`。
  `disabled` **真的生效**（ADR-026）：为真的工具不进 §5.3 的工具目录，因而不会绑给模型。
  `allowAutoExecution` 仍然**不消费**——它要表达的是「调用前需要人工确认」，而 HITL 还没做；
  没有审批环节就无法正确表达这个语义，硬解释成「不暴露给模型」会让用户以为自己设的是
  「需要审批」而实际是「工具消失」。因此也不给它做界面开关（ADR-020 同一取向）。
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

发现结果**立即**对 Agent 生效（ADR-026）：服务端在写完缓存后会刷新编排层的工具快照，
下一次执行就能看到这批工具，**不需要重启进程**。Server 的新增 / 修改 / 删除同理
（`app/core/mcp_registry.py::_sync_orchestration_tools`）。

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
  "image": "multi-agent-collaboration-platform-backend:latest",
  "available": true,
  "reason": null,
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
| `available` | 探测结果：守护进程可达**且**镜像已在宿主机 |
| `reason` | 不可用时的**具体原因**；可用时为 `null` |
| `limits` | 当前生效限额，逐项对应 `SandboxSettings` |

`reason` 由后端自己给出（`Sandbox.unavailable_reason()`），不是接口层的兜底文案——**套接字没挂、
镜像不在宿主机、没装 Docker SDK 是三件需要三种不同处理的事**，界面上要分得开。探测本身炸了也
回 `200` 加原因：这是诊断接口，它自己 500 就没人能诊断了。

探测是**两件事**，缺一不可：

1. 守护进程可达（`ping`）；
2. 沙箱镜像已在**宿主机**上。沙箱容器是 backend 通过宿主机套接字创建的**兄弟容器**（ADR-023），
   镜像不在宿主机时只 `ping` 会得到「绿灯、但第一次执行代码就失败」的假象。

镜像缺失时 `reason` 会给出三条可执行的路：宿主机 `docker pull`、把 `SANDBOX_IMAGE` 指到本地
已有镜像、或开 `SANDBOX_AUTO_PULL_IMAGE`（默认**关**——拉取可能长时间阻塞，安全边界组件应当
失败得快、原因得准）。

**只读，没有写接口**（`PUT`/`POST` 返回 `405`）。这些参数是部署期安全边界——做成运行时可改的
界面等于让 Web 操作者放宽自己容器的隔离，那样的开关必然是个假开关。决策与理由见 ADR-020；
「沙箱在部署里真正可用」是 ADR-023。

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
| `status` | `ready` / `failed`（登记成功但正文与页面图**都**取不到，`error` 说明原因） |
| `size_bytes` | 原始字节数。**注意是原文件大小**，不是抽取出来的文本长度 |
| `has_original` | 原件字节是否还在库里（ADR-024）。`false` 只出现在旧策略之前落库的附件上——界面据此决定要不要给「打开原件」入口，不给必然 404 的链接 |
| `text_content` | **不在响应里回传**。它只在执行阶段被读进提示词；原件另由 §5.16.2 提供下载 |

`ready` 有两种形态，**受理字段完全一样、区别只在 `text_content` 与 `error`**：

- **有正文**：`text_content` 非空，`error` 一般为空；
- **无正文但有页面图**：扫描版 PDF，或内嵌字体子集字符码冲突的 PDF。执行阶段把页面渲成 PNG、
  当图片附件走视觉通路（ADR-027），此时 `text_content` 为 `null`，`error` 里带一句**降级说明**
  （如「这份 PDF 没有文本层（扫描件），已改用页面图像提供正文。」）。**前端必须显示这句说明**，
  否则用户会以为正文是被正常提取出来的。

`failed` **仍然是登记成功的附件**（有 id、可挂消息、会出现在气泡里）。它只是没进模型上下文——
这一点必须如实显示。ADR-027 之后扫描件不再落到这里（页面能渲成图就报 `ready`），
剩下的 `failed` 是「正文与页面图都取不到」：加密文档、渲染组件缺失（`error` 会点明
`pypdfium2`）、上传时探测通过但执行时渲染失败的极端情况。

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

#### 5.16.2 读取附件原件

`GET /api/v1/attachments/{attachment_id}/content`

回**原件字节**（ADR-024），按类型选处置方式：

- 图片：`Content-Type` 为登记的 `mime`，`Content-Disposition: inline`——气泡里的缩略图与
  「查看原图」要能直接渲染；
- 文本 / 文档（**含解析失败的附件**）：同一份原件，`Content-Disposition: attachment`——
  txt/docx/xlsx 在浏览器里没有渲染器，`inline` 只会开出一个空白页；
- id 不存在，或该行早于原件留档策略落库（`has_original=false`）：`404
  ATTACHMENT_CONTENT_UNAVAILABLE`。

回 `404` 而不是空响应：空响应会被前端当成一份有效内容渲染出来，「没内容」和「内容是空的」是两件事。

响应头带 RFC 5987 的 `Content-Disposition`（中文文件名用 `filename*=UTF-8''...`，避免头部按
latin-1 编码报错）。

前端用法：气泡里的条目在 `has_original=true` 时**本身就是这个地址的链接**——图片新窗口看原图，
其余带 `download` 属性直接存成上传时的文件名。`has_original=false` 的旧行不给入口，因为那会是
一个必然 404 的链接。

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

### 5.17 查询阶段执行轨迹（只读）

`GET /api/v1/workflows/{workflow_id}/stages`

执行台卡片弹窗（`frontend/src/workspace/AgentStageModal.tsx`）的数据源。与 §5.4 的分工是：
**§5.4 回答「这次任务调了哪些工具」，§5.17 回答「哪个 Agent 收到什么、调了什么、产出了什么」**，
两者不互相替代。

响应 `200`：

```json
{
  "workflow_id": "…",
  "mode": "static",
  "task": "统计上季度华东区的退货率",
  "availability": "available",
  "reason": null,
  "items": [
    {
      "stage": "analyze",
      "role": "analyst",
      "input": "已收齐三类原始数据……",
      "input_from": "collect",
      "output": "整体退货率 4.8%……",
      "tool_calls": [
        {
          "call_id": "…", "tool_name": "calculator", "status": "succeeded",
          "input": {"expression": "128/2680"}, "output": {"result": 0.0478}, "error": null
        }
      ],
      "truncated": false,
      "reason": null
    }
  ]
}
```

- `items` 按阶段固定顺序返回 `collect` / `analyze` / `report`（当前静态链路的三个阶段）。
- `input` 是本阶段**实际读到的上游正文**，`input_from` 标明它来自哪一步；根阶段两者都是 `null`
  （它收到的就是原始任务 `task`，不重复回传一份）。
- `tool_calls` 的元素结构与 §5.4 的 `tool_calls` 表同形。上限定为**单段正文 8000 字符、
  单次工具入参/出参 4000 字符**，超限不只是截断：正文被截断时 `truncated=true`，工具载荷则换成
  `{"truncated": true, "bytes": n, "preview": "…"}`。界面据此显示「已截断」——
  静默剪掉一段内容再当作全部展示，比不显示更糟。
- `reason` 与「有轨迹」互斥，写的是**为什么没有**：还没轮到 / 正在执行（轨迹在阶段完成后才落盘）/
  阶段已完成但状态已被清理 / 载荷无法解析。四种原因指向四种不同的下一步动作，前端必须原样显示，
  不得改写为「暂无数据」。
- `mode` 为 `dynamic` 时 `availability=not_integrated`、`items=[]`、`reason` 说明动态链路当前
  不落盘逐步骤轨迹（ADR-019）；界面同样原样显示这句原因。

| 状态 | 码 | 情况 |
| --- | --- | --- |
| 200 | — | 读到轨迹，或该阶段确实还没有轨迹（看 `reason`） |
| 404 | `WORKFLOW_NOT_FOUND` | Workflow 不存在 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 状态存储（Dapr sidecar）读不到 |

**数据来源与边界**：轨迹取自 Dapr State Store 里 `_record_checkpoint` 写入的阶段状态
（`app/api/stage_trace.py`）。因此 503 与 200+`reason` 必须分开——前者是环境没起来，后者是任务
还没跑到。模型内部的隐藏推理（reasoning / thinking 块）**不在本接口范围内**：编排层只落盘行动与
结论，接口不伪造中间过程。该接口**只读**：不写状态存储、不建表，也不触发任何阶段重跑。

### 5.18 查询会话的历史工作流（只读）

`GET /api/v1/sessions/{session_id}/workflows`

全屏协作画布（`frontend/src/workspace/CollabCanvas.tsx`）的数据源之一。与 §5.13 的分工是：
**§5.13 列的是「有哪些会话」，§5.18 列的是「这个会话里每一次对话各自跑出的协作工作流」**。
一次提交（一次对话）对应一条 Workflow，编号就是返回列表的**下标 + 1**。

响应 `200`：

```json
{
  "items": [
    {
      "id": "wf-…", "session_id": "s-…", "agent_run_id": "run-…",
      "status": "completed", "current_step": null,
      "checkpoint": {"status": "completed", "current_step": null,
                     "completed_steps": ["collect", "analyze", "report"],
                     "updated_at": "…"},
      "created_at": "…", "updated_at": "…", "completed_at": "…"
    },
    {
      "id": "wf-…", "session_id": "s-…", "agent_run_id": "run-…",
      "status": "running", "current_step": "report",
      "checkpoint": {"status": "running", "current_step": "report",
                     "completed_steps": ["collect", "analyze"],
                     "updated_at": "…"},
      "created_at": "…", "updated_at": "…", "completed_at": null
    }
  ],
  "total": 2
}
```

- **必须升序返回（按 `created_at`）**。编号由位置决定，倒序会让「对话 1」在新增一次对话后
  变成原来的「对话 2」，用户回看时点按的对话会整体错位。存储层只提供升序读法
  （`app/core/checkpoint.py::list_workflows_for_session`），接口不做二次排序。
- 元素结构与 §4.8 的 Workflow 同形，字段含义一致。
- `total` 是本次会话实际的工作流条数。**该列表不分页**：一个会话的工作流条数就是它的对话轮数，
  量级天然很小；上限由存储层的 `limit`（默认 200）兜底。
- 只读：不建会话、不建 Workflow。草稿态会话（还没有提交过消息）返回 `items: []`，
  前端据此显示空画布而不是报错。
- 前端不要用它替换「当前任务」下拉——那个下拉取的是 `latest_workflow_id`（§5.13），
  本接口不承担「哪个是当前任务」的判定。

| 状态 | 码 | 情况 |
| --- | --- | --- |
| 200 | — | 读到列表，可能为空 |
| 404 | `SESSION_NOT_FOUND` | 会话不存在 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储（PostgreSQL / Dapr）读不到 |

### 5.19 工作区（work_dir，阶段 1：只读）

`GET|POST /api/v1/workspaces`、`GET /api/v1/workspaces/{workspace_id}`、
`GET /api/v1/workspaces/{workspace_id}/tree`、`DELETE /api/v1/workspaces/{workspace_id}`

工作区的授权单位是**宿主固定根下的子目录**（`WORKSPACE_HOST_ROOT` 挂进容器的 `/workspace`，
见 ADR-033）：接口不接收宿主绝对路径，`path` 一律相对工作区根。绑定之后该会话的 Agent
多出 `list_work_files` / `read_work_file` 两个**会话级**工具（与 §5.3 的静态目录无关——
那份目录列的是进程级注册表）。

阶段 2 起 `workspace_write` 档位可用，提档后**再**多出三个**非破坏性**写工具：
`write_work_file`（只能新建）、`make_work_dir`、`move_work_entry`（目标已存在时拒绝）。
覆盖与删除按 ADR-033 §6 必须人工审批，随审批链路在阶段 3 上线——因此现在**没有**删除
工具，`write_work_file(overwrite=true)` 也返回 409 `WORKSPACE_APPROVAL_REQUIRED`。

`POST /api/v1/workspaces` 请求：

```json
{ "session_id": "3f2b…", "path": "sessions/3f2b…", "mode": "read_only", "name": null }
```

- `path` 省略或为空时默认绑到 `sessions/<session_id>/`；目录不存在时由**平台**创建。
- `mode` 取 `read_only` / `workspace_write`；创建时即可选写档位，也可用下面的 `PATCH` 改。
  **档位只能由人调整**（ADR-033 §3），Agent 没有提权通道：它不会成为工具。

响应 `201`（`GET /workspaces/{id}` 同形；列表为 `{"items": [...], "total": n}`）：

```json
{
  "id": "9f1c…",
  "session_id": "3f2b…",
  "path": "sessions/3f2b…",
  "mode": "read_only",
  "name": null,
  "quota": { "max_file_bytes": 5242880, "max_total_bytes": 268435456, "max_entries": 2000 },
  "usage": { "available": true, "total_bytes": 0, "entries": 0, "truncated": false, "scan_limit": 10000 },
  "created_by": null,
  "updated_by": null,
  "created_at": "2026-09-23T02:00:00Z"
}
```

- `usage` 按需扫描工作区目录得到（不落库，避免多写者下的计数漂移）；目录被删或工作区根
  不可用时为 `{"available": false, "reason": "…"}`，而不是整体 500。
- `quota` 三项都已生效：`max_file_bytes` 限读取与单次写入，`max_total_bytes` /
  `max_entries` 在写之前校验（超限返回 409 `WORKSPACE_QUOTA_EXCEEDED`，错误里带当前用量）。

`PATCH /api/v1/workspaces/{workspace_id}`：

```json
{ "mode": "workspace_write", "name": "项目工作区" }
```

字段省略表示不改动；响应与 `GET` 同形。提档会让该会话的 Agent 多出三个写工具，属于一次
**显式人工授权**，所以服务端记 `workspace.updated` 日志并在 `updated_by` 里回显操作者。

`GET /api/v1/workspaces/{workspace_id}/tree?path=&depth=1`：

```json
{
  "workspace_id": "9f1c…",
  "path": "",
  "depth": 1,
  "entries": [
    { "name": "reports", "path": "reports", "kind": "dir", "outside": false, "size_bytes": null, "modified_at": "2026-09-23T02:00:00Z" },
    { "name": "escape", "path": "escape", "kind": "symlink", "outside": true, "size_bytes": null, "modified_at": null }
  ],
  "truncated": false,
  "limit": 500
}
```

- `path` 相对**工作区**，`depth` 取值 1–8（超出按上限截断）。
- `kind` 取 `file` / `dir` / `symlink` / `other`；指向工作区之外的符号链接
  `outside=true` 且**不跟随**——不列它的子项，也不把根外的结构暴露出去。
- 条目数超过 `WORKSPACE_TREE_MAX_ENTRIES` 时 `truncated=true`。

`DELETE /api/v1/workspaces/{workspace_id}` 返回 204：只解除登记，**不删宿主文件**。

路径校验（ADR-033 §4）：只接受相对路径；`..`、绝对路径/盘符/UNC、Windows 保留名与
非法字符、NTFS 数据流，以及解析后落在工作区之外的符号链接一律拒绝。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `SESSION_NOT_FOUND` | 会话不存在（`session_id` 非空时先校验） |
| 404 | `WORKSPACE_NOT_FOUND` | 工作区不存在 |
| 409 | `WORKSPACE_EXISTS` | 该相对路径已登记 |
| 409 | `WORKSPACE_QUOTA_EXCEEDED` | 超过单文件 / 总字节 / 条目配额 |
| 409 | `WORKSPACE_APPROVAL_REQUIRED` | 覆盖或删除需要人工审批（阶段 3 提供） |
| 422 | `WORKSPACE_PATH_REJECTED` | 路径非法或越出工作区 |
| 422 | `VALIDATION_ERROR` | 其它取值问题（例如 `mode` 取值无效） |
| 503 | `WORKSPACE_DISABLED` | `WORKSPACE_ENABLED=false` |
| 503 | `WORKSPACE_ROOT_UNAVAILABLE` | 工作区根不存在或不是目录 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储读写失败 |

### 5.20 工作区审批（覆盖 / 删除）

`GET /api/v1/sessions/{session_id}/approvals`、
`POST /api/v1/approvals/{approval_id}/decision`

工作区里的**破坏性动作**要人工审批（ADR-033 §6）：覆盖已有文件、删除条目、覆盖式移动。
Agent 第一次调用只会**提交申请**并拿到一条非重试错误（文案里带审批 id），它应当停下来；
批准之后**重试同一调用**才真正执行。

```json
GET /api/v1/sessions/3f2b…/approvals?status=pending
{
  "items": [
    {
      "id": "ap-1",
      "workspace_id": "9f1c…",
      "session_id": "3f2b…",
      "run_id": null,
      "kind": "delete",
      "target": "reports/old.md",
      "reason": "删除工作区内的条目",
      "status": "pending",
      "payload": { "kind": "file" },
      "decided_by": null,
      "requested_at": "2026-09-23T02:01:00Z",
      "decided_at": null
    }
  ],
  "total": 1,
  "pending": 1
}
```

```json
POST /api/v1/approvals/ap-1/decision
{ "decision": "approved" }
```

`decision` 取 `approved` / `denied`；响应为该条审批的完整视图。语义要点：

- **一次一授权**：批准只放行「同工作区 + 同动作 + 同目标」的下一次调用，放行后该记录置
  `consumed`；再想覆盖/删除同一个目标要重新申请。
- **不覆盖既成决定**：只有 `pending` 可以决策，重复决策返回 409 `APPROVAL_NOT_PENDING`。
- **过期不放行**：超过 `WORKSPACE_APPROVAL_TTL_SECONDS`（默认 900 秒）未决策的 `pending`
  在列表/决策时被标成 `expired`——既不放行也不删记录。
- **不刷屏**：同一目标反复请求会复用同一条未决策记录；已批准的未消费记录也会被复用。
- `status` 取 `pending` / `approved` / `denied` / `expired` / `consumed`，省略或 `all`
  返回全部；列表响应额外给 `pending` 计数，供界面角标使用。
- `reason` 由平台生成，**不照抄模型输出**（提示注入会经由审批卡片影响人）。

错误码：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `SESSION_NOT_FOUND` | 会话不存在 |
| 404 | `APPROVAL_NOT_FOUND` | 审批记录不存在 |
| 409 | `APPROVAL_NOT_PENDING` | 该审批已被决策（或已过期），不能再次决策 |
| 422 | `VALIDATION_ERROR` | `decision` / `status` 取值不合法 |
| 503 | `DATA_SOURCE_UNAVAILABLE` | 存储读写失败 |

### 5.21 出网策略（只读）

`GET /api/v1/config/egress`

出网策略由环境变量决定（ADR-034），这里只做**只读投影**——能改策略的接口等于给了一条
绕过安全边界的路。响应 `200`：

```json
{
  "mode": "public_only",
  "allow_hosts": ["*.example.com"],
  "deny_hosts": ["blocked.example.com"],
  "internal_hosts": ["redis", "search-gateway"],
  "allowed_ports": [443, 8800],
  "model_exempt": true,
  "max_redirects": 5,
  "blocked": { "private_ip": 2, "denied_host": 1 }
}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `public_only`（默认，只放公网）/ `allowlist`（再要求域名命中白名单） |
| `allow_hosts` / `deny_hosts` | 域名白/黑名单，**黑名单优先**；按 label 边界匹配 |
| `internal_hosts` | 平台内部依赖的精确主机名，豁免私网判定（**不是"整个内网"**） |
| `allowed_ports` | 允许的端口；豁免私网判定不等于豁免端口 |
| `model_exempt` | 模型流量是否豁免私网判定（Ollama 与内网网关继续可用） |
| `blocked` | **进程内**按原因累计的拒绝次数。工具调用发生在 worker 进程，所以这里通常是空的——看全局要用 Prometheus 的 `macp_egress_blocked_total`（每个进程各自暴露，由 scrape 汇总） |

配置非法时返回 503 `VALIDATION_ERROR`（策略在首次构造时校验，不做"跳过这条规则"的宽容处理）。
被拒绝的调用不会重试：工具侧返回 `retryable=false`；MCP 远程条目的策略判定发生在
**建立会话时**（构造会话工厂保持零 IO，ADR-026），`discover` 因此返回
502 `MCP_DISCOVERY_FAILED`，调用期则表现为那次调用失败。

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

### 6.2 其余规划项

工作区的登记 / 目录树 / 读取 / 提档 / 四个写工具（含审批）见 §5.19 与 §5.20，均已实现。
出网策略的应用层判定与只读投影见 §5.21；**尚未落地**的是 ADR-034 §4 的网络层强制
（egress 代理）与 MCP / 模型 SDK 内的 IP 钉扎，理由与残余风险记在 ADR-034 的实现口径一节。

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
     方块左上角的 34px 图标位放**角色图标**（按 `role` 解析，见 ADR-029），不再放显示名首字；
     解析器与协作画布节点共用，见「全站版式与控件复用」的 `AgentGlyph` 一条。
  3. 「任务记录」`frontend/src/records/RecordsPage.tsx` —— 执行结果的**观测**数据。
     内部再分三个副路由，分区依据是「记录产生的位置」而不是数据类型：
     `runs` 运行记录（当前会话最近一次执行的终态与检查点）、
     `calls` 工具调用（§5.5 的 tool-calls 链路）、
     `metrics` 指标采样（§5.6）。三者的体量与读取频率差很多，同页混排会互相淹没。
     容器与行渲染器在 `frontend/src/records/Inspection.tsx`
     （`Records` 统一「加载中 / 失败 / 未接入 / 无记录 / 有数据」五态，分页仅在多页时出现）。
  4. 「工具与配置」新增 `workspaces` 分区（`config/WorkspacePanel.tsx`，§5.19）：
     工作区列表 + 选中后的目录树与配额。**登记与提档是人的动作**——它是本页唯一
     会改变 Agent 能力边界的分区，因此也放在「写配置」这一组里。
     「执行边界」分区扩成两段：沙箱（§5.15）+ **出网策略（§5.21，只读）**——
     两者都是部署期安全边界，只回答「现在是什么口径」，不给运行期开关。
  5. 对话流内的**审批卡片**（`workspace/ApprovalCard.tsx`，§5.20）：破坏性动作
     （覆盖 / 删除）的放行入口。挂在执行活动卡片里（`workspace/RunActivity.tsx`），
     与触发它的那次工具调用同处；顶栏「对话」入口带 pending 角标。

### 7.1 工作区与审批的前端约定（§5.19 / §5.20 / §5.21）

- **不要做「选择本地文件夹」的原生对话框**：浏览器给不了后端宿主路径。用户能选的是
  **服务端可见的根内子目录**（`WORKSPACE_HOST_ROOT` 之下），列表来自 `GET /workspaces`
  与 `GET /workspaces/{id}/tree`。文案照此写，避免让人以为能选任意路径（ADR-033 §1）。
- **档位只有两档，且提档是人的动作**：`read_only` ⇄ `workspace_write`（`PATCH`，§5.19）。
  **界面上不出现 `full_access`**——它没有实现，放上去就是假开关（ADR-033 §2）。
  档位切换要写明后果：「提档后本次会话的 Agent 多出三个写工具（新建 / 建目录 / 移动）」。
- **Agent 没有提权通道**：不要做「Agent 申请提权 → 用户点同意」的卡片。越界访问由服务端
  直接拒绝（`WORKSPACE_PATH_REJECTED`），界面只如实转述（ADR-033 §3）。
- **目录树是只读视图**：`kind=symlink` 且 `outside=true` 的条目要标出来且**不可展开**；
  它们不是可读内容，也不该在界面上看起来像普通目录。
- **审批卡片回答三个问题**：要动什么（`kind` + `target`）、为什么要人来看（`reason`，
  由平台生成）、决策后会发生什么。三个按钮状态要对齐服务端语义：
  `approved` = 「已允许，Agent 重试同一调用才会执行」、`denied` = 「已拒绝」、
  `consumed` = 「已放行过一次」、`expired` = 「已过期，未放行」。
  **不要把「需要审批」渲染成「失败」**：Agent 侧那条 `409 WORKSPACE_APPROVAL_REQUIRED`
  是流程的第二段，不是错误终点。
- **`allowAutoExecution` 不给界面出口**：它至今没有消费方（ADR-020 / ADR-026 同一取向），
  审批落地后它才有语义；在那之前不给假开关。
- **出网策略只读**：`GET /config/egress` 只展示模式、白/黑名单、内部服务、端口、
  模型豁免与**进程内**拒绝计数。计数在 worker 进程里增长，界面要么标注「本进程」，
  要么引导看 Prometheus 的 `macp_egress_blocked_total`——不要把 0 说成「没有被拦过」。
- 联调与冒烟：`frontend/rendercheck/config-smoke.tsx` 覆盖工作区面板与出网段的
  「加载 / 失败 / 空 / 有数据」四态；`workspace-smoke.tsx` 覆盖审批卡片在
  pending / approved / consumed / expired 四种状态下的渲染，且断言文案与服务端语义一致。
- 全站版式与控件复用：
  - 页面级标题一律用 `.page-heading`（眉标 + `h1` + 一句话释义），左对齐、不居中。
  - 页面级副路由一律用 `components/PageTabs.tsx`（`role="tablist"`、←/→ 键盘可达、
    `width: fit-content`），卡片内的次级切换用同组件的 `variant="inline"`；
    旧 `.cfg-tabs` / `.cfg-tab` 已废弃，不要再新增。
  - 状态胶囊与状态文案唯一来源是 `components/Status.tsx`（`Status`、`statusText`、
    `toolCallStatusText`），不要在页面里各写一份映射。
  - **Agent 角色图标的唯一来源是 `components/AgentGlyph.tsx`**（`AgentGlyph`、
    `agentIconKey`、`AGENT_ICON_GLYPHS`），配置页角色方块与协作画布节点都调它。
    口径：先按 `role` 匹配语义、再按显示名、最后回退机器人（`Bot`）；关键词表
    `AGENT_ICON_RULES` **顺序即优先级**。加图标往表里加一行即可，不要在视图里另做映射
    （两个视图各画各的，一致性会慢慢消失——这就是原先「配置页画首字、画布画机器人」
    的成因，见 ADR-029）。画布上**状态优先于角色**：`failed` / `paused` / `running`
    画各自的图形，其余才画角色图标。
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
- 工作台内的四块视图职责互斥。ADR-018 定的是「三块视图不重复渲染同一份数据」，
  **ADR-031 把对话流并入并改按时间划边界**——重复本身不是问题，**错位**才是：
  | 视图 | 回答什么 | 时间面 |
  | --- | --- | --- |
  | 对话流（`.run-activity`） | 这一次执行**当下**跑到哪、调了什么、拿到了什么、产出了什么 | 进行中 |
  | Agent 执行台弹窗（§5.17） | 单步的完整轨迹，含上游输入原文与截断标记 | 跑完之后回看 |
  | 任务协作侧栏 | 任务级整体状态、协作画布、用量 | 任务级 |
  | 任务记录页 | 跨任务的逐条工具调用与采样明细 | 审计 |
  四个面都**只读**同一批服务端数据（§5.17 / §5.5），谁都不写、不缓存，因此不存在「以哪一份为准」。
  1. **对话流**（`App.tsx::MessageBubble` + `workspace/RunActivity.tsx`）——消息正文与执行过程：
     - 正文一律走 Markdown 渲染（`components/Markdown.tsx`，GFM）。**不得**再退回
       `<p>{content}</p>`：模型按 Markdown 组织输出（标题、加粗、列表、表格、围栏代码块），
       直接塞进 `<p>` 会把记号原样吐出，结构全丢。渲染层**不挂 `rehype-raw`**——
       那等于把模型输出当 DOM 执行；`react-markdown` 默认不解析裸 HTML，这条要保持。
     - 正文的**渐进揭示**（`components/useStreamText.ts`）是**呈现效果，不是流式传输**：
       后端没有事件流，助手正文是工作流终态一次性落库的。因此前端不得据此宣称
       「正在逐 token 接收」；真要流式得先在后端开只读事件流端点。
       只对**本次会话新到达**的消息播放（历史会话整屏重放会让人以为任务在重新执行），
       非浏览器环境与 `prefers-reduced-motion: reduce` 下降级为立即全文。
     - 执行活动卡片摆在**提问之后、答复之前**（`reportIndex` 定位）：过程要出现在结果的
       上一个位置；排到整段对话末尾会看起来像另一个任务。
     - 逐步轨迹按 §5.17 如实呈现，三段小标题与执行台弹窗**逐字一致**
       （**分配到的任务 → 执行轨迹 → 阶段产出**）；同一份字段在两处用两套词会被读成
       两件事，冒烟里同时断言两条渲染路径。**不伪造思维链**——模型内部的隐藏推理没有落盘，
       卡片只呈现落盘过的事实：**不写口径脚注，也不复述任务原文**（这两条脚注已按评审删除；
       口径说明由执行台弹窗承载，见本条 2）。轨迹缺席时转述服务端给的具体原因，不留白。
     - 与弹窗的唯一差别：**根步骤不铺输入原文**（用户的原始任务就在上方那条用户消息里）。
     - 正在跑的那一步默认摊开、跑完的收成一行；摘要行必须仍写清
       「谁 / 什么状态 / 动了几次工具」，否则收起就是信息丢失。
  2. **Agent 执行台**（`App.tsx` 内联，卡片类名 `dock-node cli-node`）——按 Workflow 的
     `checkpoint.completed_steps` 与 `current_step` 展示阶段状态；不得在无 Workflow 时预填
     三张 Agent 卡片。卡片点击打开**单个 Agent 的执行轨迹弹窗**
     （`frontend/src/workspace/AgentStageModal.tsx`，数据走 §5.17），不改变右侧侧栏内容——
     侧栏是任务级视图，不跟随单卡点击而变。
     弹窗回答的是「它收到了什么、调了什么、产出了什么」：分配到的任务（上游正文）、按序的工具
     调用（含失败原因与截断标记）、阶段产出。**不放模型与参数**——角色绑定与调参属于
     「Agent 团队」页；`AgentTraceView` 是其中的纯视图部分，按显式 props 驱动以便离屏冒烟挂载。
     模型内部的隐藏推理没有落盘，弹窗如实说明这一点，不伪造「思维链」。
  3. **任务协作侧栏**（`App.tsx::Inspector`）——只放任务级信息：整体状态、运行时长、
     协作画布（`frontend/src/workspace/CollaborationGraph.tsx` → `GraphCanvas.tsx`）与用量统计
     （`frontend/src/workspace/TaskUsage.tsx`）。协作画布按**波次**表达：波内并行、波间串行；
     当前后端是固定串行流水线，每波一个节点，编排层支持 fan-out 后只需把同波阶段放进
     同一个数组，同波多节点会自动圈进「并行协作区」框。用量按采样原值展示、不累加，口径同 §5.5。
     **节点是圆形的 Agent 节点，回答的是「这个 Agent 是用什么跑的」**：圆面一个角色图标，
     圆下方两行写名字与 `模型 · Token`（按角色归集），完整参数表在悬停面板里。节点**不是
     执行轨迹的入口**——点了不跳 §5.17 的弹窗：执行台卡片已经承担那个入口，两块视图都能点进
     同一份轨迹就又会混成一个。首尾另有 `任务` / `交付` 两个端子，说明任务从哪进来、结果从哪出去。
     **连线记录上游这一步动过的工具**，画成连线上的一枚胶囊（`calculator ×1`），
     把工具挂在边上而不是节点里，因为用户要看的是「这条数据是怎么被加工出来的」。
     画法照 `Jasper-zh/Multi-Agent-Playground` 的 `GraphViewer.vue`：同一盒子里绝对定位的圆节点
     加同尺寸 SVG 的三次贝塞尔边，**灰虚线 = 要走的边、蓝实线 = 已经走通的边**，进度靠颜色区分
     而不是靠文字说明（ADR-028 决策 7）。节点与连线胶囊的悬停详情（参数全表、Token、分配到的
     任务、阶段产出、工具入参出参）用 CSS `:hover` / `:focus-within` 驱动，不用 JS 状态：
     DOM 常驻，键盘可达，离屏冒烟也能断言内容（ADR-028 决策 5）。
     **这些浮层只在「全屏画布」里给**，侧栏紧凑档一个都不渲染：侧栏要回答的是「这个 Agent
     用什么跑的」，圆下方那行 `模型 · Token` 就是答案，再挂一份带任务与产出的浮层等于把执行
     轨迹搬回侧栏，与本条开头的四块视图分工（ADR-018 + ADR-031）冲突。
     浮层贴节点**侧面**（面宽 380px、与圆留 14px），不挂正下方——挂下方会压住下一段链路，而
     链路正是要给人看的东西；靠右（`x > 0.58w`）的节点自动翻到左侧，竖直方向按面高上限夹在
     可见区内。内容按读图顺序排：**分配到的任务 → 阶段产出 → 本阶段工具调用 → 生效参数 →
     Token 消耗**，参数与 Token 沉到最后（ADR-028 决策 8）。
     **连线上的工具链胶囊（面宽 340px）同样贴侧面**，不挂胶囊正上/正下方：胶囊钉在画布中线上，
     居中弹会正好盖住上游那一串节点（ADR-028 决策 9）。它的面高由两条约束取小——样式表里的
     设计上限 `--cv-pop-max`，与内联算出的可容高度 `--cv-pop-fit`。
     **节点不可拖**：位置本身在表达流程顺序，拖动既不写回数据也不改变后续行为（ADR-028 决策 8）。
     侧栏给**「全屏画布」**入口（`frontend/src/workspace/CollabCanvas.tsx`，`position: fixed`
     覆盖层，Esc 关闭）。画布顶部按 §5.18 列出该会话的每次对话（`对话 1 / 2 / 3`，编号 = 列表
     下标 + 1），点一下切换；选中的不是当前对话时，轨迹与用量按那一份 Workflow 单独取
     （§5.17 + §5.5），**不复用侧栏那一份**——复用会让「对话 1」显示成「对话 2」的数据。
     §5.18 的列表只在画布打开时请求；拉不到就退化成「只有当前这一次对话」，不报错空白。
     底部一行按「正在跑的 → 下一个还没跑的 → 全跑完了」依次退，报当前或下一步是谁。
  4. **任务记录页**——逐条工具调用与采样明细的唯一入口；侧栏与弹窗只给跳转入口。
     原先工作台侧栏内嵌的 `WorkflowInspection`（调用链路 + 任务采样合体）已随 ADR-018 删除，
     记录页继续分别复用 `ToolCallRecords` / `RuntimeSampling`。
- 时间戳统一走 `config/shared.tsx::formatStamp`：今天给「今天 HH:mm」、昨天给「昨天 HH:mm」、
  更早补日期、跨年补年份。历史会话列表与消息气泡此前只显示 `HH:mm`，跨天后无法区分是哪一天；
  各处**不得**再各写一份 `toLocaleTimeString`。
- 弹窗与表单的确认行一律**右对齐**（`.cfg-modal-foot`、`.cfg-actions`）：主操作在右下角，
  取消在左。新增按钮条时沿用这两个类，不要另起一个左对齐的容器。

## 8. 版本与变更规则

- 文档版本：`v0.7`，更新时间：2026-09-15。
- 任何新增或修改路由，先更新本文件的“已实现接口/规划接口”和对象 Schema，再修改代码。
- 若 OpenAPI 与本文档冲突，以实际路由和响应模型为准，并在同一变更中修正文档。

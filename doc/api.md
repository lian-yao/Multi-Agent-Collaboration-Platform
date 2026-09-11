# REST API 契约（开发基线）

> 本文档是当前后端 REST 接口的事实契约。已实现接口以 `app/api/main.py` 的 FastAPI 路由为准；规划接口单独标注“未实现”，在代码落地前不得由前端假定可用。

## 1. 通用约定

- 基础路径：`/api/v1`；健康检查为 `/health`。
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

列表与详情统一读取 API 进程的 `AGENT_*` 配置，返回当前 provider 对应的 model、temperature。`status=idle` 为静态角色状态，不代表模型服务健康；运行状态由 Workflow 展示。API 与 Worker 必须使用相同环境配置，配置修改需要重启对应进程。

## 3. 执行语义

团队任务唯一入口是发送会话消息：

1. 服务端校验 Session 状态。
2. 持久化 Message、AgentRun、WorkflowRun。
3. 调度 Dapr Workflow，返回 `202 Accepted`。
4. 客户端轮询 Workflow，并重新读取消息列表获取最终回复。

当前请求体只有 `content`：

```json
{ "content": "分析一篇技术文章的核心要点" }
```

当前不接受 `decision_agent_id`、Agent 列表、工具列表等字段；前端的主决策 Agent 选择仅为预览交互，不能改变后端编排。

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

### 4.3 查询会话

`GET /api/v1/sessions/{session_id}`

响应 `200` 返回 Session；不存在返回 `404 SESSION_NOT_FOUND`。

### 4.4 发送消息并调度 Workflow

`POST /api/v1/sessions/{session_id}/messages`

响应 `202`：

```json
{
  "message_id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "workflow_id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "status": "pending"
}
```

接口内部在调度前会将持久化 Workflow 和 AgentRun 更新为 `running`；返回体保留 `pending` 作为接收状态。会话暂停时返回 `409 SESSION_PAUSED`。

### 4.5 查询会话消息

完成任务的报告由终态活动追加为 assistant 消息（ADR-008）；失败不追加报告。生产存储按最新消息分页，再在页内按时间升序返回。前端读取第一页最近 100 条，并在终态延迟补刷一次；完整历史加载尚未实现。

`GET /api/v1/sessions/{session_id}/messages?page=1&page_size=20`

响应 `200`：

```json
{ "items": [], "page": 1, "page_size": 20, "total": 0 }
```

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

响应为 `{items: Provider[]}`，当前只返回选中的 Provider。字段：id、name、model、base_url（可空）、status、temperature。数据来自 API 进程 AGENT_* 环境配置；status 为 configured 或 missing_model，不代表模型服务可达。base_url 去除用户信息、query、fragment；不返回密钥，不主动探测模型端点。

### 5.2 查询 Agent 详情

GET /api/v1/agents/{agent_id}

响应字段为 id、name、role、model、provider、temperature、status。未知角色返回 404 AGENT_NOT_FOUND。当前 Agent 配置由运行时默认配置提供。

### 5.3 查询工具目录

GET /api/v1/tools?page=1&page_size=20

item 字段为 name、description、input_schema（JSON 对象）、status。当前 MCP 注册表未落地，返回 availability=not_integrated；API 的工具目录读取适配由 C 提供真实注册表后接入。不会把四种规划工具当成已注册工具。

### 5.4 查询 Workflow 工具调用

GET /api/v1/workflows/{workflow_id}/tool-calls?page=1&page_size=20

item 字段为 id、run_id、workflow_run_id（可空）、tool_name、input（JSON 对象）、output（JSON 或 null）、status（running/succeeded/failed）、error（可空）、created_at、updated_at。读取 doc/data-model.md 的 tool_calls 表，按 created_at、id 升序分页，仅返回 workflow_run_id 匹配记录，或 workflow_run_id 为空且 run_id 匹配该 Workflow 的记录。Workflow 不存在返回 404 WORKFLOW_NOT_FOUND；表不存在返回 not_integrated。API 不创建表、不写审计记录。

### 5.5 查询执行指标

GET /api/v1/metrics?page=1&page_size=20

item 字段为 metric_name、value（有限数值）、labels（JSON 对象）、recorded_at。读取 doc/data-model.md 的 metrics 表，按 recorded_at、id 降序分页。可选 workflow_id 查询参数按 labels.workflow_id 严格过滤，未知 Workflow 返回 404；无参数时展示全局采样记录。表不存在返回 not_integrated。

指标展示保留原始 metric_name、labels、采样时间，不累加分页中可能重复的采样值。Token 名称交接约定为 input_tokens/output_tokens/total_tokens，数值 0 显示为 0，缺少采样显示“暂无采样”；比率和耗时由采集方定义后写入，前端不估算。C 负责采集、去重和 labels.workflow_id（可选 agent_id/model）关联，B 负责建表与审计写入，D 只负责读取和呈现；此约定需 A/B/C 联调验收。

## 6. 规划接口（当前未实现）

下列接口已列入设计方向，但当前 FastAPI 不提供路由，前端不得直接调用：

| 方法 | 路径 | 规划用途 |
| --- | --- | --- |
| POST | `/api/v1/agents/{agent_id}/run` | 单 Agent 调试执行 |
| PATCH | `/api/v1/config/agents/{agent_id}` | 修改 Agent 模型与参数 |

### 6.1 规划请求示例（不保证可用）

```json
PATCH /api/v1/config/agents/{agent_id}
{
  "model": "qwen2.5-coder:7b",
  "temperature": 0.3
}
```

这些接口落地前，必须先补充 Pydantic Schema、存储写入、权限边界、审计记录和测试，并同步更新本文档。

## 7. 前端对接约束

- 初始化顺序：并行调用 `GET /agents` 与 `POST /sessions`。
- 发送消息后保存 `workflow_id`，每 2 秒轮询一次 Workflow；终态为 `completed`、`failed`、`cancelled` 时停止轮询。
- Token 与调用明细由 §5 读取；区分加载、失败、未接入、无记录、有记录，运行时轮询，终态补刷。切换 Workflow 时丢弃旧请求结果；调用和指标独立失败，不能阻断会话功能。主决策 Agent 选择仍为预览，任务标题取用户消息摘要。
- Agent 执行台根据 Workflow 的 `checkpoint.completed_steps` 与 `current_step` 展示阶段状态；不得在无 Workflow 时预填三张 Agent 卡片。

## 8. 版本与变更规则

- 文档版本：`v0.3`，更新时间：2026-09-11。
- 任何新增或修改路由，先更新本文件的“已实现接口/规划接口”和对象 Schema，再修改代码。
- 若 OpenAPI 与本文档冲突，以实际路由和响应模型为准，并在同一变更中修正文档。

# REST API 契约（开发基线）

> 本文档将原“REST API 规划”细化为开发契约。语义决策已定（见第 5 节）；后续变更需先更新本文件，
> FastAPI 落地后以实际 OpenAPI 为准，并同步更新本文件。

## 1. 通用约定

- 基础路径：`/api/v1`
- 请求与响应：`application/json`
- 时间字段：ISO 8601，UTC
- ID 格式：UUID（字符串）
- 分页参数：`page`（从 1 开始，默认 1）、`page_size`（默认 20，最大 100）
- 错误响应统一结构：

```json
{
  "code": "SESSION_NOT_FOUND",
  "message": "会话不存在",
  "request_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

`request_id` 在请求头 `X-Request-ID` 存在时原样返回，否则由服务端生成。

### 错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | 请求参数校验失败 |
| 404 | `SESSION_NOT_FOUND` | 会话不存在 |
| 404 | `AGENT_NOT_FOUND` | Agent 不存在 |
| 404 | `WORKFLOW_NOT_FOUND` | Workflow 不存在 |
| 404 | `TOOL_NOT_FOUND` | 工具不存在 |
| 409 | `SESSION_PAUSED` | 会话已暂停，不能接收新消息 |
| 409 | `WORKFLOW_NOT_PAUSABLE` | Workflow 当前状态不允许暂停/恢复 |
| 500 | `INTERNAL_ERROR` | 服务内部错误 |

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

状态机：

```text
active ⇄ paused
```

`paused` 时会话内不允许提交新消息；已有运行中的 Workflow 可被暂停。

### Message

```json
{
  "id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "role": "user",
  "content": "帮我分析这篇技术文章",
  "status": "completed",
  "created_at": "2026-09-07T08:00:00Z"
}
```

`role`：`user` / `assistant` / `system` / `tool`。

状态机：

```text
queued → running → completed
              └────→ failed
```

### AgentRun

一次 Agent 执行记录，可关联 Workflow：

```json
{
  "id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "running",
  "workflow_id": null,
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:00:00Z"
}
```

状态与 Message 一致，新增 `paused`：

```text
queued → running ⇄ paused → completed
              └─────────→ failed
```

### WorkflowRun

```json
{
  "id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "status": "running",
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:00:00Z"
}
```

状态机：

```text
pending → running ⇄ paused → completed
               └─────────→ failed
               └─────────→ cancelled
```

## 3. 消息执行语义（已定：统一异步）

按企业级长时任务与 Durable Workflow 的常规做法，Agent 执行统一采用异步模型：

- `POST /sessions/{id}/messages` 立即持久化 Message 与 AgentRun、调度 Workflow，返回 `202`；
- 客户端通过 `GET /workflows/{workflow_id}` 轮询执行状态；
- 执行完成后通过 `GET /sessions/{id}/messages` 获取结果：终态活动会追加一条
  `role=assistant` 的报告消息（ID 由 workflow_id 派生，重放不重复），失败任务不写；
- 暂停/恢复接口作用于会话及其运行中的 Workflow。

理由：LLM 推理与工具调用耗时不可控，HTTP 同步连接不适合长任务；统一异步可让多 Agent 流水线、
暂停/恢复和前端实时状态展示共用同一套契约，避免为单 Agent 单独维护同步通道。

Web UI 若需要实时刷新，先采用轮询；事件流（SSE）等实时通道不作为本期 REST 契约范围。

> `app/api/main.py` 已按本契约异步落地：会话、消息、暂停/恢复与 Workflow 状态接口已在 D3–D4 完成，
> 多 Agent 编排、工具与指标端点随 M3/M4 里程碑补充。

## 4. 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建会话 |
| GET | `/sessions/{session_id}` | 查询会话 |
| POST | `/sessions/{session_id}/messages` | 发送消息并执行 Agent |
| GET | `/sessions/{session_id}/messages` | 查询会话消息 |
| POST | `/sessions/{session_id}/pause` | 暂停会话与运行中的 Workflow |
| POST | `/sessions/{session_id}/resume` | 恢复会话与 Workflow |
| GET | `/agents` | 查询 Agent 团队与状态 |
| POST | `/agents/{agent_id}/run` | 运行指定 Agent |
| GET | `/workflows/{workflow_id}` | 查询 Workflow 状态 |
| GET | `/tools` | 查询可用 MCP 工具 |
| PATCH | `/config/agents/{agent_id}` | 调整 Agent 模型参数 |
| GET | `/metrics` | 查询执行指标 |

### 4.1 创建会话

`POST /api/v1/sessions`

请求：

```json
{
  "user_id": "demo-user"
}
```

响应：`201 Created`，返回 Session 对象。

### 4.2 查询会话

`GET /api/v1/sessions/{session_id}`

响应：`200 OK`，返回 Session 对象；不存在返回 `404 SESSION_NOT_FOUND`。

### 4.3 发送消息

`POST /api/v1/sessions/{session_id}/messages`

请求：

```json
{
  "content": "分析一篇技术文章的核心要点"
}
```

响应：`202 Accepted`

```json
{
  "message_id": "c8a1d0a1-1f31-4a2e-9b7e-2f4c9a0b1c2d",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_run_id": "a1b2c3d4-e5f6-4a5b-9c8d-1e2f3a4b5c6d",
  "workflow_id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "status": "pending"
}
```

会话处于 `paused` 时返回 `409 SESSION_PAUSED`。

### 4.4 查询消息

`GET /api/v1/sessions/{session_id}/messages?page=1&page_size=20`

响应：`200 OK`

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0
}
```

### 4.5 暂停会话

`POST /api/v1/sessions/{session_id}/pause`

行为：将会话置为 `paused`；若存在运行中的 Workflow，则一并暂停。

响应：`200 OK`，返回最新 Session 与 WorkflowRun 状态。

无运行中 Workflow 时仅暂停会话；无法暂停时返回 `409 WORKFLOW_NOT_PAUSABLE`。

### 4.6 恢复会话

`POST /api/v1/sessions/{session_id}/resume`

行为：会话恢复为 `active`，暂停的 Workflow 从断点继续执行。

响应：`200 OK`。

### 4.7 查询 Agent 团队

`GET /api/v1/agents`

响应：`200 OK`

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

### 4.8 运行指定 Agent

`POST /api/v1/agents/{agent_id}/run`

请求：

```json
{
  "task": "收集指定主题的资料"
}
```

响应：`202 Accepted`，返回 AgentRun 与 Workflow 标识；Agent 不存在返回 `404 AGENT_NOT_FOUND`。

### 4.9 查询 Workflow

`GET /api/v1/workflows/{workflow_id}`

响应：`200 OK`，返回 WorkflowRun 对象与当前阶段摘要：

```json
{
  "id": "e2f3a4b5-c6d7-4e8f-9a0b-1c2d3e4f5a6b",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "running",
  "current_step": "analysis",
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:01:00Z"
}
```

不存在返回 `404 WORKFLOW_NOT_FOUND`。

### 4.10 查询工具

`GET /api/v1/tools`

响应：`200 OK`

```json
{
  "items": [
    {
      "name": "calculator",
      "description": "数学表达式求值",
      "input_schema": {}
    }
  ]
}
```

### 4.11 修改 Agent 配置

`PATCH /api/v1/config/agents/{agent_id}`

请求：

```json
{
  "temperature": 0.3,
  "model": "qwen2.5-coder:7b"
}
```

响应：`200 OK`，返回更新后的 Agent 配置；Agent 不存在返回 `404 AGENT_NOT_FOUND`。

### 4.12 查询指标

`GET /api/v1/metrics?metric_name=tool_call_success_rate&from=2026-09-07T00:00:00Z&to=2026-09-07T23:59:59Z`

响应：`200 OK`

```json
{
  "items": [
    {
      "metric_name": "tool_call_success_rate",
      "value": 0.96,
      "labels": {},
      "recorded_at": "2026-09-07T08:00:00Z"
    }
  ]
}
```

## 5. 已定决策

1. **消息执行语义**：统一异步（202 + 轮询），见第 3 节。
2. **Session 生命周期**：本期只支持 `active` / `paused`，不引入关闭/归档；
   历史会话始终可查询，避免在两周交付范围内增加未要求的删除/归档接口。
3. **暂停/恢复粒度**：同时作用于会话与其运行中的 Workflow；`paused` 时提交新消息返回
   `409 SESSION_PAUSED`。
4. **任务入口**：会话消息是团队任务的统一入口，提交后由编排层自动规划与分工；
   `/agents/{agent_id}/run` 仅用于单 Agent 直接执行与调试，不作为团队编排入口。

以上决策若有后续变更，先更新本节再同步修改接口与实现。

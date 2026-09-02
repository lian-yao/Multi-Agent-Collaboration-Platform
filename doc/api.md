# REST API 设计（规划）

> 当前为规划文档，接口尚未实现。FastAPI 落地后以实际 OpenAPI 为准，并同步更新本文件。

## 约定

- 基础路径：`/api/v1`
- 请求与响应：JSON
- 时间字段：ISO 8601
- 分页参数：`page`、`page_size`
- 错误响应：

```json
{
  "code": "SESSION_NOT_FOUND",
  "message": "会话不存在",
  "request_id": "uuid"
}
```

## 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建会话 |
| GET | `/sessions/{session_id}` | 查询会话 |
| POST | `/sessions/{session_id}/messages` | 发送消息并执行 Agent |
| GET | `/sessions/{session_id}/messages` | 查询会话消息 |
| POST | `/sessions/{session_id}/pause` | 暂停长时间运行的工作流 |
| POST | `/sessions/{session_id}/resume` | 恢复工作流 |
| GET | `/agents` | 查询 Agent 团队与状态 |
| POST | `/agents/{agent_id}/run` | 运行指定 Agent |
| GET | `/workflows/{workflow_id}` | 查询 Workflow 状态 |
| GET | `/tools` | 查询可用 MCP 工具 |
| PATCH | `/config/agents/{agent_id}` | 调整 Agent 模型参数 |
| GET | `/metrics` | 查询执行指标 |

## 主要对象

- `Session`：用户会话，包含独立上下文与消息历史。
- `AgentRun`：一次 Agent 执行，可关联 Workflow。
- `WorkflowRun`：Dapr Workflow 实例，支持断点续传。
- `ToolCall`：一次工具调用记录。

# 数据模型（规划）

> 当前为规划文档，表和 Key 尚未落地。实际以 SQLAlchemy 模型和 Redis 结构为准，并同步更新本文件。

## PostgreSQL

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `sessions` | 会话持久化 | id、user_id、status、created_at |
| `messages` | 消息历史 | id、session_id、role、content、created_at |
| `agents` | Agent 角色配置 | id、name、role、model、temperature |
| `workflow_runs` | Workflow 执行记录 | id、session_id、status、checkpoint |
| `tool_calls` | 工具调用审计 | id、run_id、tool_name、input、output、status |
| `metrics` | 指标聚合 | id、metric_name、value、labels、recorded_at |

## Redis

| Key | 用途 | 说明 |
| --- | --- | --- |
| `session:{id}:messages` | 会话上下文 | 多轮对话消息 |
| `workflow:{id}:state` | Workflow 状态 | Dapr State Store 后端 |
| `agent:{id}:memory` | Agent 长期记忆 | 跨会话偏好与知识 |
| `pubsub:agent-events` | Agent 间事件 | Dapr Pub/Sub 主题 |

## 一致性

- Redis 保存高频状态与临时上下文。
- PostgreSQL 保存会话、审计和持久化记录。
- Dapr Workflow 在状态变更持久化后才推进下一步。

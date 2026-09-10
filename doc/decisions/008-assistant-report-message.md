# ADR-008: 终态回写报告为 assistant 消息

状态：已接受

## 背景

`doc/api.md` §3 已定契约：`POST /sessions/{id}/messages` 立即返回 `202`，
客户端轮询 `GET /workflows/{workflow_id}` 看进度，**执行完成后通过
`GET /sessions/{id}/messages` 获取结果**。

但 D3-D4 至 D5-D6 的实现只做了前半段：`finalize_activity` 回写
`workflow_runs` / `agent_runs` / `messages` 的**状态字段**，阶段正文只留在
Dapr State Store（`agentrun:workflow:{workflow_id}:{step}`）与 Workflow 活动输出里。
结果是 `messages` 表只有用户自己发的那条 `role=user` 消息，Web UI 的「消息记录」
看不到报告——契约与实现漂移。ADR-007 已把这条列为后续增量。

## 决策

1. **终态写入报告**：`finalize_activity` 在 `status=completed` 且携带报告正文时，
   写入一条 `messages(role=assistant, status=completed)`，`content` 取
   `report` 阶段的活动输出，`agent_run_id` 沿用本次执行，`session_id` 取 Workflow
   载荷中的会话。
2. **幂等**：消息 ID 由 `workflow_id` 派生（`report_message_id`，uuid5），入库使用
   PostgreSQL `INSERT ... ON CONFLICT (id) DO NOTHING`，活动被重放时不会产生重复
   报告；重复调用返回已存在记录。
3. **失败不写报告**：`status=failed` 只回写状态与 `workflow_runs.error`，不写
   assistant 消息，避免把半成品当结果。
4. **不引入推送通道**：前端继续用既有轮询（`doc/api.md` §3 已说明事件流不在本期
   契约范围），本决策只补齐落库。

## 影响

- `GET /sessions/{id}/messages` 在任务完成后返回两条：用户的 `user` 消息与
  Agent 的 `assistant` 报告，前端「消息记录」无需改造即可显示（`.log-marker.assistant`
  样式已存在）；本轮同时把前端角色标签从 `assistant` 改为 `Agent`。
- 报告随 `messages` 一起成为会话事实源的一部分，`messages.content` 为 `TEXT`，
  不额外截断；Dapr State Store 中的阶段快照保持不变，仍用于恢复与审计。
- 同一次 Workflow 即使终态活动被重放，也只会有一条报告消息。

# Dapr × LangGraph 集成设计（开发基线）

> 状态：D3–D4 已按本设计落地（2026-09-09 补充 durable 终态回写修订），后续增量不重开架构决策。
> 本文件解决“LangGraph 图编排”与“Dapr Agents / Dapr Workflow 持久化执行”如何共存的问题，
> 是 Dapr 持久化集成的实现基线。

## 1. 目标与范围

依据设计文档与 ADR-004：

- 编排框架固定为 LangGraph：图结构、节点、状态与多 Agent 分工由 LangGraph 定义。
- Dapr Agents 1.0.6 已引入：承担 Agent 生命周期、持久化执行、记忆与工具生态运行时能力。
- 优先集成持久化执行与状态管理，本期不深入 Service Invocation 与 Actor 模型。

本文档范围：

1. 定义 LangGraph 与 Dapr Agents/Workflow 的职责边界；
2. 定义执行、状态与恢复的主链路；
3. 定义验收故障场景与后续测试要求；
4. 记录已定设计决策与备选评估。

## 2. 职责边界

| 层 | 归属 | 负责内容 | 不负责 |
| --- | --- | --- | --- |
| 编排层 | LangGraph | Agent 图拓扑、节点执行顺序、消息状态、角色分工 | 进程级恢复与跨重启调度 |
| 运行时层 | Dapr Agents / Dapr Workflow | Workflow 实例调度、活动持久化与重放、断点恢复、Pub/Sub 事件 | Agent 编排策略本身 |
| 存储层 | Redis / PostgreSQL / Dapr State Store | 会话与记忆、审计记录、Workflow 状态 | 业务规则 |

关键结论（已定）：

- **一次用户级执行 = 一个 AgentRun = 至多一个 Dapr Workflow 实例。**
- LangGraph 保持“图定义”与“状态 Schema”的事实源；Dapr 侧不重新定义编排规则。
- 跨进程恢复以 Dapr Workflow 的实例状态为准；应用侧 `workflow_runs.checkpoint` 保存面向展示与
  审计的最近快照，不作为恢复唯一依据。

## 3. 执行模式（已定：节点级活动）

按企业级 Durable Workflow 实践（活动应短、可重试、幂等、可独立持久化），执行模式定为
**模式 B 的简化版**：

- 固定三步流水线（收集 → 分析 → 报告）映射为三个顺序 Workflow 活动；
- LLM 调用与工具调用分别作为独立活动，便于超时、重试与审计；
- 已完成活动结果由 Dapr 持久化，进程崩溃后仅重放不重跑；
- LangGraph 仍负责图拓扑与状态 Schema，Workflow 步骤序列由图生成。

第 3 天上午的 POC 用于验证序列化、幂等与恢复语义的实现细节；模式本身不再重开评审。

> 修订（2026-09-10）：阶段活动内的 LLM 调用已由 Fake 结果切换为真实模型
> （`run_role_stage` → Ollama），活动边界、载荷结构与恢复语义不变，见 ADR-007。

> 备选评估：模式 A（整图作为一个持久化执行单元）实现改动小，但单个活动可能长时间运行，
> 恢复粒度与可观测性不足，未采用。

## 4. 状态设计

### 4.1 谁持有什么状态

| 状态 | 持有方 | 用途 |
| --- | --- | --- |
| 会话/消息历史 | PostgreSQL + Redis | 多轮对话、页面展示 |
| AgentRun 执行记录 | PostgreSQL | 业务审计、API 状态 |
| Dapr Workflow 实例状态 | Dapr State Store（statestore） | 跨进程恢复、活动重放 |
| LangGraph 进度摘要 | PostgreSQL `workflow_runs.checkpoint` | 仅存展示/审计用摘要，不作为恢复依据 |
| 长期记忆 | Redis / 记忆存储 | 跨会话偏好与知识 |

### 4.2 状态写入顺序

```text
POST message
  → 写入 messages(queued) + agent_runs(queued) + workflow_runs(pending)
  → workflow_runs/agent_runs/messages 置为 running
  → 创建/调度 Dapr Workflow 实例（调度失败则三者回写 failed）
  → 每完成一个阶段：
      → Dapr Workflow 持久化该阶段结果
      → 回写 workflow_runs.status/current_step/checkpoint（仍为 running）
  → 终态回写活动执行：workflow_runs/completed|failed
    + agent_runs/completed|failed + message/completed|failed
```

失败与暂停同理：先由 Dapr 侧持久化事实，再回写业务表；两者不一致时以 Dapr 状态为准并告警。

> 修订（2026-09-09）：业务行先置 `running` 再调度，避免“终态回写活动先于 API 的
> running 回写完成”导致状态机竞态；终态回写作为 Workflow 内的持久化活动
> （`finalize_activity`）执行，进程重启后仍能补齐 `completed` / `failed`。

## 5. 故障恢复主链路

验收场景：多 Agent 流水线执行到“分析”阶段时，杀掉 backend 进程，重启后任务续跑并成功完成。

恢复顺序：

1. 请求先落 `messages` 与 `agent_runs`，保证提交不丢；
2. Workflow 每完成一个活动即被 Dapr 持久化，进程崩溃后实例仍在；
3. 重启后 backend 注册同一批 Workflow/活动；
4. Dapr Scheduler 将未完成实例恢复到最后已完成活动；
5. 后续活动继续执行；最后回写业务状态为 completed。

实现前必须验证：

- 活动结果缓存有效，崩溃后不会重复执行副作用操作；
- LLM 调用与工具调用都有幂等键或审计记录，允许重放但禁止重复外部副作用；
- 恢复耗时目标 < 5 秒（从服务可用到实例继续执行）。

## 6. 并发与投递语义

- Workflow 活动重放按 Dapr 语义进行，应用层不假设“活动只执行一次”。
- 工具调用写 `tool_calls(status=running)` 后执行；重放前先查同 ID 结果，重复投递时返回缓存结果。
- Pub/Sub 提供至少一次投递；消费端按 `instance_id + 事件 ID` 去重。
- 暂停只作用于当前 Workflow 实例与所属会话，新消息在 `paused` 状态下返回 409。

## 7. 模块落地清单

| 模块 | 落地内容 | 依赖 |
| --- | --- | --- |
| `app/orchestration` | 暴露图定义、状态 Schema、节点/步骤序列化接口 | 无 |
| `app/workflows` | Workflow 注册、调度、暂停/恢复/查询封装 | Dapr Agents 运行时 |
| `app/core` | Dapr app-id、组件名、State Store 配置 | 无 |
| `app/api` | 按 `doc/api.md` 契约封装 | `app/workflows` |
| `app/memory` | 会话/长期记忆读写 | 存储层 |
| `app/observability` | Span 命名、指标输出 | Dapr Agents 遥测 |

## 8. 开发顺序（D3–D4）

> 增量修订（2026-09-07）：D3–D4 仍按“固定三步 + 节点级活动”的已有决策落地，
> 作为后续动态编排的可持久化基础。
> 动态意图拆分、Send 并行波次、依赖感知调度、人工介入（HITL）与失败 3 次返工
> **不在本增量范围**；如需引入，先补 ADR 并修订 `doc/15 AI Native多智能体协作平台.md` 再排期。

1. D3 上午：模式 B 最小 POC（固定三步流水线 + Fake 模型），验证序列化、幂等与恢复；
2. D3 下午：确认状态写入顺序与活动边界，更新本文档；
3. D4 上午：Workflow 调度 + 状态查询 + 暂停/恢复接口；
4. D4 下午：故障恢复演练脚本与自动化/半自动化测试（见 `doc/testing.md`）。
5. 收尾：固化可扩展点——每个步骤保持独立活动、活动载荷保持结构化可序列化，
   后续动态 Fan-out 只需增加“任务计划/波次调度”，不改持久化与恢复边界。

## 9. 决策结论

1. **执行模式**：模式 B 简化版（三步流水线 = 三个顺序活动），LangGraph 负责图与状态 Schema。
2. **Checkpoint 语义**：`workflow_runs.checkpoint` 只存展示/审计用进度摘要；
   完整执行状态以 Dapr State Store 为恢复事实源，应用侧不复制全量状态。
3. **暂停/恢复粒度**：同时作用于会话与其运行中的 Workflow；`paused` 时新消息返回 409
   （与 `doc/api.md` 第 5 节一致）。
4. **恢复演练**：D4 前提供可复现手工脚本，D7 前固化为自动化集成测试（见 `doc/testing.md`）。
5. **范围边界**：D3–D4 实现固定三步模式 B；动态并行分派、依赖 DAG、HITL 与 3 次返工
   按第 8 节说明暂不引入。

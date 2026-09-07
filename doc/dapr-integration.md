# Dapr × LangGraph 集成设计（开发基线草案）

> 状态：草案，需四人评审。
> 本文件解决“LangGraph 图编排”与“Dapr Agents / Dapr Workflow 持久化执行”如何共存的问题，
> 是第 3-4 天开发（Dapr 持久化集成）的前置设计。实现细节在最小 POC 验证后固化并更新本文件。

## 1. 目标与范围

依据设计文档与 ADR-004：

- 编排框架固定为 LangGraph：图结构、节点、状态与多 Agent 分工由 LangGraph 定义。
- Dapr Agents 1.0.6 已引入：承担 Agent 生命周期、持久化执行、记忆与工具生态运行时能力。
- 优先集成持久化执行与状态管理，本期不深入 Service Invocation 与 Actor 模型。

本文档范围：

1. 定义 LangGraph 与 Dapr Agents/Workflow 的职责边界；
2. 定义执行、状态与恢复的主链路；
3. 定义验收故障场景与后续测试要求；
4. 列出需评审确认的设计决策。

## 2. 职责边界

| 层 | 归属 | 负责内容 | 不负责 |
| --- | --- | --- | --- |
| 编排层 | LangGraph | Agent 图拓扑、节点执行顺序、消息状态、角色分工 | 进程级恢复与跨重启调度 |
| 运行时层 | Dapr Agents / Dapr Workflow | Workflow 实例调度、活动持久化与重放、断点恢复、Pub/Sub 事件 | Agent 编排策略本身 |
| 存储层 | Redis / PostgreSQL / Dapr State Store | 会话与记忆、审计记录、Workflow 状态 | 业务规则 |

关键结论（拟议）：

- **一次用户级执行 = 一个 AgentRun = 至多一个 Dapr Workflow 实例。**
- LangGraph 保持“图定义”与“状态 Schema”的事实源；Dapr 侧不重新定义编排规则。
- 跨进程恢复以 Dapr Workflow 的实例状态为准；应用侧 `workflow_runs.checkpoint` 保存面向展示与
  审计的最近快照，不作为恢复唯一依据。

## 3. 候选执行模式（需 POC 验证后固化）

两种候选模式都会先做最小 POC（第 3 天上午完成），结论写入本文档后开始完整实现：

### 模式 A：整图作为一个持久化执行单元

- LangGraph 编译后的图为整体能力边界；
- Dapr Workflow 负责调度该执行的开始、暂停、恢复与完成；
- 图内部若发生中断，进程重启后由 Dapr Workflow 重放已完成活动，并从断点继续；
- 优点：LangGraph 内部实现自由度高、改动小；
- 风险：若单次图执行本身超过活动时长/内存边界，或图内部有长时工具等待，恢复粒度不够细。

### 模式 B：节点级活动（细粒度）

- 按 LangGraph 图拓扑生成 Dapr Workflow 的确定性步骤序列；
- 每个图节点对应一个 Workflow 活动（LLM 调用、工具调用分别成活动）；
- 已完成活动结果由 Dapr 持久化，崩溃后仅重放不重跑；
- 优点：恢复粒度细、可观测性好、天然支持暂停/恢复；
- 风险：LangGraph 动态条件边与图结构需要确定性序列化，实现成本更高。

推荐先用模式 B 的简化版做 POC：**固定三步流水线（收集 → 分析 → 报告）为三个顺序活动**，
验证恢复语义后再扩展动态边。

## 4. 状态设计

### 4.1 谁持有什么状态

| 状态 | 持有方 | 用途 |
| --- | --- | --- |
| 会话/消息历史 | PostgreSQL + Redis | 多轮对话、页面展示 |
| AgentRun 执行记录 | PostgreSQL | 业务审计、API 状态 |
| Dapr Workflow 实例状态 | Dapr State Store（statestore） | 跨进程恢复、活动重放 |
| LangGraph 最近快照 | PostgreSQL `workflow_runs.checkpoint` | 展示当前进度与断点摘要 |
| 长期记忆 | Redis / 记忆存储 | 跨会话偏好与知识 |

### 4.2 状态写入顺序

```text
POST message
  → 事务写入 messages(queued) + agent_runs(queued)
  → 创建/调度 Dapr Workflow 实例
  → agent_runs/status = running
  → 每完成一个阶段：
      → Dapr Workflow 持久化该阶段结果
      → 回写 workflow_runs.status/current_step/checkpoint
  → 完成：workflow_runs/completed + agent_runs/completed + message/completed
```

失败与暂停同理：先由 Dapr 侧持久化事实，再回写业务表；两者不一致时以 Dapr 状态为准并告警。

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

1. D3 上午：模式 A/B 最小 POC（固定三步流水线 + Fake 模型）；
2. D3 下午：固化执行模式与状态写入顺序，回写本文档；
3. D4 上午：Workflow 调度 + 状态查询 + 暂停/恢复接口；
4. D4 下午：故障恢复演练脚本与自动化/半自动化测试（见 `doc/testing.md`）。

## 9. 待评审决策

1. 执行模式选择模式 B（节点级活动）还是模式 A（整图执行）？
2. `workflow_runs.checkpoint` 是 LangGraph 原始状态 JSON 还是展示用摘要？（推荐摘要 + 完整 JSON 分列存储）
3. 暂停/恢复是否限定在 Workflow 粒度，还是同时影响会话？（推荐同时影响会话，语义一致）
4. 恢复演练是手工脚本（先）还是集成测试（后）？本文件要求 D4 前至少手工脚本可复现。

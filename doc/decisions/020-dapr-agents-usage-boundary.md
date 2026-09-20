# ADR-020: Dapr Agents 1.0.6 的使用边界（依赖与运行时约束 vs 高层抽象）

状态：已接受（记录现状；改变边界需新 ADR）

## 背景

分工表与设计文档把 Dapr Agents 1.0.6 列为**核心框架**之一，ADR-004 据此引入依赖并锁定
OpenTelemetry 1.39.1，同时规定「Workflow 与状态读写能力应复用其运行时」，并要求
「先做最小可运行 POC 再固化实现细节」。

实际落地的持久化执行链路用的是 Dapr Python SDK 的 `dapr.ext.workflow`
（`app/workflows/pipeline.py`：父 Workflow + 阶段子 Workflow + 活动，
`app/workflows/worker.py` 注册运行时）与 `dapr.clients`（`app/workflows/state.py`）。
代码里没有任何 `dapr_agents` 调用点，因此需要把「引用了依赖」与「使用了它的 API」
两件事写清楚——否则评审会问「核心框架在哪里用」。

## 现状（2026-09-20 核对）

| 项 | 事实 |
| --- | --- |
| 依赖 | `dapr-agents==1.0.6` 在 `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 三处同步 |
| 版本约束 | 其语义约定要求 OpenTelemetry 1.39.1（`opentelemetry-sdk` 与 OTLP exporter 固定该版本，ADR-004） |
| 生产链路实际调用 | `dapr.ext.workflow`（Workflow/活动/子 Workflow/重放）、`dapr.clients`（State Store 读写） |
| `dapr_agents` 调用点 | **0 处**（`rg dapr_agents app tests` 无命中） |
| 运行期证据 | compose 全栈跑通：三步流水线 `completed`、断点续跑、工具审计、指标与追踪（见 `doc/roadmap.md` 验证记录） |

## 决策

1. **保留依赖与版本约束**：`dapr-agents==1.0.6` 继续作为直接依赖钉住 OpenTelemetry
   兼容组合，升级 OTel 必须先按 ADR-004 验证。
2. **持久化执行以 Dapr Workflow 为运行时事实来源**：`dapr.ext.workflow` 即 Dapr 官方
   耐久化执行运行时；`dapr-agents` 的 `DurableAgent` 正是这层之上的高层封装，
   同一进程里再叠一层不会带来新能力。
3. **不引入 `dapr_agents` 高层抽象**，原因逐项对应既有决策：

| `dapr_agents` 抽象 | 与之重叠的既有决策 | 结论 |
| --- | --- | --- |
| `DurableAgent` / `AgentRunner`（pub/sub 驱动的 Agent 服务） | 编排固定为 LangGraph + 固定三步工作流（ADR-001、ADR-007、`doc/dapr-integration.md` §3） | 不采用：会与生产链路形成两套编排 |
| `DaprChatClient`（Dapr Conversation API） | 模型接入 API 优先、Provider 配置由系统保存（ADR-014） | 不采用：需新增 Conversation 组件与凭据通道 |
| `dapr_agents.memory`（Dapr State Store 记忆） | 会话/长期记忆 Redis 契约（ADR-005、ADR-019） | 不采用：会让记忆事实源分裂 |
| `AgentTool` | MCP 工具注册与调用（ADR-009、ADR-012） | 不采用：工具层已按 MCP 标准化 |

4. **未完成项（待定，需方向决策）**：ADR-004 要求的「最小可运行 POC」**尚未做**。
   已核实的可行性边界：`DurableAgent(...)` 构造即连接 Dapr sidecar（gRPC 50001），
   没有 sidecar 会立刻 `UNAVAILABLE`，因此 POC 需要一次独立的 `dapr run`
   （CLI 1.18.2 可用）+ 组件目录，且不与 compose 已占端口冲突。
   做与不做、以及是否要在报告里给出「Dapr Agents 端到端跑通」的演示证据，
   属方向与验收范围问题，由人类决定；若决定做，另开任务与 ADR 修订本节。

## 影响

- 报告口径可以明确写成「Dapr Agents 1.0.6 已引入并作为运行时兼容约束；持久化执行由
  Dapr Workflow 承载」，不会与代码事实冲突。
- 若后续要用 `dapr_agents` 高层能力（例如 HITL 审批、Agent 作为独立服务暴露），
  必须新开 ADR 并修订 `doc/dapr-integration.md` §7 的模块依赖列。
- `doc/15 ...平台.md` 的「核心框架」表述与建议方案措辞如需调整，属事实源修改，
  需人类同意后再改（本次未动）。

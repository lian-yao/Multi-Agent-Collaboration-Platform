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

4. **POC（2026-09-20 已完成）**：ADR-004 要求的「最小可运行 POC」落地为
   `scripts/poc_dapr_agents.py`，在本地 Dapr 运行时上跑通一个 `DurableAgent` 工作流。

### POC 结果（2026-09-20）

```powershell
# 跑一次（宿主机根目录；组件用 dapr init 默认目录，不要求 compose 在跑）
dapr run --app-id macp-agents-poc --dapr-http-port 3510 --dapr-grpc-port 50001 `
    -- uv run python scripts/poc_dapr_agents.py
# 再起一个进程读回同一个实例（验证状态跨进程持久）
dapr run --app-id macp-agents-poc --dapr-http-port 3510 --dapr-grpc-port 50001 `
    -- uv run python scripts/poc_dapr_agents.py --inspect <instance_id>
```

| 项 | 实测结果 |
| --- | --- |
| Agent | `MacpPocAgent`（`EchoAgentExecutor`，不需要模型凭据） |
| 注册 | `Registered workflows/activities on WorkflowRuntime for agent 'MacpPocAgent'` |
| 工作流 | `dapr.agents.MacpPocAgent.workflow`，实例 `8d74349217fc44998aba0f4c1e9a6e9e` |
| 终态 | `WorkflowStatus.COMPLETED`（Dapr 侧 `ORCHESTRATION_STATUS_COMPLETED`），脚本退出码 0 |
| 输出 | `{"role": "assistant", "content": "echo: POC：用 echo 执行器回显这条任务，证明 Dapr Agents 工作流跑通。"}` |
| 持久化 | 第二个进程 `--inspect` 读回同一实例：`COMPLETED` + 同一输出；状态存储里可见 `macp-agents-poc\|\|dapr.internal.default.macp-agents-poc.workflow\|\|<instance>\|\|history-0000NN` 与 `metadata` 键 |
| 前提 | 需要 sidecar：`DurableAgent(...)` 构造即连 gRPC 50001，无 sidecar 直接 `UNAVAILABLE` |

复跑（同一脚本、新实例 `fdcf795411b149a0a65028d1fef0a028`）同样 `COMPLETED`，
`--inspect` 同样读回完整输出——结论可重复，不是一次性偶然结果。

**POC 中发现的上游不一致（dapr-agents 1.0.6，值得反馈）**：活动注册用的是 agent 前缀名
`dapr.agents.<agent>.<method>`，标准 LLM 分支也按前缀名调用
（`ctx.call_activity(self._activity_name(self.call_llm), ...)`），但 **executor 分支**
（`agents/durable.py` 第 675-679 行）传的是裸绑定方法 `self.run_executor`，运行时据此查找
未加前缀的 `run_executor`，报 `Activity function named 'run_executor' was not registered`、
工作流终态 FAILED。POC 用「显式注册一个未加前缀的活动别名」绕过
（`runtime.register_activity(agent.run_executor)`），上游修复后该行可删。

**结论**：`dapr-agents` 1.0.6 的 Durable Workflow 链路在本地 Dapr 运行时上可用，
但走 executor 形态需要补一个注册别名；生产链路继续用 `dapr.ext.workflow` + 固定三步编排，
不引入该高层封装（理由见上文决策 2/3）。

## 影响

- 报告口径可以明确写成「Dapr Agents 1.0.6 已引入并作为运行时兼容约束；持久化执行由
  Dapr Workflow 承载」，不会与代码事实冲突。
- 若后续要用 `dapr_agents` 高层能力（例如 HITL 审批、Agent 作为独立服务暴露），
  必须新开 ADR 并修订 `doc/dapr-integration.md` §7 的模块依赖列。
- `doc/15 ...平台.md` 的建议方案措辞已按人类确认同步（2026-09-20）：核心框架一栏补
  「依赖与 OpenTelemetry 版本约束；持久化执行由 Dapr Workflows 承载，使用边界见 ADR-020」，
  架构图里 `Dapr Agents / Agent生命周期管理` 节点改为
  `Dapr Workflow 运行时（dapr.ext.workflow）`，模块2 末尾补落地口径与 POC 指针。

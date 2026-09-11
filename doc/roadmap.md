# 开发路线图

按设计文档整理，10 个工作日。

| 阶段 | 天数 | 任务 | 输出物 |
| --- | --- | --- | --- |
| 第 1-2 天 | 2 | 环境搭建（Dapr CLI 初始化 + Python 虚拟环境）；LangGraph 快速原型；对接 Ollama 本地模型 | LangGraph 基本 Agent 对话能力完成 |
| 第 3-4 天 | 2 | 集成 Dapr Workflow 与 State Management；会话记忆持久化和 Workflow 状态快照 | Dapr 持久化集成完成 |
| 第 5-6 天 | 2 | 实现多 Agent 编排（LangGraph 多节点图）；示例场景：信息收集、处理、报告 | 多 Agent 协作能力完成 |
| 第 7-8 天 | 2 | 实现 MCP 工具注册与调用；至少 4 个示例工具；集成可观测性 | 工具生态与可观测性完成 |
| 第 9-10 天 | 2 | 开发 Web UI；端到端集成测试；Docker + Dapr Compose 部署；撰写项目报告 | 完整交付与部署演示 |

## 当前进度

- 已完成（D1-D2）：Dapr CLI 初始化、Python 虚拟环境、LangGraph 单 Agent 原型与测试、Ollama 模型验证。
- 已完成（D3-D4，里程碑 M2）：固定三步 Dapr Workflow + State Management、断点续跑演练脚本、
  编排状态契约与 Checkpoint 摘要、会话/记忆数据结构、会话与 Workflow REST API；
  API 发起的执行完成后由 durable 终态活动回写 completed/failed。
- 已完成（D5-D6 接线）：多 Agent 流水线的 LangGraph 图与角色分配、角色 Prompt 与示例场景、
  可恢复的多 Agent 子任务 Workflow、Workflow 阶段活动默认调用真实 Ollama 模型
  （Fake 仅保留为恢复演练开关，见 ADR-007）。
- 已完成（D5-D6 收尾）：最终报告作为 `messages(role=assistant)` 在终态回写并经
  `GET /messages` 返回，Web 消息记录直接可见（见 ADR-008）。
- 已完成（D7-D8 编排层，成员 A）：流水线接入 MCP 工具——编排层冻结工具契约
  （`ToolSpec` / `ToolCall` / `ToolCallRecord` / `ToolRegistry`）与模型驱动的 ReAct 调用循环，
  阶段载荷回传 `tool_calls`；`app/mcp` 注册表落地后无需改动即可接入（见 ADR-009）。
- 已完成（D7-D8 可观测增量）：编排层输出结构化行为日志
  （`event=stage.start|finish|failed`、`tool.call`），按 `workflow_id` 关联，
  级别由 `LOG_LEVEL` 控制（见 ADR-010）。
- 下一步：M4 其余部分——成员 C 的内置工具/沙箱/可观测接入、成员 B 的 `tool_calls`
  审计落库、成员 D 的调用链路与 Token 统计，以及 Jaeger/Prometheus 指标。
- 范围说明：D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

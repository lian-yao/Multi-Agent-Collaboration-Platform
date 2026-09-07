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

- 已完成：Dapr CLI 初始化、Python 虚拟环境、LangGraph 单 Agent 原型与测试。
- 下一步：集成 Dapr Workflow 与 State Management。
- 进行中（D3-D4）：成员 A 的编排状态 Schema、序列化与 Checkpoint 摘要接口已完成；
  成员 B 按固定三步模式 B 接入 Dapr Workflow 与 State Management。
- 范围说明：D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

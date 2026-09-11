# 测试策略与测试计划

> 本文件将原“测试策略”细化为开发前可执行的测试计划。每个里程碑验收时必须能给出本文件
> 对应用例的运行结果。

## 1. 测试层级与运行环境

| 层级 | 范围 | 目录 | 运行前提 |
| --- | --- | --- | --- |
| 单元测试 | 图节点、状态、模型工厂、工具函数、Schema | `tests/unit` | 无需外部服务 |
| 集成测试 | Dapr、Redis、PostgreSQL、MCP、API | `tests/integration` | 本地 Dapr + 服务 |
| 端到端测试 | REST API + Web UI + Dapr + 部署 | `tests/e2e` | 完整 compose 环境 |

常用命令：

```bash
uv run pytest -m "not integration"        # 单元
uv run pytest                              # 全部可用用例
cd deploy; .\start.ps1                     # 起完整环境
```

测试替身：

- LLM 使用 `FakeChatModel`（已有实现），按用例注入固定回复；
- MCP 工具使用内存版注册表或本地 Fake MCP Server；
- 不依赖真实模型的用例禁止请求 Ollama/OpenAI。

## 2. 用例矩阵

### 2.1 单元测试（U）

| 编号 | 用例 | 断言要点 | 对应里程碑 |
| --- | --- | --- | --- |
| U-01 | 单 Agent 图返回回复 | 末条消息为 AI 消息 | M1 |
| U-02 | 多轮状态累积 | 历史消息不丢失、顺序正确 | M1 |
| U-03 | 模型工厂按配置创建 | Ollama/OpenAI 分支正确、非法 provider 报错 | M1 |
| U-04 | 会话/AgentRun/Workflow 状态迁移 | 非法迁移被拒绝 | M2/M3 |
| U-05 | 图状态 Schema 序列化 | LangGraph 状态可无损转 JSON 并还原 | M3 |
| U-06 | 工具函数独立行为 | 计算器/SQL 只读校验等返回值正确 | M4 |
| U-07 | 工具调用幂等键 | 同 ID 重复投递返回缓存结果 | M4 |
| U-08 | 沙箱边界拒绝越权 | 代码执行工具拒绝网络/危险命令 | M4 |
| U-09 | 流水线接入 MCP 工具 | 注册表工具被发现，按模型 `tool_calls` 调用并回填观察；失败记为 failed 且不中断；阶段载荷可序列化 | M4 |
| U-10 | 行为日志事件 | 阶段开始/结束/失败与工具调用输出 `event=... field=value` 结构化日志，含 workflow_id 与耗时 | M4 |

### 2.2 集成测试（I）

| 编号 | 用例 | 断言要点 | 里程碑 |
| --- | --- | --- | --- |
| I-01 | Workflow 调度与状态查询 | 调度后状态 running，完成后 completed | M2 |
| I-02 | 会话消息写入 PostgreSQL + Redis | 双写一致、Redis 可重建 | M2 |
| I-03 | Workflow 断点持久化 | 阶段结果可在 State Store 查到 | M2 |
| I-04 | 暂停/恢复 API | paused 时新消息 409，resume 后续跑 | M3 |
| I-05 | 多 Agent 三步流水线 | collector → analyst → reporter 顺序完成 | M3 |
| I-06 | MCP 工具发现与调用 | 工具可发现、可调用、审计落库 | M4 |
| I-07 | 可观测数据输出 | 关键 Span 与指标可在 Jaeger/Prometheus 查到 | M4 |
| I-08 | 配置热更新 | PATCH agent 后新执行使用新配置 | M4 |

### 2.3 端到端测试（E）

| 编号 | 用例 | 步骤 | 通过标准 | 里程碑 |
| --- | --- | --- | --- | --- |
| E-01 | 单 Agent 问答 | 创建会话 → 发消息 → 轮询/取结果 | 返回结构化回答 | M5 |
| E-02 | 多 Agent 协作 | 提交三步任务 → 观察状态流转 | 三步依次完成并生成报告 | M5 |
| E-03 | 故障恢复 | 执行中断掉 backend → 重启 | 任务从断点续跑成功，无状态丢失 | M3 起可演练，M5 验收 |
| E-04 | Web 会话管理 | UI 创建会话、发消息、暂停/恢复 | UI 与 API 状态一致 | M5 |
| E-05 | 一键部署 | `start.ps1` → 健康检查 → `stop.ps1` | 全部服务健康 | M5 |

## 3. 关键链路测试设计

### 3.1 Workflow 恢复测试（I-03 / E-03）

最小可复现方案：

1. 启动流水线到固定阶段（如执行 collector 后）；
2. 记录 Dapr Workflow `instance_id`；
3. 杀掉 backend 进程/容器；
4. 重启 backend；
5. 轮询 `GET /workflows/{id}`，断言从 `analysis` 阶段继续并最终 `completed`；
6. 记录从服务可用到实例恢复执行的耗时，目标 `< 5s`。

自动化路线：已提供 `scripts/fault_recovery.ps1` 固化手工演练步骤；D7 前改为 pytest 集成用例。

### 3.2 并发会话测试（E 系列性能）

- 使用 `locust` 或 `hey` 模拟 10 个并发会话；
- 断言：全部会话完成、无 5xx 比例超过阈值、平均延迟记录在报告；
- 指标从 Prometheus 导出 Token 消耗与工具调用成功率。

## 4. 里程碑验收清单

| 里程碑 | 必须通过的用例 |
| --- | --- |
| M1（单 Agent） | U-01、U-02、U-03 |
| M2（Dapr 持久化） | I-01、I-02、I-03 |
| M3（多 Agent） | U-04、U-05、I-04、I-05、E-03 手工版 |
| M4（工具 + 可观测） | U-06、U-07、U-08、U-09、U-10、I-06、I-07、I-08 |
| M5（交付） | E-01 至 E-05 全部 |

## 5. 失败处理约定

- 任一用例失败：先复现，再定位，修复后将失败模式固化为新的测试或本文档约束；
- 对 Dapr/编排等共享行为，先写测试或同步补测试，不允许“看起来正确”代替；
- 每次里程碑结束时在报告记录：运行命令、通过数/失败数、失败原因。

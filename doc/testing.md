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
| U-07 | 工具调用幂等键 | 同 ID 重复投递返回缓存结果；失败可同 ID 重试；`running` 拒绝并发重放；装饰器记录 running→终态（ADR-011） | M4 |
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
| I-09 | D7-D8 只读巡检接口 | Provider/Agent/工具/调用/指标接口的分页、`availability` 区分「未接入」与「零条记录」、404/422/503 契约 | M4 |

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

自动化路线：已提供 `scripts/fault_recovery.ps1` 固化手工演练步骤。

现状（2026-09-11）：仍为手工脚本；`tests/unit/test_workflow_pipeline.py` 覆盖了活动重放、
子 Workflow 实例 ID 稳定、终态回写等**单元级**的恢复语义，真实 Dapr 进程被杀→重启的
pytest 集成用例尚未落地，E-03 仍按 M5 手工验收。

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
| M4（工具 + 可观测） | U-06、U-07、U-08、U-09、U-10、I-06、I-07、I-08、I-09 |
| M5（交付） | E-01 至 E-05 全部 |

### 4.1 M4 当前状态（2026-09-11）

合并 A/B/D 三份 D7-D8 提交后的实测状态。**M4 未完成**，缺口集中在成员 C 的
内置工具/沙箱/可观测接入。

| 用例 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| U-06 工具函数独立行为 | 未实现 | 尚无内置工具（`app/tools` 为空），待 C |
| U-07 工具调用幂等键 | 通过 | `tests/unit/test_tool_audit.py`（ADR-011） |
| U-08 沙箱边界拒绝越权 | 未实现 | 尚无沙箱（`app/sandbox` 为空），待 C |
| U-09 流水线接入 MCP 工具 | 通过 | `tests/unit/test_pipeline_tools.py`，含「阶段活动消费默认注册表」（ADR-009） |
| U-10 行为日志事件 | 通过 | `tests/unit/test_observability.py`（ADR-010） |
| I-06 MCP 工具发现与调用 | 部分 | 发现、调用、审计链路已就位并有单元级证据；缺真实注册表，端到端验收待 C |
| I-07 可观测数据输出 | 未实现 | 未接 OpenTelemetry/Jaeger；Prometheus 目前只抓 dapr-sidecar |
| I-08 配置热更新 | 未实现 | `PATCH /api/v1/config/agents/{agent_id}` 未实现（`doc/api.md` §6） |
| I-09 只读巡检接口 | 通过 | `tests/integration/test_inspection_api.py`；用 SQLite 内存表与注入目录数据，不等于真实 PostgreSQL/MCP 验收 |

运行命令与结果：`uv run pytest -q` → **127 passed**（`tests/unit` 与 `tests/integration` 全部用例）。

## 5. 失败处理约定

- 任一用例失败：先复现，再定位，修复后将失败模式固化为新的测试或本文档约束；
- 对 Dapr/编排等共享行为，先写测试或同步补测试，不允许“看起来正确”代替；
- 每次里程碑结束时在报告记录：运行命令、通过数/失败数、失败原因。

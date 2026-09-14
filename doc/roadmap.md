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

### 已完成的里程碑

- **D1-D2（M1 单 Agent）**：Dapr CLI 初始化、Python 虚拟环境、LangGraph 单 Agent 原型与测试、
  Ollama 模型验证。
- **D3-D4（M2 Dapr 持久化）**：固定三步 Dapr Workflow + State Management、断点续跑演练脚本、
  编排状态契约与 Checkpoint 摘要、会话/记忆数据结构、会话与 Workflow REST API；
  API 发起的执行完成后由 durable 终态活动回写 completed/failed。
- **D5-D6（M3 多 Agent 协作）**：多 Agent 流水线的 LangGraph 图与角色分配、角色 Prompt 与示例场景、
  可恢复的多 Agent 子任务 Workflow、阶段活动默认调用真实 Ollama 模型
  （Fake 仅保留为恢复演练开关，见 ADR-007）；最终报告作为 `messages(role=assistant)`
  在终态回写并经 `GET /messages` 返回（见 ADR-008）。

### D7-D8（M4 工具生态与可观测）：进行中

按 `分工.md` §3 的四条分工记录落地情况。**M4 尚未完成**，缺口见下一节。

| 分工 | 负责 | 状态 | 落地内容 |
| --- | --- | --- | --- |
| 流水线接入 MCP 工具并验收 | A | 已完成 | 编排层冻结工具契约（`ToolSpec`/`ToolCall`/`ToolCallRecord`/`ToolRegistry`）与模型驱动的 ReAct 调用循环，阶段载荷回传 `tool_calls`，注册表缺失时保持原行为（ADR-009）；并补充结构化行为日志（ADR-010） |
| 支持工具调用审计落库 | B | 已完成 | `tool_calls` 表与 `app/core/tool_audit.py`：先写 running 再执行，成功/失败回写，按 `call_id` 幂等缓存、并发重放拒绝；阶段活动透传 `run_id`/`workflow_run_id` 接入审计（ADR-011） |
| 完成内置工具、沙箱、可观测接入 | C | 已完成（代码与单元级证据） | `app/tools` 四个内置工具（计算器 AST 白名单、网页搜索、沙箱代码执行、只读 SQL）、`app/sandbox` 策略层 + Docker 隔离后端、`app/mcp` Server/Client/注册表（inprocess/stdio/http 三种传输）、`app/observability` 追踪 + Prometheus 指标 + `metrics` 采样 + 模型回调（ADR-012） |
| 展示调用链路与 Token 统计 | D | 已完成（只读层） | 新增只读接口 `/providers`、`/agents/{id}`、`/tools`、`/workflows/{id}/tool-calls`、`/metrics`，Web 工作台接入工具调用详情与 Token 采样展示，契约见 `doc/api.md` §5 |

- **M4 缺口（C 侧已清）**：MCP 注册表与 4 个内置工具、工具沙箱隔离、
  OpenTelemetry 追踪、Prometheus 指标采集均已落地（ADR-012）。剩下的缺口不在 C：
  - `metrics` 表尚未建，`PostgresMetricSink` 只反射不建表，因此采样写入静默跳过、
    `/metrics` 仍返回 `availability=not_integrated`——建表属 B；
  - `/metrics` 与 `/tools` 的读取接线（把 `app.mcp.registry.tool_catalog()` 注入
    `InspectionStore`、暴露 Prometheus 文本端点）属 D，见 `doc/api.md` §5；
  - 容器环境变量（`OBS_TRACING_ENDPOINT` 等）与 Prometheus 抓取配置属部署侧。
- **待联调**：真实 `tool_calls` / `metrics` 写入的端到端链路（PostgreSQL + Dapr Worker）。
  审计链路（B）、注册表与工具（C）、只读接口（D）均已就位，缺的是真实数据库上的跑通。
- **其他未实现**：`PATCH /api/v1/config/agents/{agent_id}` 配置热更新（见 `doc/api.md` §6）。

### 验证记录

- 2026-09-11（合并 A/B/D 三份 D7-D8 提交后）：`uv run pytest -q` → **127 passed**，
  含 API 集成、工具审计、行为日志与流水线用例。
- 2026-09-14（C 的 D7-8 落地后）：`uv run pytest -q` → **288 passed / 1 failed**。
  新增 C 侧用例：`tests/unit/test_builtin_tools.py`（U-06）、
  `tests/unit/test_sandbox_policy.py`（U-08）、`tests/unit/test_mcp_tools.py`（I-06 单元级）、
  `tests/unit/test_observability_metrics.py`（I-07 单元级）。
  唯一失败是 A 的 `tests/unit/test_pipeline_tools.py::test_role_stage_without_registry_does_not_bind_tools`：
  它用「C 的模块不存在」构造「没有注册表」的前置条件，注册表落地后该前置条件不再成立
  （见 ADR-012「影响」）。
- 前端 TypeScript 与 Vite 构建通过（D 侧记录）。
- 注意事项：数据库读取用例使用 SQLite 内存表与注入目录数据，MCP 用例走内存协议往返而非
  跨进程 stdio，因此不代表真实 PostgreSQL、真实 MCP Server 或浏览器端到端验收。

### 范围说明

- D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

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
| 展示调用链路与 Token 统计 | D | 已完成（只读层与接线） | 新增只读接口 `/providers`、`/agents/{id}`、`/tools`、`/workflows/{id}/tool-calls`、`/metrics`，Web 工作台接入工具调用详情与 Token 采样展示（`doc/api.md` §5）；`app/api/main.py` 已注入 `tool_catalog` 并新增 Prometheus 文本端点 `/metrics`（`doc/api.md` §5.6），2026-09-15 补齐 |

- **M4 缺口（2026-09-15 全部落地）**：MCP 注册表与 4 个内置工具、工具沙箱隔离、
  OpenTelemetry 追踪、Prometheus 指标采集（ADR-012）；读取接线（工具目录注入与
  Prometheus 文本端点，`doc/api.md` §5.3、§5.6）；`metrics` 建表
  （`app/core/checkpoint.py::MetricRecord` + `init_checkpoint_schema()`，
  `doc/data-model.md` §3）；容器 `OBS_*` 与 Prometheus 抓取配置
  （`backend:8000/metrics`，见 `doc/deployment.md`）。
  仍在的缺口不在本轮范围：
  - A 的 `tests/unit/test_pipeline_tools.py::test_role_stage_without_registry_does_not_bind_tools`
    仍是过期前置条件，测试未全绿（需改为 `set_tool_registry_factory(lambda: None)`）；
  - 本地 qwen2.5-coder:7b 在真实运行中把工具调用当文本输出（例如
    `{"name": "web_search", "arguments": {...}}`），未返回原生 `tool_calls`，
    所以真实流水线的审计记录是 0 条；工具审计本身已在真实库上单独验证通过。
    这是模型/Prompt 行为问题，属 A 的模型接入范围。
- **端到端联调（2026-09-15 已跑通）**：真实 PostgreSQL + Dapr Workflow + Ollama 提交消息
  → Workflow `completed`（collect/analyze/report 三段）→ `metrics` 表写入 16 条采样
  （`stage_duration_ms`、`stage_runs`、`input_tokens`/`output_tokens`/`total_tokens`、
  `workflow_runs`）→ `GET /api/v1/metrics` 返回 `availability=available`；
  工具审计经真实注册表调用后落 `tool_calls` 一行（`calculator` `21*2` → `42`，
  `status=succeeded`）→ `GET /api/v1/workflows/{id}/tool-calls` 可读回。
  容器侧：`deploy/start.ps1` 重建镜像后 `backend:8000/metrics` 返回 6 个指标族，
  Prometheus 抓到 `backend`（`http://backend:8000/metrics`）与 `dapr-sidecar` 两个
  target 均为 `up`，`count({__name__=~"macp_.*"})` = 43 条序列；
  `GET /api/v1/tools` 在容器内返回 4 个工具且 `availability=available`；
  Jaeger 里 `macp-backend` 服务可查到 `stage.run`、`llm.chat` 等 span。
  追踪导出原先没有任何调用点（`configure_tracing()` 只在测试里被调用，真实运行中
  span 全进空操作 Tracer），本轮在 `app/workflows/worker.py::main()` 启动时补上配置，
  属成员 B 的 worker 入口。
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
- 2026-09-15（D 侧接线后）：`uv run pytest -q` → **291 passed / 1 failed**，
  新增工具目录默认接线与 Prometheus 文本端点用例；失败仍是上面 A 的过期用例。
  前端在 `npm ci` 后 `npm run build` 通过（首次失败是本地 `node_modules` 缺依赖，非代码问题）。
- 2026-09-15（M4 收口后）：`uv run pytest -q` → **294 passed / 1 failed**
  （新增 `tests/unit/test_metrics_table_schema.py`，校验 `metrics` 表 DDL 与索引契约）。
  真实环境验收：`init_checkpoint_schema()` 在 PostgreSQL 建出 `metrics`
  （列 `id/metric_name/value/labels/recorded_at`，索引 `idx_metrics_name_time`）；
  compose 栈上跑通一次完整 Workflow 并读回采样与审计记录；宿主进程起
  `uvicorn app.api.main:app` 后 `/metrics` 返回 6 个 `macp_*` 指标族、
  `/api/v1/tools` 返回 4 个工具且 `availability=available`；
  `promtool check config deploy/prometheus/prometheus.yml` → SUCCESS。
- 注意事项：数据库读取用例使用 SQLite 内存表与注入目录数据，MCP 用例走内存协议往返而非
  跨进程 stdio，因此不代表真实 PostgreSQL、真实 MCP Server 或浏览器端到端验收。

### 范围说明

- D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

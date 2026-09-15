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

### D7-D8（M4 工具生态与可观测）：代码完成，验收未闭环（2026-09-15）

按 `分工.md` §3 的四条分工记录落地情况。**四条分工的代码均已落地、测试全绿
（`uv run pytest -q` → 323 passed / 0 failed）；M4 验收未闭环**——A 的分工原文是
「流水线接入 MCP 工具**并验收**」，当前只有假模型单元级证据，真实模型路径下流水线
没有产生任何工具调用。验收路径已按 ADR-014 改为由 OpenAI 兼容 API 承担，Ollama
降为备用。状态口径与 `doc/testing.md` §4.1 一致，**不得只凭测试全绿判定 M4 完成**。

| 分工 | 负责 | 状态 | 落地内容 |
| --- | --- | --- | --- |
| 流水线接入 MCP 工具并验收 | A | 代码完成，验收未闭环 | 编排层冻结工具契约（`ToolSpec`/`ToolCall`/`ToolCallRecord`/`ToolRegistry`）与模型驱动的 ReAct 调用循环，阶段载荷回传 `tool_calls`，注册表缺失时保持原行为（ADR-009）；并补充结构化行为日志（ADR-010）。代码与单元级证据齐备，但真实模型路径未产生工具调用，验收证据待补 |
| 支持工具调用审计落库 | B | 已完成 | `tool_calls` 表与 `app/core/tool_audit.py`：先写 running 再执行，成功/失败回写，按 `call_id` 幂等缓存、并发重放拒绝；阶段活动透传 `run_id`/`workflow_run_id` 接入审计（ADR-011） |
| 完成内置工具、沙箱、可观测接入 | C | 已完成（代码与单元级证据） | `app/tools` 四个内置工具（计算器 AST 白名单、网页搜索、沙箱代码执行、只读 SQL）、`app/sandbox` 策略层 + Docker 隔离后端、`app/mcp` Server/Client/注册表（inprocess/stdio/http 三种传输）、`app/observability` 追踪 + Prometheus 指标 + `metrics` 采样 + 模型回调（ADR-012） |
| 展示调用链路与 Token 统计 | D | 已完成（含 Provider 配置写入面板） | 新增只读接口 `/providers`、`/agents/{id}`、`/tools`、`/workflows/{id}/tool-calls`、`/metrics`，Web 工作台接入工具调用详情与 Token 采样展示（`doc/api.md` §5）；`app/api/main.py` 已注入 `tool_catalog` 并新增 Prometheus 文本端点 `/metrics`（`doc/api.md` §5.6），2026-09-15 补齐；同日「工具与配置」页新增 Provider 配置表单，读写 §5.8（含 403 提示、清空即回退环境配置、清除覆盖按钮），令牌由操作者手动输入且只留内存 |

- **M4 代码落地情况（2026-09-15）**：代码侧原缺口已全部落地——MCP 注册表与 4 个内置工具、工具沙箱隔离、
  OpenTelemetry 追踪、Prometheus 指标采集（ADR-012）；读取接线（工具目录注入与
  Prometheus 文本端点，`doc/api.md` §5.3、§5.6）；`metrics` 建表
  （`app/core/checkpoint.py::MetricRecord` + `init_checkpoint_schema()`，
  `doc/data-model.md` §3）；容器 `OBS_*` 与 Prometheus 抓取配置
  （`backend:8000/metrics`，见 `doc/deployment.md`）。
  **M4 验收未闭环的唯一阻塞项（属 A 的 D7-D8 范围，尚未收口）**：
  - 本地 qwen2.5-coder:7b 在真实运行中把工具调用当文本输出（例如
    `{"name": "web_search", "arguments": {...}}`），未返回原生 `tool_calls`，
    所以真实流水线没有工具调用、审计记录为 0 条；工具审计链路本身已在真实库上
    单独验证通过。2026-09-15 复核：Ollama 把该模型标记为 tools-capable，其模板
    要求调用包裹在 `<tool_call></tool_call>` 内，而实测两次（含显式格式示范的
    system 提示）均返回裸 JSON，属**指令遵从度问题，不是模型能力缺失**。
  - 2026-09-15 决策（ADR-014）：模型接入改为「API 优先、Ollama 备用」，
    Provider 配置（provider/model/base_url/api_key/temperature）由系统保存
    （`provider_configs` 事实源 + Redis 镜像，`doc/api.md` §5.8）。
    **待办：拿到可用凭据后在 API 模型下重跑真实 Workflow，产出 `tool_calls`
    审计记录即可关闭本缺口。**
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
- **I-08 配置热更新（2026-09-15 补齐）**：`PATCH /api/v1/config/agents/{agent_id}`
  已实现（`doc/api.md` §5.7、ADR-013），覆盖值落 `agent_configs`，阶段活动执行时解析，
  无需重启进程；写接口以 `ADMIN_TOKEN` 做 fail-closed 权限边界。
  真实环境验收：在宿主机 API（配了 `ADMIN_TOKEN`）把 `analyst` 的 model 覆盖成
  `qwen2.5-coder:7b-does-not-exist` 后，**未重启**的容器 backend 立刻在
  `GET /agents/analyst` 返回覆盖值，下一次 Workflow 在 analyze 阶段失败于
  `ResponseError: model 'qwen2.5-coder:7b-does-not-exist' not found (404)`
  （collect 已用环境配置跑完）；清除覆盖后再次提交任务恢复 `completed`。
  容器未配置 `ADMIN_TOKEN` 时同一请求返回 `403 CONFIG_WRITE_FORBIDDEN`。

### D9-10（M5 端到端与性能）：进行中

按 `分工.md` §3 记录本轮（角色 C：端到端测试补缺、性能数据）的落地情况。

| 分工 | 负责 | 状态 | 落地内容 |
| --- | --- | --- | --- |
| 端到端测试补缺 | C | 已完成 | `tests/e2e/`：无容器回归网（真实 API/Workflow/流水线/工具/可观测，仅替换 Dapr 运行时与 PostgreSQL 落库）与真实 compose 验收入口（默认 skip，`MACP_E2E_LIVE=1` 启用，覆盖 E-01/E-02/E-04/I-06/E-05 健康）；E-03 由 `scripts/measure_recovery.py` 脚本化测量；并发会话由 `scripts/perf_concurrency.py` 测量（ADR-015） |
| 性能数据 | C | 已完成 | 10 并发会话：成功率 1.0、受理延迟 p50 0.46s、端到端 p50 16.54s / p95 18.61s、Token 18650（621.7/次）；E-03 恢复耗时 ≤0.2s（目标 <5s）、不可用 2.62s；数据采集通道与口径见 ADR-015 |
| Web UI 与部署编排 | D | 未完成 | Web 控制台、`start.ps1`/`stop.ps1` 全流程尚未在 M5 口径下验收（本轮只做了 compose 已起后的健康检查） |

- **M5 未完成**：E-01/E-02 的「结构化报告」在真实模型下不达成（缺口 F-02：
  模型把工具调用写成纯文本 `content`，`tool_calls=0`，工具未真正执行；
  该结论实测于 Ollama `qwen2.5-coder:7b`，默认提供方已按 ADR-014 改为
  OpenAI 兼容 API，**拿到凭据后需在 API 模型下重跑真实 Workflow 才能定论**）；
  E-05 的 `start.ps1 → 健康检查 → stop.ps1` 全流程、E-04 的 Web UI 侧均未验。
- **本轮新发现的跨模块缺口（均未改他人代码，见 ADR-015）**：
  F-01 跨阶段同工具调用被审计主键合并、返回首个结果（A+B，**仍未清**）；
  F-02 真实模型不产出结构化 `tool_calls`（需 A/B 决策）；
  F-03 Token 采样缺 `model` 标签（C 侧，**已修复**：取回调 `metadata["ls_model_name"]`）；
  F-04（B：`metrics` 表；D：`/tools` 接线与 Prometheus 文本端点；A：失败用例与 I-08）
  ——**三项均已落地**（`metrics` 建表与真实库采样、容器 `/metrics` 抓取、I-08 配置热更新）；
  F-05 poc/故障演练路径业务终态 `completed` 与 Dapr 终态 `FAILED` 不一致（B，
  **已修复**：`session_id` 不再硬编码为 `demo-session`，CLI 对运行时终态非 `COMPLETED`
  即非零码退出）——修复后的恢复演练需重跑一遍；
  F-06 会话/长期记忆未接入编排（`app/memory/` 只有 Protocol，历史消息既不落记忆
  也不回注 Prompt）——**本轮只记录，未处置**。

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
- 2026-09-15（A 修复过期前置条件后）：`uv run pytest -q` → **323 passed / 0 failed**。
  `tests/unit/test_pipeline_tools.py::test_role_stage_without_registry_does_not_bind_tools`
  改用 `set_tool_registry_factory(lambda: None)` 显式构造「没有注册表」的前置条件；
  该用例原先依赖「`app/mcp` 不存在」，成员 C 落地注册表后 `default_tool_registry()`
  会回退到真实注册表，前置条件失效（见 ADR-012）。用例意图与断言未改动。
- 2026-09-15（API 优先接入落地后）：`uv run pytest -q` → **366 passed / 0 failed**。
  新增 `tests/unit/test_provider_config.py`（26 例）与
  `tests/integration/test_provider_config_api.py`（15 例），覆盖 Provider 配置的
  合并顺序、Redis 镜像降级、密钥脱敏与 §5.8 契约；默认提供方改为 OpenAI 兼容 API
  （ADR-014），`tests/test_langgraph_agent.py` 同步更新默认值与缺凭据 fail-fast 用例。
- 2026-09-15（前端配置面板落地后）：`npm --prefix frontend run build` → 通过
  （`tsc --noEmit && vite build`）。后端真实链路冒烟（本机 PostgreSQL 5433 +
  Redis 6380 + uvicorn，`ADMIN_TOKEN` 已配置）：无令牌/错误令牌 `PUT` → `403
  CONFIG_WRITE_FORBIDDEN`；正确令牌 `PUT` → `200` 且响应与回读都不含密钥；
  `/providers` 反映合并后的生效值；删除 `provider:config` 缓存后 `GET` 仍返回
  存储值并自动回填。冒烟写入的行与缓存键已清理。前端目前没有测试框架，
  门禁是类型检查 + 构建，见 `doc/testing.md` §3.3。
- 2026-09-15（C 的 D9-10 落地后，合入 master 前的分支实测）：`uv run pytest -q` →
  **300 passed / 1 failed / 5 skipped**（1 failed 为当时 A 的过期前置条件用例，
  已在 master 上修复；5 skipped 为需要 compose 的 `tests/e2e/test_live_e2e.py`）。
  真实 compose 环境：`MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s` →
  4 passed + 1 xfail（F-02 记为未达成）；并发与恢复实测数据见 ADR-015。
- 2026-09-15（C 的分支合入 master 后）：`uv run pytest -q` → **378 passed / 0 failed /
  5 skipped**（378 = master 的 366 例 + 本分支新增 12 例，5 skipped 为需要 compose 的
  `test_live_e2e.py`）。合并时 `doc/roadmap.md`、`doc/testing.md` 各有同点追加型冲突，
  已按「master 记录在前、本分支记录在后」解决；并做了语义对齐：本分支 ADR 改号为
  **015**（原 013 与 master 的 `013-agent-config-hot-update` 重号）、F-04 三项前置
  标为已落地、F-05 标为已修复、F-02 补上「实测于 Ollama `qwen2.5-coder:7b`、
  默认提供方改 API 后需凭据复测」的前提。
- 注意事项：数据库读取用例使用 SQLite 内存表与注入目录数据，MCP 用例走内存协议往返而非
  跨进程 stdio，因此不代表真实 PostgreSQL、真实 MCP Server 或浏览器端到端验收。
  本轮已补上真实 compose 上的 REST 链路验收，但 Web UI 侧（E-04）与
  `start.ps1`/`stop.ps1` 全流程（E-05）仍未验。

### 范围说明

- D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

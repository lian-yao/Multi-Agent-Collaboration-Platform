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

### D7-D8（M4 工具生态与可观测）：已完成（2026-09-15）

按 `分工.md` §3 的四条分工记录落地情况。**四条分工均已完成，M4 验收通过**：
真实 Dapr Workflow + OpenAI 兼容 API（`deepseek-flash`）跑通 collect → analyze → report，
collector 与 analyst 两阶段各产生一次 `calculator` 工具调用（`21*2`、`21+21`，均
`succeeded`），审计落 `tool_calls` 表并可经 `GET /workflows/{id}/tool-calls` 读回；
Token 采样按阶段记录（total 1020 / 1344 / 3051）。踩坑与决策见下，证据见验证记录。
状态口径与 `doc/testing.md` §4.1 一致。

| 分工 | 负责 | 状态 | 落地内容 |
| --- | --- | --- | --- |
| 流水线接入 MCP 工具并验收 | A | 已完成 | 编排层冻结工具契约（`ToolSpec`/`ToolCall`/`ToolCallRecord`/`ToolRegistry`）与模型驱动的 ReAct 调用循环，阶段载荷回传 `tool_calls`，注册表缺失时保持原行为（ADR-009）；并补充结构化行为日志（ADR-010）。真实模型验收见下方验证记录 |
| 支持工具调用审计落库 | B | 已完成 | `tool_calls` 表与 `app/core/tool_audit.py`：先写 running 再执行，成功/失败回写，按 `call_id` 幂等缓存、并发重放拒绝；阶段活动透传 `run_id`/`workflow_run_id` 接入审计（ADR-011） |
| 完成内置工具、沙箱、可观测接入 | C | 已完成（代码与单元级证据） | `app/tools` 四个内置工具（计算器 AST 白名单、网页搜索、沙箱代码执行、只读 SQL）、`app/sandbox` 策略层 + Docker 隔离后端、`app/mcp` Server/Client/注册表（inprocess/stdio/http 三种传输）、`app/observability` 追踪 + Prometheus 指标 + `metrics` 采样 + 模型回调（ADR-012） |
| 展示调用链路与 Token 统计 | D | 已完成（含 Provider 配置写入面板） | 新增只读接口 `/providers`、`/agents/{id}`、`/tools`、`/workflows/{id}/tool-calls`、`/metrics`，Web 工作台接入工具调用详情与 Token 采样展示（`doc/api.md` §5）；`app/api/main.py` 已注入 `tool_catalog` 并新增 Prometheus 文本端点 `/metrics`（`doc/api.md` §5.6），2026-09-15 补齐；同日「工具与配置」页新增 Provider 配置表单，读写 §5.8（清空即回退环境配置、清除覆盖按钮），写入不鉴权（ADR-015） |

- **M4 代码落地情况（2026-09-15）**：代码侧原缺口已全部落地——MCP 注册表与 4 个内置工具、工具沙箱隔离、
  OpenTelemetry 追踪、Prometheus 指标采集（ADR-012）；读取接线（工具目录注入与
  Prometheus 文本端点，`doc/api.md` §5.3、§5.6）；`metrics` 建表
  （`app/core/checkpoint.py::MetricRecord` + `init_checkpoint_schema()`，
  `doc/data-model.md` §3）；容器 `OBS_*` 与 Prometheus 抓取配置
  （`backend:8000/metrics`，见 `doc/deployment.md`）。
  **缺口二已收口（2026-09-15）**，过程与结论保留如下：
  - 原阻塞：本地 qwen2.5-coder:7b 在真实运行中把工具调用当文本输出（例如
    `{"name": "web_search", "arguments": {...}}`），未返回原生 `tool_calls`，
    所以真实流水线没有工具调用、审计记录为 0 条；工具审计链路本身已在真实库上
    单独验证通过。2026-09-15 复核：Ollama 把该模型标记为 tools-capable，其模板
    要求调用包裹在 `<tool_call></tool_call>` 内，而实测两次（含显式格式示范的
    system 提示）均返回裸 JSON，属**指令遵从度问题，不是模型能力缺失**。
  - 2026-09-15 决策（ADR-014）：模型接入改为「API 优先、Ollama 备用」，
    Provider 配置（provider/model/base_url/api_key/temperature）由系统保存
    （`provider_configs` 事实源 + Redis 镜像，`doc/api.md` §5.8）。
  - 验收：`deepseek-flash` 真实运行产出 2 条 `tool_calls`（均 `succeeded`），
    报告正文引用了工具返回值 `42`。首次试跑失败暴露的过程问题：模型名写成
    `deep` 时 DeepSeek 返回 400 并列出支持的名称，说明**配置错误会以阶段失败
    fail-fast 暴露**，不会被吞掉。
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
  **2026-09-15 更新**：该令牌边界已按 ADR-015 取消，上面的令牌步骤不再适用。

### D9-10（M5 端到端与性能）：已完成（2026-09-16）

按 `分工.md` §3 记录本轮（角色 C：端到端测试补缺、性能数据）的落地情况。

| 分工 | 负责 | 状态 | 落地内容 |
| --- | --- | --- | --- |
| 端到端测试补缺 | C | 已完成 | `tests/e2e/`：无容器回归网（真实 API/Workflow/流水线/工具/可观测，仅替换 Dapr 运行时与 PostgreSQL 落库）与真实 compose 验收入口（默认 skip，`MACP_E2E_LIVE=1` 启用，覆盖 E-01/E-02/E-04/I-06/E-05 健康）；E-03 由 `scripts/measure_recovery.py` 脚本化测量；并发会话由 `scripts/perf_concurrency.py` 测量（ADR-016） |
| 性能数据 | C | 已完成（**2026-09-20 真实 API 提供方重测，以本节末条为准**） | 10 并发会话：成功率 1.0、受理延迟 p50 **0.348s**、端到端 p50 **54.63s** / p95 74.57s、Token **432596（4241.1/次）**、工具成功率 **0.6798**；E-03 恢复 **2.48s**（目标 <5s）、重启 3.07s、两个终态同时成功。原 Ollama/poc 口径：p50 16.54s、Token 621.7/次；通道与口径见 ADR-016 |
| Web UI 与部署编排 | D | 已完成（浏览器渲染除外） | `deploy/start.ps1` / `stop.ps1` 全流程实跑通过，并修复两处 Windows PowerShell 5.1 下被 `docker compose` stderr 中断的缺陷；E-04 的 Web 侧数据路径（经 nginx 反代）补了自动化用例；`doc/deployment.md` 补「演示与验收」与浏览器核对清单。2026-09-15 晚又用 `gpt-5.5` 把依赖模型的 4 条 live 用例补跑通过（7 passed）。详见 `doc/testing.md` §4.3 与 §4.3.1 |

- **M5 状态**：E-01/E-02 的「结构化报告」曾因缺口 F-02 不达成，该缺口已在
  API 模型下复测通过（见「验证记录」中 2026-09-15 的 M4 真实验收）后关闭。
  D 侧的 E-05（`start.ps1 → 健康检查 → stop.ps1` 全流程）与 E-04 的 Web 侧数据路径
  已于 2026-09-15 实跑通过。
  **M5 已闭环（2026-09-16 回填）**：「任务历史」的完整列表缺口已由新增
  `GET /api/v1/sessions` 枚举接口 + 任务记录页「历史会话」分区补上（`doc/api.md`
  §5.13），任务记录页因此从三分区扩为四分区；浏览器渲染仍以人工核对 + 渲染冒烟
  （`frontend/rendercheck/`，jsdom 端到端断言）为准，前端未引入浏览器自动化属既定边界。
  本轮的并发与恢复性能数据仍测于 Ollama 提供方 + poc 假模型路径，
  默认提供方改 API 后建议带凭据重取一轮。
- **本轮新发现的跨模块缺口（均未改他人代码，见 ADR-016）**：
  F-01 跨阶段同工具调用被审计主键合并、返回首个结果（A+B，**已于 2026-09-20 修复**，
  见下方验证记录）；
  F-02 真实模型不产出结构化 `tool_calls`（C 侧上报，**已关闭**：Ollama
  `qwen2.5-coder:7b` 下模型把调用写成 `content` 里的裸 JSON；默认提供方改
  OpenAI 兼容 API 后，`deepseek-flash` 在 collector/analyst 两阶段各产生一次
  `calculator` 调用并落审计表）；
  F-03 Token 采样缺 `model` 标签（C 侧，**已修复**：取回调 `metadata["ls_model_name"]`）；
  F-04（B：`metrics` 表；D：`/tools` 接线与 Prometheus 文本端点；A：失败用例与 I-08）
  ——**三项均已落地**（`metrics` 建表与真实库采样、容器 `/metrics` 抓取、I-08 配置热更新）；
  F-05 poc/故障演练路径业务终态 `completed` 与 Dapr 终态 `FAILED` 不一致（B，
  **已修复**：`session_id` 不再硬编码为 `demo-session`，CLI 对运行时终态非 `COMPLETED`
  即非零码退出）——修复后的恢复演练需重跑一遍；
  F-06 会话/长期记忆未接入编排（当时 `app/memory/` 只有 Protocol，历史消息既不落记忆
  也不回注 Prompt）——**当时只记录，未处置**。2026-09-16 合入 C-1 后 Redis 实现已落地
  并有单测覆盖，但编排与 API 仍无调用方，口径见下文「M5 之后的增量」；
  **2026-09-20 会话记忆接线完成**（ADR-019），长期记忆仍无调用方。

### M5 之后的增量（2026-09-16，合入 PR #9 与 C-1）

M5 闭环后又有两批改动合入 `master`，当时都未回填本文件的进度与验证记录，此处补齐。

| 批次 | 提交 | 落地内容 |
| --- | --- | --- |
| PR #9（成员 D，`member-d/d9-10-delivery`） | `8d1ab87`、`239a113`、`a9a1cf6`、`9ef8400`、`39048d7` | ADR-017 多 Provider / 多模型 / MCP Server 三张注册表与配置 API（`doc/api.md` §5.9–§5.12）、历史会话枚举 `GET /api/v1/sessions`（§5.13）与删除（§5.14）、ADR-018 工作台视图三分；`agent_registry` 角色目录与自定义角色 CRUD（含前端「新建/删除」入口）；前端重构为「工作台 / 配置 / 记录」三页 |
| C-1（成员 C） | `eda7758` | 单元与 E2E conftest 的 DSN 自足化：在导入 `app.*` 之前把 DSN 钉成内存 SQLite，配置库不可达时不再阻塞在建连上 |
| 同上 | `31d9cef` | 根目录 `.env_example` 补齐 `TOOL_` / `MCP_` / `SANDBOX_` / `OBS_` 四段（原有 `AGENT_*` / `REDIS_URL` / `DATABASE_URL`） |
| 同上 | `44712db` | `sql_default_limit` 真正生效；搜索失败给出可排查的原因 |
| 同上 | `69960e1` | Redis 会话记忆与长期记忆实现（`app/memory/redis_store.py`，19 例单测） |
| 同上 | `38200cc` | 沙箱 Docker 后端隔离与 fail-closed 回归测试（仅测试，`app/sandbox` 无改动） |
| 同上 | `f043dbf` | 已注册的外部 MCP Server 并入流水线工具注册表；新增跨进程 stdio 端到端用例 |

- **合并方式与完整性**：C-1 的首个提交 `eda7758` 的父提交即 `88e7390`（PR #9 的合并提交），
  `master` 以快进方式合并到 `f043dbf`，未产生合并提交，也未回退 PR #9 的任何产物。
- **缺口状态变化**：
  - F-06 口径变更：`app/memory/` 不再「只有 Protocol」——Redis 实现已落地且有单测，
    但 `app/api`、`app/orchestration`、`app/workflows` 里仍无消费者，历史消息既不落记忆
    也不回注 Prompt，即**「实现已就绪、接线未做」**。（该缺口已于 2026-09-20 收口，
    见下方验证记录。）
  - I-06 的遗留缺口「仍缺跨进程 stdio 传输的端到端用例」已由
    `tests/e2e/test_mcp_stdio_e2e.py`（真实启动 `python -m app.mcp.server` 子进程）关闭。
  - F-01 已于 2026-09-20 修复（调用 ID 派生键加入阶段名，见验证记录）；另外
    `38200cc` 的提交信息写的是「Docker 后端的隔离参数」，但 diff 内
    没有任何 `app/sandbox` 变更，实际只补了测试——若原意是加隔离参数，那部分没有进来。
- **文档缺口**：上述两批提交都没有改 `doc/`（C-1 只改了根目录 `.env_example`），
  所以本文件与 `doc/testing.md` 的验证记录一度停留在 371 例；
  `doc/decisions/005-memory-schema.md` §3 的「真实存储实现由 `app/core`（成员 B）接入」
  也已与代码不符。以上均已按实际修订。

### 验证记录

> 本节按时间追加，**最新一条为准**；更早条目里的用例数是当时的快照，不代表当前状态。

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
  4 passed + 1 xfail（当时 F-02 未达成，见下）；并发与恢复实测数据见 ADR-016。
- 2026-09-15（取消写入令牌后，ADR-015）：`uv run pytest -q` → **359 passed / 0 failed**
  （删除 7 个令牌用例、各留 1 例「裸请求可写」；`AdminSettings`/`get_admin_settings()`
  一并移除，用例数下降属预期），`npm --prefix frontend run build` 通过。
  真实环境复测（本机 PostgreSQL 5433 + Redis 6380 + uvicorn，**不配置任何令牌**）：
  `PUT /api/v1/config/provider` → `200` 且响应不含密钥；`GET` 回读生效值；
  `/providers` 反映新值；删除 `provider:config` 后 `GET` 仍返回存储值并自动回填。
  冒烟数据与缓存键已清理。
- 2026-09-15（M4 真实验收，缺口二关闭）：容器栈（compose：backend + dapr-sidecar +
  scheduler + placement + PostgreSQL + Redis + Jaeger + Prometheus）上，
  Provider 配置由界面写入（`provider=openai`，`model=deepseek-flash`，
  `base_url=https://api.deepseek.com`，凭据已配置），提交「用 calculator 计算 21*2」
  后 Workflow `completed`（collect → analyze → report）：
  - `GET /workflows/{id}/tool-calls` → 2 条 `calculator` 记录（`21*2`、`21+21`，
    均 `succeeded`，输出 `{"value": 42}`）；
  - 报告作为 `messages(role=assistant)` 落库，1877 字，正文引用工具返回值 `42`；
  - `GET /metrics?workflow_id=...` → 20 条采样，含各阶段 `input/output/total_tokens`
    （collector 943/77/1020、analyst 1216/128/1344、reporter 1858/1193/3051）与
    `tool_calls`/`tool_call_duration_ms`（`tool_name=calculator`）；
  - 对照：同一任务在 `deepseek-v4-pro` 上也产生 1 条成功调用（`21*2`）。
  失败样本保留：模型名填 `deep` 时阶段 `collect` 以 400 失败、
  Workflow 置 `failed`，`workflow_runs.error` 记录服务端原文。
- 2026-09-15（C 的分支再次合入 master 后）：`uv run pytest -q` → **371 passed /
  0 failed / 5 skipped**（5 skipped 为需要 compose 的 `test_live_e2e.py`；
  用例数比 master 的 359 多出的是本分支的 E 系列与性能口径用例，
  比上一轮的 378 少则是 master 删掉 7 个令牌用例所致）。
  合并时 `doc/roadmap.md`、`doc/testing.md` 各有同点追加型冲突，已按
  「master 记录在前、本分支记录在后」解决；并做语义对齐：本分支 ADR 改号为
  **016**（013 与 015 已分别被 master 的配置热更新、取消写入令牌占用）、
  F-02 按 master 的真实 API 验收标为**已关闭**、F-04 三项前置标为已落地、
  F-05 标为已修复。
- 2026-09-15（D 侧 D9-10：Web UI 与部署编排）：在本分支补 D 负责的两行。
  - **修复**：`deploy/start.ps1` 与 `deploy/stop.ps1` 在 Windows PowerShell 5.1 下被
    `docker compose` 写到 stderr 的构建/停止进度中断——脚本顶部
    `$ErrorActionPreference = "Stop"` 会把原生命令的 stderr 当成终止性错误，
    于是 `start.ps1` 在镜像构建成功、容器已起来之后直接退出，**既不执行三段健康检查
    也不打印访问地址**；`stop.ps1` 对已经完成的停止报错并返回非零码。两处都改为在该
    调用期间临时切到 `Continue` 并以 `$LASTEXITCODE` 判定成败（与文件里 `docker info` /
    `compose version` / `compose config` 的既有写法一致）。
  - **新增验收用例**（`tests/e2e/test_live_e2e.py`，仍由 `MACP_E2E_LIVE=1` 开关控制，
    默认 skip，因此无容器环境下 `uv run pytest` 依旧全绿）：E-05 Web 侧
    （`frontend` 容器可访问、SPA 入口引用的构建产物可取到、nginx 把 `/api` 反代到
    backend、`/providers` 与 `/config/provider`/`/tools`/`/metrics` 经反代可用且
    `availability=available`、Provider 配置响应不含密钥）与 E-04 Web 侧
    （经反代跑完「新建任务 → 暂停 → 暂停期提交被拒 409 → 恢复 → 回读」六步）。
  - **文档**：`doc/deployment.md` 新增「演示与验收（D9-10，成员 D）」——E-05 的通过标准
    （退出码 + 三段健康检查 + `stop` 后容器为空）、E-04 的自动化命令与浏览器核对清单；
    同时修正过时的模型前置条件（默认提供方已是 OpenAI 兼容 API，ADR-014，
    原文「宿主机需要运行 Ollama」不再成立）；`doc/testing.md` 补 §4.3 与
    「`uv run pytest` 需 PostgreSQL/Redis 可达」的前置说明。
  - 验证（本机 compose）：`uv run pytest -q -p no:cacheprovider` → **359 passed / 0 failed**，
    14.51s（未起 PostgreSQL/Redis 时同一命令 30 分钟无结果，说明该前置条件此前没有写明）；
    `MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s -k "frontend or
    web_ui_session or health"` → **3 passed**；`npm --prefix frontend run build` 通过
    （3134 modules，产物 `index-*.js` 251.80 kB）；
    `deploy/start.ps1` → `deploy/stop.ps1` 实跑各一次，均为
    `SCRIPT_OK=True / LASTEXITCODE=0`，中间打印三段 `is healthy` 与 7 行访问地址，
    停止后项目容器全部移除。
  - **未完成**（2026-09-15 当轮）：依赖模型的 4 条 live 用例（E-01/E-02 两条、I-06、
    E-04 API 侧续跑）未跑，本机无可用模型凭据；浏览器渲染仍为人工核对；「任务历史」的
    完整列表需要新增接口，未做。
- 2026-09-15（D 侧 D9-10 补跑：模型侧 live 验收）：拿到可用 API 提供方后，起全栈并用
  `gpt-5.5` 把上一条「未完成」里依赖模型的 4 条补齐。
  - 环境：`deploy/start.ps1` 起 compose（9 个服务全部 Healthy）；
    `PUT /api/v1/config/provider` 写入 `provider=openai` / `model=gpt-5.5` /
    `base_url=http://host.docker.internal:3000/v1`（容器内访问宿主机网关必须走
    `host.docker.internal`，`compose.yaml` 已配 `extra_hosts`）。
  - 结果：`MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest
    tests/e2e/test_live_e2e.py -q -s` → **7 passed**（714.75s，无 skip）。
    E-01/E-02 三步流水线 `completed`（workflow `9b48b152`，330.9s，
    `completed_steps=[collect, analyze, report]`、`current_step=None`），报告 3983 字符
    结构化 Markdown 而非工具调用 JSON，ADR-016 F-02 在 API 提供方下确认关闭；
    I-06 `/workflows/{id}/tool-calls` 返回 `availability=available`；
    E-04 API 侧续跑 88.3s 到 `completed`（并发与恢复两组性能数据仍未在 API 模型下重取）。
  - 两个环境约束（已写入 `doc/testing.md` §4.3.1）：
    (1) `MACP_E2E_TIMEOUT` 默认 300s 偏紧——单次 LLM 调用实测 24–35s，
    三步流水线叠加工具轮次可达 330s，用默认值会**偶发**判超时（首轮实测一个 workflow
    318s，恰超 300s 上限）；
    (2) **本机整体无公网出口**——容器与宿主机访问 `api.duckduckgo.com` 均超时
    （宿主机 10s 返回 `000`），`web_search` 必然以
    `ToolExecutionError: 搜索服务不可达: timed out` 落库并重试，进一步拉长耗时。
    要稳定复现需把 `TOOL_SEARCH_ENDPOINT` 指向可达搜索服务，或在无网环境不向 Agent
    暴露该工具——属工具层配置（成员 C 范围），本轮未改。
  - **顺带发现的测试隔离缺口**（未修，仅记录）：Provider 覆盖一旦写进 PostgreSQL，
    `uv run pytest` 会有 8 条转红（`tests/unit/test_agent_config.py` 3、
    `tests/integration/test_config_api.py` 2、`tests/integration/test_inspection_api.py` 3）
    ——这批用例 monkeypatch 了 `AgentSettings` / `list_agent_configs`，却没有屏蔽库里的
    *活的* Provider 覆盖。显式 `PUT` 全 `null` 清除覆盖后 8 条立即恢复通过（38 passed），
    全量回到 **371 passed / 7 skipped**。
- 2026-09-16（合入 PR #9 与 C-1 后，本机 compose 全栈在跑）：`uv run pytest -q` →
  **606 passed / 5 failed / 7 skipped，17.01s**（共收集 618 例；上面 371 例是
  2026-09-15 的快照，PR #9 的注册表用例与 C-1 的记忆/沙箱/stdio 用例都未计入）。
  7 skipped 全部是 `tests/e2e/test_live_e2e.py` 里受 `MACP_E2E_LIVE=1` 控制的
  compose 验收用例。
  5 条失败**全部在集成层**，是同一处测试隔离缺口：`tests/integration/` 至今没有 conftest，
  DSN 落到 `app/core/storage.py` 的默认 `localhost:5433/multi_agent`，于是这些用例读到
  开发环境里真实保存的 `provider_configs` 行（`openai` / `deepseek-flash`），与
  「环境配置 + 无覆盖」的期望值冲突——`tests/integration/test_config_api.py`
  （`test_patch_updates_effective_config`、`test_null_clears_override`）与
  `tests/integration/test_inspection_api.py`（`test_provider_and_agent_agree` 两参数化、
  `test_missing_model`）。单元层的同类失败已由 `eda7758` 修复，这条记录取代
  2026-09-15 的「8 条转红」口径（当时 3 条在单元层，现为 0 条）。
- 2026-09-20（修复集成层测试隔离，`codex/integration-test-isolation`）：新增
  `tests/integration/conftest.py`（导入 `app.*` 之前把 DSN 钉成内存 SQLite；
  autouse fixture 用 `set_redis_factory` 注入 Provider 配置的内存 Redis 替身）与
  `tests/integration/test_isolation_contract.py`（隔离契约用例：覆盖行读不到 + 默认 DSN
  不是开发库，删掉 conftest 任一半即转红）。
  - **根因更正**：上一条把泄漏源记成「读开发库 `provider_configs` 行」，实测主泄漏源是
    **本机 Redis 的活镜像 `provider:config`**（`provider_config_row()` 的解析顺序是
    Redis 优先）。单把 `DATABASE_URL` 钉成内存 SQLite 后 5 条仍失败，必须同时替换 Redis
    客户端才归零；`tests/unit/conftest.py` 早已有该替身，集成层此前没有 conftest。
  - 验证：`uv run pytest tests/integration -q` → **108 passed，2.12s**
    （修复前 5 failed / 101 passed）；`uv run pytest -q` → **613 passed / 0 failed /
    7 skipped，15.49s**（620 例，含新增 2 条契约用例）；把 `REDIS_URL` 指向不可达端口后
    全量仍 **613 passed / 7 skipped**（89.24s）。反向对照：不经 conftest 的裸进程
    `provider_config_row()` 仍返回 `deepseek-flash`，证明隔离由 conftest 提供而非环境巧合。
  - **同日续修（E2E 回归网同类泄漏，已收口）**：首轮只改了集成层，随后实测确认
    `tests/e2e/` 同样只钉 DSN——Provider 配置镜像仍连本机 Redis，语义上不是「无覆盖」，
    Redis 不可达时该批用例 6.63s → 80.09s。修法与集成层一致：
    `tests/e2e/conftest.py` 增加 `MemoryRedis` + autouse fixture，
    `tests/e2e/test_regression_net_isolation.py` 固化契约（文件名避免与集成层重名，
    否则 pytest 默认导入模式下同名模块互相顶掉）。先确认 RED（读到 `deepseek-flash`）
    再上替身转 GREEN。
  - 续修后验证：`uv run pytest tests/e2e -q`（Redis 不可达）→ **13 passed，6.31s**；
    `REDIS_URL=redis://localhost:6399/0 uv run pytest -q` → **615 passed / 0 failed /
    7 skipped，15.15s**（隔离前同一命令 89.24s）；全量用例数 620 → 622。
- 2026-09-20（修复 F-01：跨阶段工具调用审计键碰撞，`codex/f01-stage-scoped-tool-call-ids`）：
  调用 ID 的派生键加入**阶段名**——`app/orchestration/tools.py::tool_call_id(..., stage=...)`
  按 `macp:tool:{scope}:{stage}:{index}:{tool_name}` 派生，`ToolCaller` 增加 `stage`，
  `app/orchestration/pipeline_graph.py::run_role_stage` 与
  `build_multi_agent_pipeline` 构造 `ToolCaller` 时带上阶段。修复前 `ToolCaller` 每阶段重建、
  `index` 从 0 起算，而 ID 只含 Workflow 级 scope，导致同一 Workflow 的两个阶段在相同序号调
  同名工具时 ID 相同：`app/core/tool_audit.execute_tool_call` 按 ID 幂等，后一个阶段直接返回
  前一个阶段的**缓存结果**、审计只落 1 行（真实模型下即「分析师/报告员拿到收集者的结果」）。
  修法与 `app/core/tool_audit.py` 模块文档「call_id 取自 workflow_id + stage + tool_name」
  的约定一致；同阶段重放仍得到同一 ID，Dapr 活动重放的幂等语义不变。
  - **先红后绿**：新增 `tests/unit/test_pipeline_tools.py`
    （`test_role_stage_scopes_tool_call_ids_by_stage`、
    `test_role_stage_keeps_tool_call_id_stable_when_the_same_stage_replays`、
    `test_tool_caller_stage_is_part_of_the_derived_call_id`），修复前实测两阶段 call_id
    完全相同（`11f76dff-…` == `11f76dff-…`）。
  - E2E 原先把缺陷当契约：`test_audit_key_collapses_distinct_calls_across_stages`
    断言「三次调用只落 1 行、后两阶段拿到首阶段结果」，改为
    `test_each_stage_call_is_audited_with_its_own_arguments`，断言 3 条审计记录、
    各阶段 output 与自身 input 对应（84 / 96 / 108）；
    `test_pipeline_runs_three_stages_in_order_with_real_tools` 的 `len(rows) == 1`
    同步改为 3。
  - 验证：`uv run pytest -q` → **618 passed / 0 failed / 7 skipped，14.15s**；
    `tests/unit/test_pipeline_tools.py` + `test_workflow_pipeline.py` + `test_tool_audit.py`
    → 46 passed；`tests/e2e/test_pipeline_e2e.py` → 6 passed。
  - **变更影响**：修复改变了调用 ID 的取值，因此升级前已落库的审计行不会与新 ID 命中缓存——
    对升级瞬间正在重放的活动，该工具调用会**多执行一次**（审计新增一行，不覆盖旧行）。
    本项目工作流时长以秒计，实际影响可忽略；如需跨版本重放严格幂等，需保留旧派生键的兼容分支。
- 2026-09-20（F-06 会话记忆接线，直接落在 `master`）：会话记忆从「实现已就绪、接线未做」
  变为真实链路的一部分，决策见 ADR-019。
  - **写点**：`POST /api/v1/sessions/{id}/messages` 受理成功后写用户消息
    （`app/api/main.py::send_message`）；`finalize_activity` 写完 assistant 报告后写同一正文
    （`id` 用 `report_message_id(workflow_id)`，与 PostgreSQL 行同 ID）。只写进入用户视野的
    消息，阶段中间结论与工具观察不写（重放会重建，写进去只会污染上下文）。
  - **读点**：`advance_pipeline_stage` 按 `session_id` 读最近 10 条
    （`CONVERSATION_CONTEXT_LIMIT`），剔除 `agent_run_id` 等于本轮的消息，按时间正序渲染成
    `【会话历史（最近 N 条，供多轮上下文继承）】` 段落放在用户输入最前面
    （`app/orchestration/pipeline_graph.py::_conversation_block`）；没有历史时提示词逐字不变。
  - **运行期与降级**：新增 `app/memory/runtime.py`（`conversation_memory()` /
    `set_conversation_memory_factory()`，与 `set_redis_factory`、`set_tool_registry_factory`
    同一模式）；Redis 不可用时实现层降级为「空 / 无操作」，记忆缺失不让协作失败。
  - **先红后绿**：新增 4 条用例后先确认失败（阶段提示词无历史、终态未写记忆、受理未写记忆、
    多轮 E2E 第二轮无上一轮上下文），再接线；另加 1 条「无历史时提示词逐字不变」的守护用例。
  - **测试基线**：三个 conftest 都注入会话记忆内存替身，用例不连真实 Redis、不污染开发环境。
  - 验证：`uv run pytest -q` → **623 passed / 0 failed / 7 skipped，15.40s**
    （630 例，含新增 U-13 ×3 与 E-07 ×1 以及守护用例）。
  - **未接线部分**：长期记忆 `agent:{id}:memory` 仍无调用方——它需要显式的「记住这个」
    交互或独立抽取流程，届时另开 ADR；向量检索按设计文档仍为可选项。
- 2026-09-20（文档口径收口：Dapr Agents 使用边界 + 前端样式偏差）：
  - **ADR-020（Dapr Agents 1.0.6 使用边界）**：核对后确认生产链路用的是
    `dapr.ext.workflow`（Workflow/活动/子 Workflow/重放）与 `dapr.clients`（State Store），
    `rg dapr_agents app tests` 命中 0 处。决策：保留依赖与 OTel 1.39.1 约束，
    不引入 `DurableAgent`/`DaprChatClient`/`dapr_agents.memory`/`AgentTool` 等高层抽象
    （逐项与 ADR-001/007/005/009/012/014 的既有决策重叠）。
    **ADR-004 要求的「最小可运行 POC」仍未做**，并已核实可行性边界：`DurableAgent(...)`
    构造即连 sidecar（gRPC 50001，无 sidecar 立刻 `UNAVAILABLE`），POC 需一次独立
    `dapr run`（CLI 1.18.2 可用）；做与不做属方向与验收范围，待人类决定。
  - **ADR-021（前端样式偏差）**：前端实际为 React + Vite + 手写 CSS
    （`styles.css` 4249 行等 5 个文件，合计约 6800 行），`package.json` 无 tailwind 依赖；
    决策：本期不迁移 Tailwind（纯样式重写，风险高于收益），门禁维持类型检查 + 构建 +
    jsdom 渲染冒烟 + 人工核对。`AGENTS.md` 技术栈一栏已改为与实现一致并指向 ADR-021；
    `doc/15 ...平台.md` 的建议方案措辞属事实源，改动需人类同意，本次未动。
  - 本轮为纯文档改动，未触碰代码，测试基线不变（623 passed / 7 skipped）。
- 2026-09-20（Dapr Agents 最小可运行 POC 完成，关闭 ADR-004 的遗留要求）：
  新增 `scripts/poc_dapr_agents.py`（`EchoAgentExecutor`，不需要模型凭据），
  在本地 Dapr 运行时（CLI 1.18.2 + `dapr init` 默认组件）上跑通一个 `DurableAgent` 工作流。
  - 实测：`dapr run --app-id macp-agents-poc --dapr-http-port 3510 --dapr-grpc-port 50001 --
    uv run python scripts/poc_dapr_agents.py` → 工作流 `dapr.agents.MacpPocAgent.workflow`
    实例 `8d74349217fc44998aba0f4c1e9a6e9e` 终态 **COMPLETED**、输出 `echo: …`、退出码 0；
    第二个进程用 `--inspect` 读回同一实例的终态与输出（状态跨进程持久），状态存储里可见
    `macp-agents-poc||dapr.internal.default.macp-agents-poc.workflow||<instance>||history-*`
    与 `metadata` 键。
  - **发现的 1.0.6 上游不一致**：executor 分支传裸绑定方法给 `ctx.call_activity`
    （`agents/durable.py:675-679`），而活动注册与 LLM 分支都用 agent 前缀名
    （`dapr.agents.<agent>.<method>`），于是报 `Activity function named 'run_executor'
    was not registered`；POC 用显式注册未加前缀别名绕过。已写入 ADR-020 供反馈上游。
  - 组件用 `dapr init` 的默认目录（指向 `dapr_redis` 6379、`actorStateStore=true`），
    **不要求 compose 栈在跑**；仓库里 `deploy/dapr/components-local/` 指向 compose 的 6380，
    栈未起时 sidecar 会因 pubsub 组件初始化失败而退出（本轮实测）。
  - 本轮未改生产代码，测试基线不变（623 passed / 7 skipped）。
- 2026-09-20（事实源同步，经人类确认后修改 `doc/15`）：设计事实源里两处与实现不一致的
  措辞按 ADR-020 / ADR-021 同步——建议方案核心框架一栏补「依赖与 OpenTelemetry 版本约束；
  持久化执行由 Dapr Workflows 承载，使用边界见 ADR-020」，前端一栏改为
  「React + Vite（样式为手写 CSS，见 ADR-021）」；架构图里 `Dapr Agents / Agent生命周期管理`
  节点改为 `Dapr Workflow 运行时（dapr.ext.workflow）`；模块2 末尾补落地口径与 POC 指针。
  ADR-020/ADR-021 中「doc/15 本次未改」的表述同步更新。
- 2026-09-20（真实 API 提供方下的验收与性能重测，compose 全栈在跑）：
  - **live 验收**：`MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest
    tests/e2e/test_live_e2e.py -q -s` → **7 passed，190.37s**
    （E-05 健康与目录、E-01/E-02 三步流水线、I-06 工具调用读取、E-04 API 侧续跑、E-05/E-04 Web 侧）。
  - **并发（取代 §D9-10 的 Ollama 数据）**：`scripts/perf_concurrency.py --sessions 10
    --concurrency 10` → 10/10 完成、成功率 1.0、总耗时 75.836s；受理延迟 p50 0.348s；
    端到端 p50 **54.628s** / p95 74.571s；Token 合计 432596（4241.1/次，102 次调用）；
    工具调用成功率 **0.6798（138/203）**；模型分布 `deepseek-flash`。
  - **恢复（E-03，取代 poc 假模型数据）**：`scripts/measure_recovery.py` → 重启 3.07s、
    **恢复 2.48s（目标 <5s）**、回填等待 0.00s、业务 `completed` 三步齐全且
    **Dapr 终态 COMPLETED**——F-05 修复后两个终态首次同时成功。
  - **F-01/F-06 真实链路定向验证**（live 会话 `33c0700b-…`）：第一轮两阶段各调 calculator
    → 审计 **4 条**独立记录（`21*2`×2、`21+21`×2，均 succeeded），报告 1962 字含 `42`；
    第二轮同会话追问「21*2 是多少」→ 回答引用上一轮两个计算结果（`42`），
    证明会话记忆写入与阶段提示词注入在真实模型下生效。
  - **新发现 F-07（同日已处置）**：先按正则统计得「18 份中 10 份为空」，后发现该口径会把
    嵌套的上游字段算进去；按「键对应阶段自身的 content」精确统计为 **63 份中 14 份（22%）**。
    真因是模型**撞上工具轮次上限（4）后仍在请求工具**（collect/analyze 常有 7–9 次工具调用、
    `pending_tool_calls=2..4`），阶段无文字产出而 workflow 仍 `completed`，下游拿到空上游内容、
    报告退化成「输入缺失」说明。已排除 F-01/F-06 改动（live 用新会话、无历史，回归网全绿）。
  - **F-07 处置（方案 2，人类确认）**：`_ensure_text_response` 在 `content` 无文字时补一次
    「请用文字给出本阶段结论，不要再调用工具」的重试，仍为空则落 WARNING 后继续；
    新增 `stage.tool_iteration_limit` 告警让「撞上限」可观测；判空只认 `content`
    （首版把「还有待处理 tool_calls」当有产出，实测仍 8/12 为空，修正后生效）。
    验证：`uv run pytest -q` → **627 passed / 7 skipped**；处置后 live 复测
    **7 passed / 343.69s**，新增 4 个工作流 12 份阶段载荷 **0 份为空**，
    日志 7 × `stage.tool_iteration_limit` + 5 × `stage.empty_content action=retry`、
    0 × `continue_with_empty`（补提示 5/5 救回）。代价：live 套件 168.87s → 343.69s。
    实现与判空坑见 ADR-009 修订；数据见 ADR-016 修订与 `doc/testing.md` §4.6。
- 2026-09-20（工具失败重试 3 次、之后按实际结果输出，人类要求）：`ToolCaller.invoke`
  改为「首次 + 最多 3 次重试（`TOOL_CALL_RETRY_LIMIT = 3`，最多 4 次尝试）」，
  **全程沿用同一 `call_id`**（审计层把 failed 行重置为 running，U-07 契约），
  因此一次逻辑调用在 `tool_calls` 表里仍只占一行；每次尝试与重试落
  `event=tool.call … attempt=N max_attempts=4`、`event=tool.retry … next_attempt=N+1`。
  耗尽重试后把失败观察交给模型，仍无文字则由 F-07 兜底补提示——输出基于真实结果、不中断流水线。
  - 先红后绿：新增 2 条单元用例（失败两次后成功采用真实输出；持续失败尝试 4 次后记 failed
    且阶段仍输出），修复前实测只尝试 1 次。
  - 验证：`uv run pytest -q` → **629 passed / 7 skipped**；真实模型 live →
    **7 passed / 586.22s**（重试前 343.69s），81 次 `tool.retry`、出现
    `attempt=4 max_attempts=4` 终态失败，`tool_calls` 表 27 failed / 61 succeeded 且
    每组 `rows == ids`。
  - **新发现（未处置）**：① 确定性错误被无谓重试——`calculator` 非法参数、
    `sql_query` 无效 SQL 重试 3 次不会成功，只增加延迟（本轮多花约 4 分钟），
    建议后续按错误类型分类重试；② **F-08**：compose 的 backend 未挂载
    `/var/run/docker.sock`，`code_execution` 在容器里必然报「沙箱不可用」（本轮 10 次全失败），
    宿主机直跑可用，修法属部署决策。
  - 口径：`doc/testing.md` §4.2 的两行旧数据已标注「以 §4.6 为准」，
    ADR-016 增加 2026-09-20 修订记录本轮命令与数字。
- 注意事项：数据库读取用例的设计口径是 SQLite 内存表与注入目录数据；**集成层已由
  `tests/integration/conftest.py` 完成隔离（2026-09-20），见上一条与 `doc/testing.md`
  §4.5**——三层 conftest 现在都把 DSN 钉成内存 SQLite，并用 autouse fixture 替换
  Provider 配置的 Redis 镜像，全量套件不再隐含依赖可连的 PostgreSQL 与 Redis
  （Redis 不可达时 `uv run pytest -q` 15.15s，见上一条）。
  MCP 用例既有内存协议往返（`tests/unit/test_mcp_tools.py`），
  也有真实跨进程 stdio（`tests/e2e/test_mcp_stdio_e2e.py`，2026-09-16 补）；两者都不等于
  真实 PostgreSQL 与浏览器端到端验收。E-04/E-05 的 Web 侧与部署脚本已补上实跑证据
  （见上文两条），**依赖模型的链路已于 2026-09-15 用 `gpt-5.5` 补跑通过**（同见上两条），
  **浏览器端渲染仍是人工核对项**（前端门禁为类型检查 + 构建，未引入浏览器自动化）。

### 范围说明

- D3-D4 不含动态并行分派、依赖 DAG 与人工介入（HITL），后续版本单独评估。

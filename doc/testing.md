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

> **`uv run pytest` 需要可达的 PostgreSQL 与 Redis**（本机 compose 的 `postgres` 与
> `redis` 即可，见 `doc/deployment.md`）。这两者不可达时，用例**不会快速失败**，而是
> 长时间阻塞在连接重试上——曾出现 30 分钟仍无结果、起栈后 14.51s 跑完（成员 D D9-10
> 实测）。跑全量前先确认 `localhost:5433` 与 `localhost:6380` 可连。

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
| I-10 | Provider 配置读写 | `GET/PUT /api/v1/config/provider`：200 生效值与回退、422 校验、503 写失败、裸请求可写（不鉴权，ADR-015）；覆盖值落 `provider_configs` 并镜像 Redis，响应与日志不含密钥（ADR-014） | M4 |

### 2.3 端到端测试（E）

| 编号 | 用例 | 步骤 | 通过标准 | 里程碑 |
| --- | --- | --- | --- | --- |
| E-01 | 单 Agent 问答 | 创建会话 → 发消息 → 轮询/取结果 | 返回结构化回答 | M5 |
| E-02 | 多 Agent 协作 | 提交三步任务 → 观察状态流转 | 三步依次完成并生成报告 | M5 |
| E-03 | 故障恢复 | 执行中断掉 backend → 重启 | 任务从断点续跑成功，无状态丢失 | M3 起可演练，M5 验收 |
| E-04 | Web 会话管理 | UI 创建会话、发消息、暂停/恢复 | UI 与 API 状态一致 | M5 |
| E-05 | 一键部署 | `start.ps1` → 健康检查 → `stop.ps1` | 全部服务健康 | M5 |

E-04/E-05 目前的自动化程度（成员 D D9-10）：会话生命周期（创建 → 暂停 → 暂停期提交被拒
→ 恢复 → 回读）与「前端容器 + nginx `/api` 反代」走 `tests/e2e/test_live_e2e.py` 的
Web 侧用例，部署脚本按 `deploy/start.ps1` / `stop.ps1` 实跑并以退出码与三段健康检查判定。
浏览器里的**渲染效果**仍需人工按 `doc/deployment.md`「演示与验收」的核对清单确认；
发消息后的三步流水线需要可用的模型凭据（提供方前提见 §4.2）。

## 3. 关键链路测试设计

### 3.1 Workflow 恢复测试（I-03 / E-03）

最小可复现方案：

1. 启动流水线到固定阶段（如执行 collector 后）；
2. 记录 Dapr Workflow `instance_id`；
3. 杀掉 backend 进程/容器；
4. 重启 backend；
5. 轮询 `GET /workflows/{id}`，断言从 `analysis` 阶段继续并最终 `completed`；
6. 记录从服务可用到实例恢复执行的耗时，目标 `< 5s`。

自动化路线：已提供 `scripts/fault_recovery.ps1` 固化手工演练步骤；

现状（2026-09-15，成员 C D9-10）：已升级为**可重复的脚本化测量**——
`uv run python scripts/measure_recovery.py` 按上述步骤自动执行并输出 JSON
（不可用时长、服务可用→实例恢复执行的耗时、扣除定时器等待后的值、业务状态是否保留、
Dapr orchestration 终态）。实测数据见 §4.2；`tests/unit/test_workflow_pipeline.py`
仍覆盖活动重放、子 Workflow 实例 ID 稳定、终态回写等**单元级**恢复语义，
被杀的进程改由 pytest 用例控制（需容器控制权）仍未落地。

注意：该演练路径曾有缺口——`app/workflows/poc.py` 用 `session_id="demo-session"`
触发 `finalize_activity` 的报告消息写入抛 `badly formed hexadecimal UUID string`，
业务行是 `completed` 而 Dapr orchestration 是 `FAILED`（缺口 F-05，见 ADR-016）。
**F-05 已由成员 B 修复**（`session_id` 不再硬编码，且 CLI 对运行时终态非 `COMPLETED`
即非零码退出）；`scripts/measure_recovery.py` 的日志侧终态校验**保留为守卫**
（不因对方修好而撤掉）。§4.2 的 E-03 数据取自修复前，且走 poc 的确定性（假模型）路径，
修复后的演练需重跑一遍确认两个终态同时为成功。

### 3.2 并发会话测试（E 系列性能）

- 使用 `locust` 或 `hey` 模拟 10 个并发会话；
- 断言：全部会话完成、无 5xx 比例超过阈值、平均延迟记录在报告；
- 指标从 Prometheus 导出 Token 消耗与工具调用成功率。

实现（成员 C D9-10）：未引入 `locust`/`hey`，用 `scripts/perf_concurrency.py`
（标准库线程池 + `httpx`）实现同等测量，避免新增依赖：

```bash
uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10 --json perf.json
```

- 每个虚拟会话：创建会话 → 发消息（记录 202 受理延迟）→ 轮询 Workflow 到终态
  （记录端到端延迟），汇总成功率、终态分布、HTTP 错误数、延迟 p50/p95/max；
- Token 消耗与工具调用成功率：事实源要求从 Prometheus 导出，但**测量当时** backend 未
  暴露 Prometheus 文本端点（D 侧）且 `metrics` 表未建（B 侧），因此改从**行为日志**采样
  （`event=llm.finish` 的 `input_tokens`/`output_tokens`/`total_tokens`/`duration_ms`、
  `event=tool.call` 的 `status`）——数值是真实测量值，通道与事实源的差异在本文件与
  ADR-016 中显式标注；工具调用 0 次时成功率输出 `null`（不谎报 0%）。
  这两条通道随后都已由 B/D 落地（F-04），**事实源通道现已可用**，
  可另取一轮从 `/metrics` 或 `metrics` 表取数并与此处数据对比。
- 模型名：`event=llm.finish` 的 `model` 字段在 F-03 修复后为真实模型名
  （修复前是集成类名或缺失），因此按模型归因 Token 效率具备前提。
- 失败率超过 `--max-failure-rate`（默认 0）时脚本以非零码退出。
- 脚本纯逻辑（百分位口径、日志解析、报告汇总）由 `tests/unit/test_perf_tooling.py` 固定。

### 3.3 前端验证现状（成员 D）

前端当前**没有测试框架**，自动化门禁是类型检查 + 构建：`npm --prefix frontend run build`
（`tsc --noEmit && vite build`）。因此前端改动按「构建通过 + 真实后端冒烟」两步验证，
不把构建通过当作功能验收。

Provider 配置面板（`frontend/src/Inspection.tsx::ProviderConfigPanel`）的验证步骤：

1. 起后端（真实 PostgreSQL + Redis）与 `npm run dev`；
2. 「工具与配置」页应显示生效的 provider/model/地址/温度与凭据状态；
3. 填入非法取值（例如 Temperature 填 3）提交 → 页面给出取值错误，配置不变；
4. 改模型或地址后提交 → 提示已保存，页面回读生效值，响应与页面都不出现密钥；
5. 点「清除覆盖并回退环境配置」→ 页面回到环境配置值。

浏览器端的自动化用例（Playwright 之类）尚未引入，属后续增量。

## 4. 里程碑验收清单

| 里程碑 | 必须通过的用例 |
| --- | --- |
| M1（单 Agent） | U-01、U-02、U-03 |
| M2（Dapr 持久化） | I-01、I-02、I-03 |
| M3（多 Agent） | U-04、U-05、I-04、I-05、E-03 手工版 |
| M4（工具 + 可观测） | U-06、U-07、U-08、U-09、U-10、I-06、I-07、I-08、I-09、I-10 |
| M5（交付） | E-01 至 E-05 全部 |

### 4.1 M4 当前状态（2026-09-15）

成员 C 的 D7-8（内置工具/沙箱/可观测接入）落地后的实测状态。
**M4 已完成（2026-09-15 验收通过）**（口径与 `doc/roadmap.md` D7-D8 一致）：
真实 Dapr Workflow + OpenAI 兼容 API（`deepseek-flash`）产出了工具调用并落审计表，
详见文末验证记录与 `doc/roadmap.md`。
**注意判定标准**：M4 的完成依据是「真实模型路径下的验收证据」，而不是用例数量——
测试全绿曾是 M4 未闭环时的状态，因此不能用测试结果替代验收。

| 用例 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| U-06 工具函数独立行为 | 通过 | `tests/unit/test_builtin_tools.py`：计算器返回值与拒绝面、只读 SQL 校验与真实只读执行、注册表发现/调用、`web_search` 注入 fetcher 的离线解析（ADR-012） |
| U-07 工具调用幂等键 | 通过 | `tests/unit/test_tool_audit.py`（ADR-011） |
| U-08 沙箱边界拒绝越权 | 通过 | `tests/unit/test_sandbox_policy.py`：Python/Shell 越权拒绝、策略先于后端、`denied` 后端不降级执行（ADR-012） |
| U-09 流水线接入 MCP 工具 | 通过（含真实模型验收） | `tests/unit/test_pipeline_tools.py`（15 例全绿，含「阶段活动消费默认注册表」，ADR-009）。真实路径已验收：API 模型下 collector/analyst 两阶段各产生一次 `calculator` 调用（`21*2`、`21+21`，`succeeded`） |
| U-10 行为日志事件 | 通过 | `tests/unit/test_observability.py`（ADR-010） |
| I-06 MCP 工具发现与调用 | 通过（缺跨进程 stdio） | `tests/unit/test_mcp_tools.py` 走 `mcp.shared.memory` 的**真实 MCP 协议往返**（发现、调用、错误还原、目录）；真实流水线验收（2026-09-15）：Workflow 内 2 次 `calculator` 调用落 `tool_calls` 并读回（`21*2`、`21+21` → `42`）；仍缺跨进程 stdio 传输的端到端用例 |
| I-07 可观测数据输出 | 通过 | `tests/unit/test_observability_metrics.py`：Span 与属性/异常、指标去重、Prometheus 文本、`metrics` 表写入（SQLite 与表缺失两种路径）、降级不阻塞；`metrics` 表已由 `app/core/checkpoint.py::MetricRecord` 建出，2026-09-15 在真实 PostgreSQL 上跑通采样落库与 `/api/v1/metrics` 读回（16 条采样），真实 Prometheus 上抓到 `backend`/`dapr-sidecar` 两个 `up` target（43 条 `macp_*` 序列），真实 Jaeger 上查到 `stage.run`/`llm.chat` span；Token 按真实模型名归因（F-03 回归，见 §4.2） |
| I-08 配置热更新 | 通过 | `PATCH /api/v1/config/agents/{agent_id}` 已实现（`doc/api.md` §5.7、ADR-013）：覆盖写 `agent_configs`，阶段活动执行时解析生效配置，无需重启；单元用例 `tests/unit/test_agent_config.py`（合并/校验/回退/表契约），集成用例 `tests/integration/test_config_api.py`（404/422/503/200 与生效值，裸请求可写见 ADR-015）；真实 PostgreSQL 上已跑通写入—读回—新执行生效 |
| I-09 只读巡检接口 | 通过 | `tests/integration/test_inspection_api.py`；覆盖分页、`availability` 区分「未接入」与「零条记录」、默认工具目录接线（`/tools` 返回 4 个注册工具）与 Prometheus 文本端点（`/metrics`）；用 SQLite 内存表与注入目录数据，不等于真实 PostgreSQL/MCP 验收 |
| I-10 Provider 配置读写 | 通过（含真实环境冒烟） | 单元与集成用例：`tests/unit/test_provider_config.py`（26 例：表契约、合并顺序、Redis 命中/回源/回填、镜像失败降级、密钥脱敏、字段校验）与 `tests/integration/test_provider_config_api.py`（11 例：422/503/200、显式 null 清除、provider 切换、裸请求可写）。2026-09-15 真实环境冒烟（本机 PostgreSQL 5433 + Redis 6380 + uvicorn）：`PUT` 不带任何令牌 → `200` 且响应不含密钥；`GET` 回读生效值；`/providers` 反映新值；删除 `provider:config` 后 `GET` 仍返回存储值并自动回填缓存。测试数据已清理 |

2026-09-15（M4 收口后）：`uv run pytest -q` → **294 passed / 1 failed**，
失败项与上面同一处，仍属成员 A 的过期前置条件；新增
`tests/unit/test_metrics_table_schema.py` 校验 `metrics` 表 DDL 与索引契约。

2026-09-15（I-08 落地后）：`uv run pytest -q` → **322 passed / 1 failed**，
失败项仍是上面同一处。新增 `tests/unit/test_agent_config.py`（13 例：表契约、
合并回退、provider 字段映射、读取失败回退、校验与审计日志）与
`tests/integration/test_config_api.py`（15 例：403 fail-closed、200 生效值、
局部更新、显式 null 清除、404、422、503）。

2026-09-15（A 修复过期前置条件后）：`uv run pytest -q` → **323 passed / 0 failed**。
上面唯一失败项已按本文档约定改为 `set_tool_registry_factory(lambda: None)` 显式构造
「没有注册表」的前置条件，用例意图与两条断言未变；未跳过或删除任何用例。
M4 未闭环的剩余缺口只剩真实流水线的工具调用行为（模型把调用当文本输出，
见 `doc/roadmap.md`），不在测试层面；因此测试全绿不构成 M4 验收通过。

2026-09-15（API 优先接入落地后）：`uv run pytest -q` → **366 passed / 0 failed**。
新增 I-10（Provider 配置读写）的单元与集成用例；默认提供方改为 OpenAI 兼容 API
（ADR-014），覆盖值落 `provider_configs` 并镜像 Redis。剩余缺口不变：**尚未在真实
API 模型下跑通工具调用**，因此 M4 仍是「代码完成、验收未闭环」。

2026-09-15（取消写入令牌后，ADR-015）：`uv run pytest -q` → **359 passed / 0 failed**。
删除 7 个令牌相关用例（`test_config_api.py` 4 例、`test_provider_config_api.py` 4 例，
其中两例为参数化），改为各留 1 例「裸请求即可写入」；`AdminSettings` 与
`get_admin_settings()` 一并移除，因此用例总数下降属于预期，不是跳过或删除有效断言。
`npm --prefix frontend run build` 通过（前端同步删除令牌输入框与 403 分支）。

2026-09-15（M4 真实验收，缺口二关闭）：compose 容器栈 + `deepseek-flash` 真实运行。
提交「用 calculator 计算 21*2」后 Workflow `completed`（collect → analyze → report），
`GET /workflows/{id}/tool-calls` 返回 **2 条** `calculator` 记录（`21*2`、`21+21`，
均 `succeeded`，输出 `{"value": 42}`），报告正文引用 `42`；
`GET /metrics?workflow_id=...` 返回 **20 条**采样，含各阶段 Token
（943/77/1020、1216/128/1344、1858/1193/3051）与 `tool_calls` 指标。
同一任务在 `deepseek-v4-pro` 上也产生 1 条成功调用，可作对照。
据此 M4 由「代码完成、验收未闭环」转为**已完成**；U-09/I-06 的剩余缺口只剩
跨进程 stdio 传输与浏览器端自动化，不影响 M4 结论。

### 4.2 E 系列当前状态（2026-09-15，成员 C D9-10）

三层证据与完整数据见 ADR-016；命令（真实环境验收与并发测量需先配好提供方凭据，
默认提供方已是 OpenAI 兼容 API，缺凭据 fail-fast，见 ADR-014）：

```bash
uv run pytest tests/e2e -q                          # 无容器回归网（6 passed）
MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s   # 真实 compose 验收
uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10
uv run python scripts/measure_recovery.py --hold-seconds 15 --restart-lead-seconds 1
```

**提供方前提**：下表除 E-01/E-02 另有注明外，数据都测于本轮 D9-10 落地时的
Ollama `qwen2.5-coder:7b`（E-03 走 poc 的确定性假模型路径）；此后默认提供方改为
OpenAI 兼容 API，表中「未达成」的结论已在 API 模型下复测通过（见上一条记录），
但**并发与恢复两组性能数据尚未在 API 模型下重取**。

| 用例 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| E-01 单 Agent 问答 | 通过（结论分提供方） | 真实环境跑通：会话→消息→轮询→`completed`，报告消息落库。答复内容在 Ollama `qwen2.5-coder:7b` 下**未达成**——报告正文是 `{"name": "web_search", ...}` 这样的工具调用 JSON 文本（缺口 F-02）；换 OpenAI 兼容 API（`deepseek-flash`）后达成：报告 1877 字并引用工具返回值 `42`（见 §4.1 与 `doc/roadmap.md` 验证记录） |
| E-02 多 Agent 协作 | 通过（结论分提供方） | 真实环境三步依次完成（`checkpoint.completed_steps=[collect, analyze, report]`，2-4s/条）；同受 F-02 影响（Ollama 下 `tool_calls=0`，工具未真正执行），API 模型下 collector/analyst 各产生一次 `calculator` 调用 |
| E-03 故障恢复 | 通过（旧数据有缺口） | `scripts/measure_recovery.py`：不可用 2.62s、**恢复耗时 ≤0.2s（目标 <5s）**、业务状态保留 `completed`/三步齐全；该次演练的 Dapr 终态为 FAILED（缺口 F-05，**已由 B 修复**），修复后的演练需重跑以确认两个终态同时成功。数据取自 poc 的确定性（假模型）路径 |
| E-04 Web 会话管理 | 部分（Web 侧数据路径已验，渲染未验） | API 侧通过：暂停 → 新消息被 409 `SESSION_PAUSED` 拒绝 → 恢复 → 原 Workflow 续跑 `completed`。Web 侧由成员 D 补自动化（§4.3）：经 nginx 反代跑完「新建任务 → 暂停 → 暂停期提交被拒 → 恢复 → 回读」六步；浏览器**渲染效果**仍需人工按 `doc/deployment.md` 的核对清单确认，发消息后的三步流水线需要模型凭据 |
| E-05 一键部署 | 通过（2026-09-15，成员 D 实跑） | `start.ps1` 退出码 `0` 且 Frontend / Backend / Dapr Sidecar 三段健康检查全部打印 `is healthy`，随后打印 7 行访问地址；`stop.ps1` 退出码 `0` 且 `docker ps -a` 中项目容器全部移除。过程中修复了两个脚本在 Windows PowerShell 5.1 下被 `docker compose` 的 stderr 中断的缺陷（见 §4.3）。容器侧 `/metrics`、`/tools` 与 Prometheus/Jaeger 已由 B/D 验收（`doc/roadmap.md`「M4 代码落地情况」） |
| 并发会话（§3.2） | 通过 | 10 会话/并发度 10：成功率 1.0、HTTP 5xx 0、受理延迟 p50 0.46s、端到端 p50 16.54s / p95 18.61s、Token 合计 18650（621.7/次调用）、工具调用成功率**无样本**（0 次；Ollama 下模型未发起工具调用，F-02 之外的模型行为差异，API 模型下重测后应不再为 0） |

合入 `master` 后（2026-09-15）：`uv run pytest -q` → **371 passed / 0 failed / 5 skipped**
（5 skipped 为需要 compose 的 `test_live_e2e.py`；比先前 378 例少，是 master
删掉 7 个令牌用例所致，不是跳过或删除有效断言）。

缺口状态（详见 ADR-016 F-01～F-06）：
F-01 跨阶段同工具调用被审计主键合并且返回首个结果（A+B，**仍未清**，影响工具链路正确性）；
F-02 真实模型不产出结构化 `tool_calls`（**已关闭**：该现象实测于 Ollama
`qwen2.5-coder:7b`，默认提供方改为 OpenAI 兼容 API 后已复测通过——
`deepseek-flash` 下两阶段各产生一次 `calculator` 调用，报告引用返回值 `42`）；
F-03 Token 采样缺 `model` 标签（C 侧，**已修复**：改为从回调 `metadata["ls_model_name"]`
取真实模型名并在 `run_id` 上传递，回归用例见 `test_observability_metrics.py`）；
F-04 `metrics` 表（B）、`/tools` 接线与 Prometheus 文本端点（D）、A 的失败用例与 I-08
——**三项均已落地**（测试全绿、真实库与容器侧均已验收）；
F-05 poc/故障演练路径业务终态与 Dapr 终态不一致（B，**已修复**：`session_id` 不再硬编码，
CLI 对运行时终态非 `COMPLETED` 即非零码退出）；
F-06 会话/长期记忆未接入编排（`app/memory/` 只有 Protocol，历史消息既不落记忆也不回注
Prompt，仅 `GET /messages` 读取）——**本轮只记录，未处置**。

### 4.3 Web UI 与部署编排（2026-09-15，成员 D D9-10）

`分工.md` §3 里 D 的 D9-10 是「UI 收尾、录制演示、部署文档」，对应 §4.2 表里 E-04/E-05
的 Web 侧与部署脚本两行。本轮落地情况：

| 分工 | 状态 | 落地内容 |
| --- | --- | --- |
| Web 控制台 | 代码未改动，验收补齐 | master 的工作台已覆盖团队状态、任务记录、调用链路与 Token 采样、会话管理；本轮没有改前端代码，补的是验收（见下两行的自动化与核对清单） |
| `start.ps1` / `stop.ps1` 全流程 | 已完成 | 修复两个脚本在 Windows PowerShell 5.1 下被 `docker compose` 的 stderr（构建/停止进度）中断的缺陷：`$ErrorActionPreference = "Stop"` 会把原生命令的 stderr 当成终止性错误，`start.ps1` 因此不做健康检查、不打印地址就退出（容器其实已经起来），`stop.ps1` 对已完成的停止报错并返回非零码。两处都改为在该调用期间临时切到 `Continue` 并以 `$LASTEXITCODE` 判定成败，与文件里 `docker info` / `compose version` / `compose config` 的既有写法一致 |
| 部署文档 | 已完成 | `doc/deployment.md` 补「演示与验收」：E-05 的通过标准（退出码 + 三段健康检查 + stop 后容器为空）、E-04 的自动化命令与浏览器核对清单；并修正模型前置条件——默认提供方已是 OpenAI 兼容 API（ADR-014），原文「宿主机需要运行 Ollama」已过时 |

验证（本机 compose，2026-09-15）：

```bash
uv run pytest -q -p no:cacheprovider
# 359 passed / 0 failed，14.51s（前置：PostgreSQL 5433 + Redis 6380 可达）

MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s \
  -k "frontend or web_ui_session or health"
# 3 passed（E-05 健康与目录、E-05 Web 侧、E-04 Web 侧）

npm --prefix frontend run build
# 通过：tsc --noEmit && vite build，3134 modules，产物 index-*.js 251.80 kB
```

`deploy/start.ps1` → `deploy/stop.ps1` 实跑：两次都得到 `SCRIPT_OK=True / LASTEXITCODE=0`，
中间打印 `Frontend is healthy.` / `Backend is healthy.` / `Dapr Sidecar is healthy.` 与
7 行访问地址；`stop.ps1` 打印 `Services stopped.` 后
`docker ps -a --filter "name=multi-agent-collaboration-platform"` 为空。

**未完成 / 未验**（不隐瞒）：

1. `tests/e2e/test_live_e2e.py` 中依赖模型的 4 条（E-01/E-02 两条、I-06、
   E-04 API 侧续跑）本轮**未跑**：本机没有可用的模型凭据。命令与预期不变，
   配好凭据后可直接重跑。
2. 浏览器渲染，以及「发消息 → 观察 Agent 执行台推进 → 展开协作详情」仍是人工步骤：
   本轮只给核对清单，没有引入浏览器自动化（前端门禁是类型检查 + 构建，见 §3.3）。
3. 「任务记录」页只显示当前会话最近一次执行（`App.tsx::History`，页面已标注
   「完整历史查询尚未接入」）。做完整历史需要新增 `GET /api/v1/workflows` 一类的列表
   接口，属新增能力，本轮未做；因此**不据此宣称** `分工.md` §7 的「Web UI 可展示任务历史」
   已闭环。

## 5. 失败处理约定

- 任一用例失败：先复现，再定位，修复后将失败模式固化为新的测试或本文档约束；
- 对 Dapr/编排等共享行为，先写测试或同步补测试，不允许“看起来正确”代替；
- 每次里程碑结束时在报告记录：运行命令、通过数/失败数、失败原因。

# ADR-016: 端到端验收与性能基线（D9-10）

状态：已接受

## 背景

`分工.md` §3 角色 C 的 D9-10 是「端到端测试补缺、性能数据」。在此之前：

- `tests/e2e` 目录不存在，`doc/testing.md` §2.3 的 E-01～E-05 没有任何自动化证据，
  §3.1 明确记录 E-03「仍为手工脚本」，§3.2 的并发会话测试没有实测数据；
- `doc/15 AI Native多智能体协作平台.md` §六 的三条性能测试（恢复 <5s、10 并发会话、
  Token 效率）没有可复现的测量手段；
- 容器环境已可完整启动（compose 全服务健康），具备真实验收条件。

## 决策

1. **三层证据，边界各自写明**（避免「看起来跑通了」）：
   - **无容器回归网** `tests/e2e/test_pipeline_e2e.py`（替身见 `tests/e2e/conftest.py`）：
     执行真实 API 路由、真实 Workflow/活动代码、真实三步 LangGraph 流水线、
     真实 MCP 注册表与 4 个内置工具、真实可观测采集；只替换两处进程外依赖——
     Dapr 运行时（`InlineWorkflowDriver` 直接驱动真实 Workflow 生成器，并把失败
     注回生成器内部以触发 Dapr 的重试与终态分支）与 PostgreSQL 落库
     （`InMemoryApiStore` + 内存审计表，保留 `ALLOWED_TRANSITIONS` 状态迁移校验与
     报告消息幂等）。无 Docker 也能跑，作为长期回归网。
   - **真实环境验收** `tests/e2e/test_live_e2e.py`：跑在完整 compose 上，默认 skip，
     `MACP_E2E_LIVE=1` 启用；覆盖 E-05（健康与目录）、E-01/E-02（三步流水线 + 报告）、
     E-04 API 侧（暂停 409 → 恢复续跑）、I-06 真实库可读（`tool_calls` 表）。
   - **性能脚本** `scripts/perf_concurrency.py`（10 并发会话、延迟分位、Token/工具
     成功率）与 `scripts/measure_recovery.py`（E-03 恢复耗时），均输出 JSON 并带
     非零退出码，可重复执行。
2. **不新增 pytest marker 或配置**：真实环境用例用环境变量 skip，
   保证 `uv run pytest` 在无容器环境下仍是全绿的可回归用例集
   （`-m "not integration"` 这类约定在 `pyproject.toml` 里并无对应注册，不引入）。
3. **数据采集通道要如实标注**：事实源要求指标从 Prometheus 导出，但测量时 backend 未暴露
   Prometheus 文本端点、`metrics` 表也未建（两项随后由成员 D 与 B 落地，见 F-04），因此
   性能脚本改用**行为日志**采样（`event=llm.finish` 的 Token/耗时、`event=tool.call` 的成败），
   数值同样是真实测量值，只是通道不同；指标口径沿用 `doc/api.md` §5.5 的名称。
   工具调用为 0 次时输出 `null` 而不是 0%（不把「没有样本」谎报成「成功率 0%」）。
   事实源通道现已可用（`/metrics` 文本端点、`metrics` 表采样），后续可把同一批测量改从
   该通道取数并对比，作为通道一致性的验收项。
4. **业务终态与运行时终态都要校验**：恢复脚本除业务库终态外，还从 backend 日志取
   Dapr orchestration 终态，避免「业务行 completed」掩盖运行时 FAILED（见 F-05）。
5. **不改动其他成员代码**：本轮发现的跨模块问题一律证据化上报（F-01～F-06），
   修复归属与时机由对应成员决定。

## 实测数据（2026-09-15，本机 compose，Ollama `qwen2.5-coder:7b`）

> **提供方前提**：本轮测量时默认提供方是 Ollama（`qwen2.5-coder:7b`），E-03 的恢复演练
> 走 `app.workflows.poc` 的确定性（假模型）路径。此后默认提供方改为 OpenAI 兼容 API
> （ADR-014，缺凭据 fail-fast，Ollama 降为备用），因此真实环境验收与并发测量都要配好
> 凭据才能重跑；F-02 已在 API 模型下复测通过并关闭，并发与恢复两组数据尚未重取。

命令与结果：

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 无容器回归网 | `uv run pytest tests/e2e -q` | 6 passed |
| 真实环境验收 | `MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s` | 4 passed + 1 xfail（当时 F-02 未达成，见下） |
| 并发性能 | `uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10` | 见下 |
| 故障恢复 | `uv run python scripts/measure_recovery.py --hold-seconds 15 --restart-lead-seconds 1` | 见下 |
| 全量回归（本分支，合入 master 前） | `uv run pytest -q` | 300 passed / 1 failed / 5 skipped（1 failed 为当时 A 侧过期的前置条件用例，已在 master 修复） |
| 全量回归（第一次合入 master 后） | `uv run pytest -q` | 378 passed / 0 failed / 5 skipped |
| 全量回归（再次合入 master 后） | `uv run pytest -q` | **371 passed / 0 failed / 5 skipped** |

合入 master 后（2026-09-15，两次合并）：`origin/master` 的提交已并入本分支——
第一次含 ADR-013 配置热更新、ADR-014 API 优先接入、`metrics` 建表、工具目录与
Prometheus 文本端点接线、I-08、前端配置面板；第二次含 ADR-015 取消配置写入令牌、
M4 真实验收（工具调用在 API 模型下跑通，F-02 由此关闭）。
本 ADR 因此改号为 **016**（013 与 015 已分别被 master 的配置热更新、取消写入令牌占用）。
用例数从 378 降到 371 是 master 删掉 7 个令牌用例所致，不是跳过或删除有效断言；
5 skipped 仍是需要 compose 的 `tests/e2e/test_live_e2e.py`。

以下数据均为**合入 master 前**在本机 compose 上测得：

- **E-01/E-02**：单条流水线 2-4s（模型预热后），首次调用含加载 8-12s；
  终态 `completed`，`workflow_runs.checkpoint.completed_steps=[collect, analyze, report]`，
  报告消息落 `messages(role=assistant)`。通过标准「生成结构化报告」在 Ollama 下
  **未达成**（见 F-02），改 API 提供方后已达成（报告 1877 字并引用工具返回值 `42`）。
- **E-04（API 侧）**：暂停 → 新消息 409 `SESSION_PAUSED` → 恢复 → 原 Workflow 继续跑到
  `completed`。
- **I-06**：`GET /workflows/{id}/tool-calls` 返回 `availability=available`
  （真实 `tool_calls` 表已建），本次 Workflow 行数 0（Ollama 下未发起调用，F-02；
  API 提供方下同一接口返回 2 条 `calculator` 记录）。
- **并发（10 会话 / 并发度 10）**：成功率 1.0（10/10 `completed`），HTTP 5xx 0 次；
  受理延迟（202）p50 0.459s / p95 0.53s / max 0.53s；
  端到端 p50 16.54s / p95 18.61s / max 18.61s（mean 16.56s）；
  Token 合计 18650（输入 17762 / 输出 888），30 次模型调用，约 621.7 Token/次；
  模型调用耗时 p50 5625ms / p95 5678ms（10 路并发下 CPU 饱和，单路串行约 0.8-1s）；
  工具调用成功率：**无样本**（0 次调用）。
- **恢复（E-03）**：后端不可用 2.62s；**恢复耗时 ≤0.2s**（低于 0.2s 轮询粒度——
  服务恢复可用时实例已完成续跑），重启→业务终态 2.63s；业务状态保留
  `completed` / 三步齐全。目标 <5s 达成。
  但同一演练的 **Dapr orchestration 终态是 FAILED**（F-05），
  因此这条数据下的「恢复成功」只对业务状态成立；F-05 已由 B 修复
  （见「发现」），修复后的恢复演练需重跑一遍才能宣称两个终态同时为成功。

## 发现（只上报，未改动其他成员代码）

- **F-01 跨阶段同工具调用被审计主键合并且返回首个结果**（A + B）。
  `app/orchestration/tools.py::ToolCaller` 每个阶段重建、`index` 从 0 起算，
  `app/workflows/pipeline.py:164` 传入的 `tool_scope` 是 Workflow 级 ID，
  因此 `tool_call_id(index, tool_name, scope)` 在三个阶段完全相同；
  `app/core/tool_audit.py::execute_tool_call` 按该 ID 幂等，直接返回首个 `succeeded` 的缓存。
  实测：analyze 请求 `12*(3+5)`、report 请求 `12*(3+6)`，两者都拿到第一阶段的
  `{"expression": "12*(3+4)", "value": 84}`，三次调用只落 1 行审计。
  真实模型下这意味着「分析师/报告员拿到收集者的检索结果」。
  回归用例：`tests/e2e/test_pipeline_e2e.py::test_audit_key_collapses_distinct_calls_across_stages`。
  建议修法：`tool_scope` 带上阶段（与 `app/core/tool_audit.py` 模块文档
  「call_id 取自 workflow_id + stage + tool_name」一致）。
- **F-02 真实模型不产出结构化 `tool_calls`**（模型/提示词侧，**已关闭**）。
  真实环境日志：`event=stage.start ... tools=4`（四个工具都已绑定）但
  `event=stage.finish ... tool_calls=0`；报告消息内容就是
  `{"name": "web_search", "arguments": {"query": "…", "max_results": 5}}` 这样的纯文本，
  即模型把工具调用写进了 `content` 而非 `tool_calls`，工具从未真正执行。
  影响：E-01/E-02「生成结构化报告」不达成，工具链路在真实模型下不被触发。
  处置：不改代码、不加提示词 hack、不换模型；以
  `test_live_e2e.py::test_live_report_is_a_report_not_a_tool_call_payload`
  的 strict xfail 固定为「已知未达成」，一旦修复该用例会 XPASS 逼人更新。
  **提供方前提与关闭依据**：本节结论测于 Ollama `qwen2.5-coder:7b`；
  此后默认提供方改为 OpenAI 兼容 API（ADR-014，缺凭据 fail-fast），
  在该提供方下已复测通过（2026-09-15，M4 真实验收）：`deepseek-flash` 跑通
  collect → analyze → report，collector/analyst 各产生一次 `calculator` 调用
  （`21*2`、`21+21`，均 `succeeded`），报告正文引用工具返回值 `42`。
  因此 F-02 记为**已关闭**，并印证了当时的判断——问题出在该模型对工具调用
  格式的指令遵从度，不是编排层的工具链路。`test_live_e2e.py` 里那条 xfail
  已相应改为正式断言（见「影响」）。
- **F-03 性能数据的按模型归因不可用**（C 侧，**已修复**）。
  `app/observability/callbacks.py::_model_name()` 对 ChatOllama 取到的是类名
  （日志实测 `event=llm.start model=ChatOllama`），`event=llm.finish` 则完全没有
  `model` 字段（`_model_from_result()` 返回 None）——`doc/15 ...§六`「对比不同模型
  （Ollama vs OpenAI）下的 Token 效率」因此无法按模型归因；即使 `metrics` 表建好，
  相同原因也会让带 `model` 标签的采样失真。
  **根因（实测，非推断）**：LangChain 1.x 起回调的 `serialized` 已是
  `{"type": "not_implemented", "name": "ChatOllama", "id": [...]}`，**没有 `kwargs`**，
  所以「从 `serialized["kwargs"]["model"]` 取」这条路根本不存在；`invocation_params`
  只有 `{"_type": "chat-ollama", "stop": null}`；`LLMResult.llm_output` 为 `None`。
  真实模型名只在回调的 `metadata["ls_model_name"]` 上（LangChain 自动注入）。
  **修法**（仅改 C 自己的模块，不碰编排层）：
  1. `_model_name(serialized, callback_kwargs)` 按
     `metadata["ls_model_name"]` → `invocation_params["model"]` →
     `serialized["kwargs"]["model"]`（旧形状兼容）→ `serialized["name"]`（兜底）取
     真实模型名；
  2. `ObservabilityCallbackHandler` 在开始时按 `run_id` 记下模型名，结束时使用
     （ChatOllama 的结果里没有模型名），`on_llm_error` 一并清理。
  验证：真实模型链路 `event=llm.start/finish model=qwen2.5-coder:7b`（修复前为
  `model=ChatOllama` / 无字段）；回归用例
  `tests/unit/test_observability_metrics.py::test_callback_handler_uses_real_model_name_not_integration_class`
  用实测回调载荷固定该形状，
  `test_callback_handler_falls_back_to_legacy_serialized_kwargs` 固定旧形状兼容。
  ——本 ADR 初稿提的「一行级修法（`_model_name` 优先取 `kwargs["model"]`）」经实测不成立，
  已按上述做法更正；`observed_stage(model=...)` 这条编排层透传路径也始终是空的
  （`pipeline_graph.py:229` 不传 `model`），本次修复不依赖它。
- **F-04 其他成员仍缺的前置（三项均已落地，2026-09-15）**：
  B——`metrics` 表 DDL 未建（`/api/v1/metrics` 恒 `not_integrated`）→ 已建
  （`app/core/checkpoint.py::MetricRecord` + `init_checkpoint_schema()`，
  `GET /api/v1/metrics` 返回 `available`）；
  D——`app/api/main.py:171` 未把 `tool_catalog()` 注入 `InspectionStore`
  （`/api/v1/tools` 恒 `not_integrated`）、Prometheus 文本端点未暴露
  （`deploy/prometheus.yml` 只抓 dapr-sidecar）→ 已注入且 `backend:8000/metrics`
  可抓（`doc/api.md` §5.3、§5.6，`doc/roadmap.md`「M4 代码落地情况」）；
  A——`tests/unit/test_pipeline_tools.py::test_role_stage_without_registry_does_not_bind_tools`
  仍失败、`PATCH /api/v1/config/agents/{agent_id}`（I-08）未实现 → 用例已改用
  `set_tool_registry_factory(lambda: None)` 显式构造前置条件，I-08 已实现（ADR-013）。
  本轮性能数据仍是**行为日志**通道采样（测量时上述通道尚不可用），
  数值有效；事实源通道现已可用，可另取一轮做通道一致性对比。
- **F-05 poc / 故障演练路径业务终态与 Dapr 终态不一致**（B 侧，**已修复**）。
  症状：`app/workflows/poc.py` 把 `session_id="demo-session"` 放进 `WorkflowTask`，
  而 `app/core/checkpoint.py::_as_uuid()` 会 `uuid.UUID(str(value))`，
  于是 `finalize_activity` 写报告消息时抛 `ValueError: badly formed hexadecimal UUID string`
  （已在容器内直接复现），业务行已写成 `completed`，Dapr 侧
  `Orchestration completed with status: FAILED`。实测：4 次演练 4 次 FAILED；
  而 API 调度路径（`session_id` 是真 UUID）无一失败（并发跑批 10/10 `COMPLETED`，
  E-02/E-04 亦为 `COMPLETED`），因此影响面是 `scripts/fault_recovery.ps1` 与
  `python -m app.workflows.poc` 这条演示/演练路径——它当时不能作为干净的 E-03 验收。
  **修复（B 侧，合入 master 后并入本分支）**：`session_id` 不再硬编码为
  `demo-session`，并在 CLI 侧对运行时终态 fail-fast（非 `COMPLETED` 或
  无 `serialized_output` 即 `SystemExit` 非零码），使这条路径不再能「业务 completed
  而运行时 FAILED」地静默通过。
  `scripts/measure_recovery.py` 的日志侧终态校验**保留**为守卫（不因对方修好就撤掉），
  本 ADR 的 E-03 实测数据仍取自修复前的测量，且走 poc 的确定性（假模型）路径。
- **F-06 会话/长期记忆未接入编排**（跨模块，**仅记录，未在本轮处置**）。
  `app/memory/` 只有 Protocol 与 schema（实际存储在 `app/core`，属 B 的 ADR-005 范围），
  `WorkflowTask` 只带 `task=payload.content`，`list_messages` 的唯一调用方是
  `GET /messages`，即历史消息既不落记忆也不回注 Prompt。
  系统当前仍可跑通是因为 PostgreSQL 是事实源；但事实源模块 2「会话记忆持久化」的
  内容在真实链路里尚未生效。是否补、由谁补留给后续决策。
  **2026-09-16 更新**：本条描述的两处已变——真实存储不再等待 `app/core`，Redis 实现已落地在
  `app/memory/redis_store.py`（`69960e1`，见 ADR-005 的「修订（2026-09-16）」）；
  **但「未接入编排」的结论不变**，`WorkflowTask` 仍只带 `task=payload.content`，
  历史消息既不落记忆也不回注 Prompt（`doc/testing.md` §4.4）。

## 影响

新增文件：`tests/e2e/conftest.py`、`tests/e2e/test_pipeline_e2e.py`、
`tests/e2e/test_live_e2e.py`、`tests/unit/test_perf_tooling.py`、
`scripts/perf_concurrency.py`、`scripts/measure_recovery.py`；本 ADR。

- **未新增依赖**：`httpx` 已是 dev 依赖、`docker` 已是直接依赖，
  因此 `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 无需同步。
- **E 系列有了可重复的验收入口**，`doc/testing.md` §3.1/§3.2 从「手工/无数据」
  变为「脚本 + 实测数据」，并在 §4.2 记录本轮状态。
- **F-01 仍未清**，会让工具链路的正确性结论失真，因此在文档里标为未清缺口；
  F-05 已由 B 修复（本 ADR 的 E-03 数据取自修复前，恢复演练的终态一致性
  需在修复后重跑一遍确认），F-04 三项前置已全部落地。
- **F-02 已关闭**（换用 API 提供方后复测通过），因此真实模型下的工具演示成立；
  `tests/e2e/test_live_e2e.py::test_live_report_is_a_report_not_a_tool_call_payload`
  已从 strict xfail 改为正式断言——**该翻转未在本机复跑**（本机无 compose 与凭据），
  下次带凭据跑 `MACP_E2E_LIVE=1` 时由该用例本身验证；若在 Ollama 提供方下跑，
  它会失败，那正是 F-02 描述的现象，M5 的验收口径以 API 提供方为准。
- **F-03 已修复**（C 侧自有的 `app/observability/callbacks.py`，未触碰他人代码）：
  Token 采样与 `event=llm.*` 日志现在带真实模型名，
  `doc/15 ...§六` 的「按模型对比 Token 效率」在通道可用后具备前提；
  仍未达成的部分是 OpenAI 侧没有可用凭据（见 F-02 与性能数据的降级说明）。

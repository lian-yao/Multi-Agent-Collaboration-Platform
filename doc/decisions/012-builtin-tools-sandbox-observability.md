# ADR-012: 内置工具、沙箱隔离与可观测接入

状态：已接受

## 背景

`doc/15 AI Native多智能体协作平台.md` 的模块 3 与模块 5 要求 4 个内置工具
（计算器、网页搜索、沙箱代码执行、只读 SQL）、沙箱强制隔离，以及
OpenTelemetry + Jaeger 追踪与 Prometheus 指标。ADR-009 已把编排层的工具契约
（`ToolSpec` / `ToolCall` / `ToolRegistry`）冻结，并指明成员 C 只要实现
`app.mcp.registry.build_tool_registry()`，流水线即可零改动接入。

在此之前 `app/mcp`、`app/tools`、`app/sandbox` 为空，`app/observability` 只有结构化
日志，因此 `doc/testing.md` 的 U-06、U-08、I-07 无法验收，I-06 缺真实注册表。

## 决策

### 工具与沙箱

1. **工具形状统一在 `app/tools/base.py`**：每个工具 = `ToolSpec` 描述（
   `name` / `description` / `input_schema`）+ Pydantic 入参模型 + `run()`。
   参数非法统一转成 `ToolExecutionError`，由编排层 `ToolCaller` 归一化为
   `failed` 记录，不中断流水线（ADR-009 的约定）。
2. **计算器用 AST 白名单求值，不用 `eval`**：只允许数值常量、四则运算与幂、
   白名单 `math` 函数与 `pi`/`e`/`tau`，并限制整数常量、幂指数与 `factorial` 入参。
   拒绝属性访问、下标、布尔比较与一切内建调用，工具本身不构成代码执行面。
3. **只读 SQL 是纵深防御**：`assert_read_only_statement()` 做可读的快速失败检查
   （去注释、去字符串字面量后要求以 `SELECT`/`WITH` 开头、无内部分号、无写关键字），
   真正的边界是事务级只读模式（PostgreSQL `SET TRANSACTION READ ONLY` +
   `SET LOCAL statement_timeout`，SQLite `PRAGMA query_only = ON`）。
   不支持的方言直接拒绝，不静默退化成可写连接。
4. **沙箱默认 Docker，且不静默降级**：`app/sandbox/docker_runtime.py` 用一次性容器
   执行代码，`network_disabled=True`、`read_only=True` + `tmpfs /tmp`、
   `mem_limit` / `nano_cpus` / `pids_limit`、`security_opt=['no-new-privileges']`、
   非 root 用户、超时即 kill。Docker 不可用时抛 `SandboxUnavailable` 并让工具失败，
   **绝不退回宿主进程执行**——安全边界不做静默降级。
5. **策略检查先于后端启动**：`run_in_sandbox()` 先做长度上限与语言策略
   （Python 走 AST 白名单，Shell 走网络/提权/破坏性命令模式），
   拒绝时后端一次都不会被调用（`SandboxViolation`）。
   两类错误语义不同：策略拒绝是代码本身越权，环境不可用是环境问题。

### MCP 接入

6. **同步门面包异步会话**：编排层的 `ToolRegistry` 协议是同步的，MCP Python SDK 是
   异步的。`app/mcp/client.py::_SessionBridge` 在后台线程里跑一个专用事件循环并复用
   会话，对上层暴露同步 `run()`。会话惰性建立（ADR-009 要求 `build_tool_registry()`
   保持廉价），`close()` 负责释放子进程/连接。
7. **传输方式可配置**：`MCP_TRANSPORT` 取 `inprocess`（默认，直接调用内置注册表，
   无进程/网络开销）、`stdio`（`python -m app.mcp.server` 子进程）或 `http`
   （连接已部署的 MCP Server）。三种后端都实现同一协议，切换不影响编排层。
8. **MCP 文本约定与错误还原**：`app/mcp/server.py` 把工具结果包成
   `{"status": "succeeded"|"failed", "output"|"error": ...}` 的 JSON 文本；
   客户端解析该约定，把 `isError` 或 `status=failed` 还原成 `ToolExecutionError`，
   使越过进程边界的失败仍然等价于本地失败。

### 可观测

9. **阶段级标签上下文**：`observed_stage()`（`app/observability/instrumentation.py`）
   用 contextvars 为一次阶段执行设置 `workflow_id` / `stage` / `role` / `agent_id` /
   `model`，工具与模型调用通过该上下文自动关联，**不需要在编排层签名里透传参数**。
   接入点在 `app/orchestration/pipeline_graph.py::_run_role_stage`，改动是一层上下文。
10. **指标去重面向 Dapr 重放**：同一执行身份（`stage:{workflow_id}:{stage}`、
    `tool:{call_id}`、`tokens:{workflow}:{stage}:{kind}`、`workflow:{id}:{status}`）
    只记一次，避免活动重放导致重复累计。Token 名称固定为
    `input_tokens` / `output_tokens` / `total_tokens`（`doc/api.md` §5.5 的交接约定）。
11. **`metrics` 表由成员 B 建，本模块只反射**：`PostgresMetricSink` 在表不存在时
    记一次 `metrics.table_missing` 警告并跳过，不建表、不改结构；
    也**不把失败吞成「零条记录」**（`doc/api.md` §5 要求区分「未接入」与「零条记录」）。
12. **观测写入无条件让位于业务**：采样写入使用独立的短超时连接
    （`DEFAULT_SINK_TIMEOUT_SECONDS`，不经业务连接池），失败一次后本进程内不再重试
    （`disabled`）并把降级状态记进日志。没有这条约束时，数据库不可用会让
    Workflow 终态活动长时间阻塞。
13. **两条导出通道**：Prometheus（进程内计数器/直方图，
    `render_prometheus_metrics()` 输出文本格式）与 `metrics` 表采样
    （供 Web 控制台按 Workflow 过滤展示，`doc/api.md` §5.5 由 D 读取呈现）。
14. **模型调用回调挂在模型实例上**：`build_chat_model()` 为 Ollama/OpenAI 挂上
    `ObservabilityCallbackHandler`。回调由 LangChain 随实例携带，`bind_tools()` 后的
    绑定对象同样继承，编排层与工具路径都不需要重复传参；
    回调内部的任何异常都被吞掉，观测失败不影响业务。

## 影响

- U-06、U-08、I-06（单元级）、I-07（单元级）有了可直接运行的证据：
  `tests/unit/test_builtin_tools.py`、`tests/unit/test_sandbox_policy.py`、
  `tests/unit/test_mcp_tools.py`、`tests/unit/test_observability_metrics.py`。
- **ADR-009 的自动接线得到验证**：`default_tool_registry()` 现在返回 C 的注册表，
  流水线零改动即可发现并调用 4 个工具。
- **A 的一条既有用例因此失效**：`tests/unit/test_pipeline_tools.py::
  test_role_stage_without_registry_does_not_bind_tools` 通过「C 的模块不存在」
  来构造「没有注册表」，其断言 `default_tool_registry() is None` 只在 C 落地前成立。
  用例意图（没有注册表时不绑定工具、不产生调用记录）仍然有效，
  但需要用 `set_tool_registry_factory(lambda: None)` 显式构造该前置条件。
  该文件属成员 A，本决策只记录，不改动。
- **I-06 仍未端到端验收**：本 ADR 的证据走的是 `mcp.shared.memory` 的真实协议往返，
  没有跨进程 stdio 与真实 PostgreSQL 写入，端到端验收待容器环境。
- **`prometheus-client` 与 `docker` 提升为直接依赖**：两者原先只作为传递依赖存在，
  其中 `prometheus-client` 甚至不在 `uv.lock` 里（`uv sync` 会移除它）。
  已按 `doc/conventions.md` 同步 `pyproject.toml`、`uv.lock`、`doc/requirements.txt`。
- **仍需其他成员配合**：`metrics` 表 DDL（B）、`/metrics` 与 `/tools` 的读取接线
  （D，见 `doc/api.md` §5）、容器环境变量与 Prometheus 抓取配置（部署侧）。

# ADR-010: 应用行为日志（结构化事件）

状态：已接受

## 背景

`doc/architecture.md` 把「追踪、指标、行为日志」划给可观测性层，
`doc/15 AI Native多智能体协作平台.md` 模块 5 要求「记录 Agent 的 ReAct 循环
（推理→行动→观察）和每次工具调用，保存在结构化日志中供审计和分析」。
但 `app/observability` 一直是空目录，后端只有两类输出：

- uvicorn 的 HTTP 访问日志（`GET /api/v1/... 200 OK`）；
- durabletask 的编排原生日志（`Orchestrator yielded with 1 task(s)`）。

两者都看不到「哪个 Workflow 的哪个阶段、用哪个角色、跑了多久、为什么失败」。
D7-D8 联调时排查一次任务失败只能靠 `workflow_runs.error` 反查，成本高。

## 决策

1. **日志基建放在 `app/observability/logging.py`**（对齐 architecture.md 的模块归属），
   只提供三件事：`get_logger(component)` 统一命名（`macp.<component>`）、
   `configure_logging()`（幂等，统一格式与级别）、`log_event()` 结构化事件输出。
   追踪与指标不在本次范围。
2. **事件格式为 `event=<name> field=value ...`**，值含空格或 `=` 时加引号，
   值为 `None` 的字段省略。一行一条事件，可以直接
   `Select-String "event=stage.finish"` 过滤与统计。
3. **当前事件清单**（由编排层发出）：

   | 事件 | 级别 | 字段 |
   | --- | --- | --- |
   | `stage.start` | INFO | workflow_id、stage、role、task_chars、tools |
   | `stage.finish` | INFO | workflow_id、stage、role、chars、tool_calls、duration_ms |
   | `stage.failed` | ERROR | workflow_id、stage、role、error、duration_ms |
   | `tools.discovered` | INFO | count、tools（有工具时才输出） |
   | `tool.call` | INFO / WARNING | call_id、tool_name、status、duration_ms、error |

4. **只记长度，不记正文**。任务正文、模型输出、工具入参都不进日志，
   只记 `task_chars` / `chars`；正文的事实源仍是 Dapr State Store 与 `messages` 表。
   这样日志不会随报告变长，也不会把用户内容写到日志系统里。
5. **入口统一调用 `configure_logging()`**（`app/workflows/worker.py::main`），
   级别可由环境变量 `LOG_LEVEL` 控制（默认 `INFO`）。第三方库自带 handler，
   不接管 uvicorn / durabletask 的日志。
6. **`workflow_id` 透传**。`app/workflows/pipeline.py::advance_pipeline_stage` 新增
   `workflow_id` 参数，阶段活动把 Workflow 实例 ID 传进来：既用于日志关联，
   也作为工具调用 ID 的默认 scope，补齐 ADR-009 留的「B 侧传实例 ID」这一环。

## 影响

- `docker compose logs backend` 现在能直接看到每个阶段的开始/结束/失败与耗时，
  并按 `workflow_id` 关联到 `workflow_runs` / `messages`：

  ```text
  2026-09-11 07:41:50,790 INFO macp.orchestration.pipeline event=stage.start workflow_id=45f8... stage=collect role=collector task_chars=6 tools=0
  2026-09-11 07:41:50,799 ERROR macp.orchestration.pipeline event=stage.failed workflow_id=45f8... stage=collect role=collector error="ConnectError: [Errno 101] Network is unreachable" duration_ms=8.7
  ```

- 失败事件按 Activity 重试各记一条（默认 3 次），因此一次失败通常看到 3 组
  `stage.start` + `stage.failed`，与 `SUBTASK_RETRY_POLICY` 一致。
- 日志级别调整只需 `LOG_LEVEL=DEBUG` 重启容器；事件命名沿用
  `stage.*` / `tool.*`，后续接 OpenTelemetry 时可据此对齐 span 与指标名。
- 改动只新增日志，不改变执行语义与阶段载荷结构：既有测试与三步流水线行为不变。

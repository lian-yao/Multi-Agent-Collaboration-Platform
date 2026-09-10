# ADR-007: Workflow 阶段活动默认使用真实 Ollama 模型

状态：已接受

## 背景

`doc/dapr-integration.md` §3 规定固定三步流水线（收集 → 分析 → 报告）映射为顺序
Workflow 活动；§8 说明 D3 上午的 POC 使用 Fake 模型验证序列化、幂等与恢复语义。
D3-D4 交付因此保留了 `fake_stage_result`：阶段活动返回 `collected task: ...` 之类的
占位串，不调用任何模型，Web 上只能看到执行进度、看不到模型产出。

D5-D6（里程碑 M3「多 Agent 协作」）的接线前提已由两侧交付：

- `app/orchestration/pipeline_graph.py::run_role_stage` 提供与 `fake_stage_result`
  同构的阶段结果（`step` / `status` / `content` / `previous`）；
- `app/agents/roles.py` 提供 collector / analyst / reporter 的角色系统 Prompt（ADR-006）。

`602c957`（feat: 封装可恢复的多 Agent 子任务 Workflow）把两者接进阶段活动，
并把每个阶段封装为子 Workflow。本文档固化该决策与其影响。

## 决策

1. **默认使用真实模型**：`advance_pipeline_stage` 默认调用 `run_role_stage`，按阶段
   取对应角色的 `system_prompt` 调用模型；阶段活动的输入/输出载荷
   （`build_step_payload` / `build_step_result`）与 Checkpoint 语义保持不变，
   恢复边界不受影响。
2. **模型来源**：沿用 `app.config.AgentSettings`（`AGENT_LLM_PROVIDER` /
   `AGENT_OLLAMA_BASE_URL` / `AGENT_OLLAMA_MODEL`），与单 Agent 图共用
   `build_chat_model`，不新增模型配置项。
3. **保留显式 Fake 开关**：`use_fake_model=True` 时仍走 `fake_stage_result`，
   供故障恢复演练（`app/workflows/poc.py` 默认开启）与没有 Ollama 的环境使用；
   Fake 不再是 Web 任务的默认路径。
4. **执行封装**：每个阶段由子 Workflow `agent_subtask_workflow` 承载，实例 ID 为
   稳定的 `{workflow_id}:{stage}`，父 Workflow 通过 `call_child_workflow` 调度并附加
   3 次尝试的重试策略，保持跨进程恢复语义。
5. **失败语义**：模型不可用或调用失败时活动抛异常，重试耗尽后由父 Workflow 的
   异常分支执行 `finalize_activity(status=failed)`，`workflow_runs.error` 记录原始
   错误——沿用既有终态回写链路。
6. **幂等边界**：LLM 调用位于活动内部，活动结果由 Dapr 持久化后不重复执行，
   与 `doc/dapr-integration.md` §6「允许重放但禁止重复外部副作用」一致。

## 影响

- 一键部署后需要宿主机 Ollama 常驻。容器通过 `deploy/compose.yaml` 的
  `AGENT_OLLAMA_BASE_URL`（默认 `http://host.docker.internal:11434`）访问；
  Ollama 不可用时任务会停在 `failed`，而不是用占位串跑完。
- 单次任务从秒级变为模型级耗时：本机 `qwen2.5-coder:7b` 单次调用 60-110 秒，
  三步串行实测约 5 分钟（2026-09-10 实测 collect 430 字、analyze 919 字、
  report 1553 字）。
- 阶段结果仍是内容字符串，落库与展示链路不变；把最终报告作为
  `messages(role=assistant)` 落库以便前端展示属于后续增量，本 ADR 不覆盖。

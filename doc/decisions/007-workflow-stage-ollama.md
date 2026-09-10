# ADR-007: Workflow 阶段活动接入真实 Ollama 模型

状态：已接受

## 背景

`doc/dapr-integration.md` §3 规定固定三步流水线（收集 → 分析 → 报告）映射为三个
顺序 Workflow 活动；§8 说明 D3 上午的 POC 使用 Fake 模型验证序列化、幂等与恢复语义。
D3-D4 交付因此保留了 `app/workflows/pipeline.py` 的 `fake_stage_result`：阶段活动返回
`collected task: ...` 之类的占位串，不调用任何模型。

D5-D6（里程碑 M3「多 Agent 协作」）的接线前提已由两侧交付：

- `app/orchestration/pipeline_graph.py::run_role_stage` 提供与 `fake_stage_result`
  同构的阶段结果（`step` / `status` / `content` / `previous`）；
- `app/agents/roles.py` 提供 collector / analyst / reporter 的角色系统 Prompt（ADR-006）。

本次把两者接进 Workflow 阶段活动，使流水线真正调用 Ollama 本地模型。

## 决策

1. **接线位置**：`app/workflows/pipeline.py::advance_pipeline_stage` 改为调用
   `run_role_stage`；阶段活动的输入/输出载荷（`build_step_payload` /
   `build_step_result`）与 Checkpoint 语义保持不变，恢复边界不受影响。
2. **模型来源**：沿用 `app.config.AgentSettings`（`AGENT_LLM_PROVIDER` /
   `AGENT_OLLAMA_BASE_URL` / `AGENT_OLLAMA_MODEL`），与单 Agent 图共用同一个模型工厂
   `build_chat_model`，不新增模型配置项。
3. **移除 Fake**：删除 `fake_stage_result`；测试替身统一使用注入的 `FakeChatModel`
   （`doc/testing.md` §1），生产路径不再保留占位实现。
4. **失败语义**：模型不可用或调用失败时活动抛异常，由 `agent_pipeline_workflow`
   的异常分支执行 `finalize_activity(status=failed)`，`workflow_runs.error` 记录原始
   错误——沿用既有终态回写链路，不新增重试策略。
5. **幂等边界**：LLM 调用位于活动内部，活动结果由 Dapr 持久化后不重复执行，
   与 `doc/dapr-integration.md` §6「允许重放但禁止重复外部副作用」一致。

## 影响

- 一键部署后需要宿主机 Ollama 常驻。容器内通过 `deploy/compose.yaml` 的
  `AGENT_OLLAMA_BASE_URL`（默认 `http://host.docker.internal:11434`）访问；
  Ollama 不可用时任务会停在 `failed`，而不是用占位串跑完。
- 单次任务从秒级变为模型级耗时：本机 `qwen2.5-coder:7b` 单次调用约 12-15 秒，
  三步串行约 35-45 秒。
- 阶段结果仍是内容字符串，落库与展示链路不变；把最终报告作为
  `messages(role=assistant)` 落库便于前端展示属于后续增量，本 ADR 不覆盖。

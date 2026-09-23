# ADR-019: 会话记忆接线（写点、读点与注入口径）

状态：已接受

## 背景

`app/memory/` 的会话记忆（Redis List `session:{id}:messages`）与长期记忆（Redis Hash
`agent:{id}:memory`）在 `69960e1` 已落地并有单测（ADR-005、`doc/testing.md` U-11），
但 `app/api`、`app/orchestration`、`app/workflows` 里一直没有调用方：历史消息既不落记忆、
也不回注提示词，设计文档模块4 的「会话记忆：支持多轮对话上下文继承」因此没有落到真实链路
（缺口 F-06，见 ADR-016）。ADR-005 的修订版明确把「历史消息怎么进提示词」留给编排/工作流层，
本 ADR 补上这条决策。

## 决策

### 写点（谁写）

1. **用户消息**：`POST /api/v1/sessions/{id}/messages` 受理成功后，
   `app/api/main.py::send_message` 把该消息追加进会话记忆（`role=user`、`status=running`、
   带 `id` 与 `agent_run_id`）。
2. **助手报告**：Workflow 终态回写活动 `finalize_activity` 在写入 assistant 报告消息
   （ADR-008）之后，把同一正文追加进会话记忆（`role=assistant`、`status=completed`、
   `id` 用 `report_message_id(workflow_id)`，与 PostgreSQL 行同 ID）。

只写「进入用户视野的消息」：阶段中间结论与工具观察不写记忆——它们是 Workflow 内部载荷、
重放时会重建，写进记忆只会污染上下文。

### 读点（谁读、怎么进提示词）

- `app/workflows/pipeline.py::advance_pipeline_stage` 在阶段活动里按 `session_id` 读
  **最近 `CONVERSATION_CONTEXT_LIMIT`（10）条**，剔除 `agent_run_id` 等于本轮的消息
  （本轮任务已作为「用户任务」进提示词，不再重复），把其余消息按时间正序渲染成
  `【会话历史（最近 N 条，供多轮上下文继承）】` + `role: content` 段落，放在用户输入最前面
  （`app/orchestration/pipeline_graph.py::_conversation_block`）。
- 没有历史时提示词与接线前逐字一致（不产生空段落），单轮行为的既有断言不受影响。

### 降级与一致性

- Redis 不可用：实现层已按 `doc/data-model.md` §5 降级为「空 / 无操作」并记警告，
  消费方不再 try/except；记忆缺失不该让一次协作失败。
- 记忆是缓存不是事实源：PostgreSQL `messages` 表是最终事实源；缓存条目里的 `status`
  是**写入时刻的快照**，不随后续状态迁移更新（注入只消费 `role` 与 `content`）。
- 运行期由 `app/memory/runtime.py::conversation_memory()` 提供实例，测试用
  `set_conversation_memory_factory` 注入内存替身（与 `set_redis_factory`、
  `set_tool_registry_factory` 同一模式）。

## 备选与未采纳

- **长期记忆本期不接线**：`agent:{id}:memory` 是跨会话偏好/知识，写入方应是显式的
  「记住这个」交互或独立抽取流程；设计文档把向量检索标为可选，因此保持
  「实现就绪、无调用方」。需要接线时另开 ADR。
- **不把历史塞进 Workflow 载荷**（如 `WorkflowTask.history`）：那会让历史随每次活动调用
  序列化、放大 Dapr 状态，且从提交到执行期间历史会过期；改在活动内按 `session_id` 读。
- **不做向量检索与摘要压缩**：超出本期范围，需要新 ADR 并修订设计事实源后再排期。

## 影响

- 提示词长度随会话轮次增长，由 10 条上限约束；单条内容长度未截断（当前报告正文最长约 4k 字符）。
- 每个阶段活动多一次 Redis 读（一次 `lrange`），失败降级为空历史。
- 测试基线：`tests/unit/conftest.py`、`tests/integration/conftest.py`、`tests/e2e/conftest.py`
  都注入会话记忆内存替身，用例不依赖真实 Redis，也不会把测试数据写进开发环境。
- 对外契约不变：`doc/api.md` 的请求/响应字段与状态码不变，受理消息只是多了一个内部写缓存动作。

## 修订（2026-09-23）：动态编排漏接会话记忆

**问题（使用者实测反馈）**：「同一个会话不记得我之前说过什么」。查证：会话记忆**写入正常**
（Redis 里能看到该会话每一轮的用户与助手消息），是**回注**没到位——而当时的接线只覆盖了
固定三步链路。

根因：本文的读点写在 `app/orchestration/pipeline_graph.py`（`_conversation_block` →
`_role_input`）与 `app/workflows/pipeline.py::_session_history`，即**静态三步**那条；
后来新增的动态编排（ADR-019 的档 2，`dynamic_graph.py`）两个入口都只给当前任务——
规划是 `HumanMessage(content=f"用户任务：\n{task}")`、每步是 `parts = [f"用户任务：\n{task}"]`，
从没带过历史。而前端默认编排模式就是 `dynamic`（`App.tsx`），所以使用者日常走的那条
恰好是没接的那条。同一条记忆里，助手对「重试我要你写的代码」回答「本会话没有其他消息」，
即模型端确实没看到历史。

修复（**两条编排共用一份实现，避免再次分叉**）：

- 新增 `app/orchestration/context.py`：`CONVERSATION_CONTEXT_LIMIT` 与
  `conversation_block()` 从 `pipeline_graph.py` 提出来，两条链路共用；
- `app/workflows/pipeline.py::_session_history` 改公开名 `session_history`（读点不变：
  最近 N 条、剔除本轮 `agent_run_id`），动态链路直接复用；
- `dynamic_graph.generate_plan()` / `step_input()` / `run_plan_step()` 增加 `history` 入口，
  历史放在提示词**最前面**（与静态链路同一形态）；
- `workflows/dynamic.py` 的 `dynamic_plan_activity` / `dynamic_step_activity` 按
  `session_id` + `agent_run_id` 读记忆并透传（`agent_dynamic_workflow` 本来就把这两个字段
  放进了 task，缺的只是有人读）。

**规划节点也要历史**：这不只是"步骤好看一点"——用户只回「重试」两个字时，规划模型没有历史
就连"重试什么"都无法判断，只能产出一份泛泛的计划。

验证：`tests/unit/test_dynamic_pipeline.py` 新增 4 例（规划提示词携带历史且历史在任务之前、
**无历史时提示词逐字不变**、步骤输入带历史前缀、`run_plan_step` 透传），
`tests/unit/test_workflow_dynamic.py` 新增 2 例（规划活动与步骤活动确实读到了会话记忆，
注入内存替身断言内容）。全量 `pytest tests/unit tests/integration/test_workspace_api.py`
→ **961 passed / 12 failed**（12 条是缺 `pypdfium2` 的既有 PDF 用例，与本修订无关）。

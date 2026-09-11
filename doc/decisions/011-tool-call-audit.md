# ADR-011: 工具调用审计与幂等

状态：已接受

## 背景

三条既有约束要求工具调用可审计、可重放：

- `doc/data-model.md` §3 定义了 `tool_calls` 表，§5.4 规定「先插 `running` 再执行，
  执行完成或失败后更新」；
- `doc/dapr-integration.md` §6 规定「允许重放但禁止重复外部副作用；重放前先查同 ID 结果，
  重复投递时返回缓存结果」；
- ADR-009 把工具契约冻结在编排层，并明确「审计落库由成员 B 负责，编排层只产出记录对象」。

在此之前 `tool_calls` 表只有设计没有实现，`doc/testing.md` 的 U-07、I-06 无法验收。
本次落地补齐这张表与调用入口，并固化语义。

## 决策

1. **落库原语放在 `app/core/checkpoint.py`**：新增 `tool_calls` 表与
   `create_tool_call` / `get_tool_call` / `mark_tool_call_running` / `complete_tool_call` /
   `fail_tool_call` / `list_tool_calls`。`create_tool_call` 用
   `INSERT ... ON CONFLICT (id) DO NOTHING`，与 ADR-008 的报告消息一样保证同 ID 只留一行。
2. **编排层不写库**：`app/orchestration/tools.py` 仍只产出 `ToolCallRecord`；
   审计由 `app/core/tool_audit.py::AuditedToolRegistry` 装饰器在注册表外侧完成。
   `list_tools` 透传，`call` 包一层——ADR-009 的 `ToolRegistry` 协议不变，
   成员 C 的实现无需感知审计。
3. **调用 ID 稳定派生**：`derive_tool_call_id(...)` 把业务键（`run_id` / `workflow_run_id` /
   `tool_name` / `tool_input`）序列化后取 sha256，再转 uuid5。
   首选仍是编排层给出的 `ToolCall.call_id`（阶段活动传入
   `tool_scope = workflow_run_id or workflow_id or run_id`，见 ADR-009/010），
   `derive_tool_call_id` 只是调用方没给 ID 时的兜底，两条路径都保证同一执行重放得到同一 ID。
4. **按已有状态决定行为**：

   | 已有记录 | 行为 |
   | --- | --- |
   | 无 | 插入 `running` 后执行 |
   | `succeeded` | 直接返回缓存 `output`，**不重复执行**工具 |
   | `failed` | 置回 `running` 后重试（允许同 ID 重试） |
   | `running` | 抛 `ToolCallInProgressError`，拒绝并发重放 |

   最后一条是对 `doc/dapr-integration.md` §6 的补充：`running` 说明另一个 worker
   正在执行同一调用，此时返回缓存没有意义（还没有结果），等待又会阻塞活动，
   因此选择快速失败，交给 Dapr 活动自己的重试策略处理。
5. **终态字段约定**：成功写 `status=succeeded` + `output`，`error` 置空；
   失败写 `status=failed` + `error="<异常类>: <消息>"`，`output` 置空。
   写入前统一过一遍 JSON 往返（`default=str`），保证落进 JSONB 列的数据可序列化。
6. **接入点**：`app/workflows/pipeline.py::advance_pipeline_stage` 在存在注册表且
   `run_id` 非空时，用 `AuditedToolRegistry` 包住注册表，并把阶段活动载荷里的
   `agent_run_id` / Workflow 实例 ID 传进来。没有注册表时不包装——没有调用就没有审计行。
7. **只读消费**：`GET /api/v1/workflows/{workflow_id}/tool-calls`（`doc/api.md` §5.4）
   只读 `tool_calls`，不建表、不写审计。

## 影响

- U-07 有了可直接运行的证据：`tests/unit/test_tool_audit.py` 覆盖
  running→succeeded 落库顺序、同 ID 缓存命中不再执行、失败落库、failed 同 ID 重试、
  `running` 拒绝并发重放，以及装饰器接线。
- **当前没有真实审计数据**：`app/mcp` 尚未提供注册表，`default_tool_registry()` 返回
  `None`，因此工具不会被调用，`tool_calls` 表为空。I-06 的端到端验收要等成员 C 落地注册表。
- `tool_calls.run_id` 非空并外键指向 `agent_runs`，所以审计只在有 AgentRun 的执行里成立；
  单 Agent 调试路径（无 AgentRun）不走这条路。
- 并发语义是「乐观拒绝」而非加锁等待：同一 `call_id` 的第二个 worker 直接失败，
  由上层重试决定后续，避免活动长时间阻塞。
- `metrics` 表与 Token 指标写入不在本决策范围，仍属 M4 缺口。

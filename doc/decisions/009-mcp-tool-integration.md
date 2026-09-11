# ADR-009: 编排层接入 MCP 工具

状态：已接受

## 背景

D7-D8（里程碑 M4）要求「实现 MCP 工具注册与调用；至少 4 个示例工具；集成可观测性」。
`分工.md` §3 把 M4 拆成四条：成员 A「流水线接入 MCP 工具并验收」、成员 C「完成内置工具、
沙箱、可观测接入」、成员 B「支持工具调用审计落库」、成员 D「展示调用链路与 Token 统计」。

开工时 `app/mcp`、`app/tools`、`app/sandbox`、`app/observability` 均为空目录，
编排层没有任何可消费的工具契约：工具描述长什么样、调用记录有哪些字段、注册表怎么发现与调用，
跨目录没有约定。若不先冻结接口，成员 A 的「接入」与成员 C 的「实现」会各自发明一套，
合并时必然返工——这正是 `分工.md` §4.2「契约先行」要避免的情况。

已有文档已给出大部分语义：

- `doc/15 AI Native多智能体协作平台.md` 模块 3：工具要能「动态发现」，由「LLM 识别任务需求
  自动选择合适的工具」；
- `doc/data-model.md` §3：`tool_calls` 表字段（tool_name / input / output / status / error）；
- `doc/api.md` §4.10：`GET /tools` 返回 name / description / input_schema；
- `doc/dapr-integration.md` §6：工具调用允许重放但禁止重复外部副作用，注册表按调用 ID 去重。

## 决策

1. **契约冻结在编排层**。`app/orchestration/tools.py` 定义 `ToolSpec`（对齐 `/tools`）、
   `ToolCall`、`ToolCallRecord`（对齐 `tool_calls` 表）、`ToolRegistry` 协议与 `ToolCaller`
   （发现 + 调用 + 记录）。工具实现与 MCP Server 仍由成员 C 在 `app/mcp` 提供，
   只要实现 `list_tools()` / `call(request)` 即可被流水线消费；审计落库仍由成员 B 负责，
   编排层只产出可落库的记录对象，不直接写库。
2. **接入方式为模型驱动（ReAct）**。角色节点把发现的工具经 `bind_tools` 交给模型，
   模型返回 `tool_calls` 时逐个调用工具，并把观察结果作为 `ToolMessage` 回填，
   直到模型不再请求工具或达到轮次上限（4 轮）。未接入注册表时保持原有的单次模型调用。
3. **默认注册表解析**。`default_tool_registry()` 依次尝试：显式工厂
   （`set_tool_registry_factory`，供测试与自定义部署）、成员 C 的
   `app.mcp.registry.build_tool_registry()`、最后返回 `None`。因此成员 C 落地注册表后，
   Dapr 阶段活动（`app/workflows/pipeline.py`）**无需改动**即可用上工具。
4. **阶段载荷新增 `tool_calls`**。阶段结果由 `{step, status, content, previous}` 扩展为
   附带 `tool_calls`（无调用时为空列表）；既有消费方只读 `content`，属向后兼容增量。
5. **工具失败不阻断流水线**。工具异常、未知工具统一归一化为 `failed` 记录并作为观察回填
   给模型；模型不支持 `bind_tools` 时退回普通对话调用。任务是否失败仍由模型与 Workflow
   终态决定，避免把工具问题放大成整体不可用。
6. **调用 ID 语义**。默认 uuid4（本进程内唯一）；持久化调用方给出稳定 `tool_scope`
   （如 Workflow 实例 ID）时改用 uuid5 派生，使同一次执行重放得到相同调用 ID，
   便于注册表按 ID 去重（`doc/dapr-integration.md` §6）。

## 影响

- 成员 A 的接入已完成并带验收测试（`tests/unit/test_pipeline_tools.py`，含「Dapr 阶段活动
  消费默认注册表」用例）。`tool_calls` 落库、`GET /tools` 端点、前端调用链路与 Token 展示
  分别由成员 B、C、D 按本契约补齐。
- `app/mcp` 未提供前行为与接入前完全一致：不绑定工具、阶段载荷 `tool_calls` 为空列表，
  既有测试与三步演示不受影响。
- 工具调用发生在 Workflow 活动内部，活动结果由 Dapr 持久化，重放不重复执行外部副作用
  （与 ADR-007 的模型调用边界一致）。若要跨重放去重，成员 B 需把 Workflow 实例 ID 作为
  `tool_scope` 传下来，并让 `tool_calls` 按调用 ID 落库。
- `default_tool_registry()` 每个阶段调用一次，成员 C 的 `build_tool_registry()` 应保持廉价
  （内部缓存客户端），避免每个阶段重建 MCP 连接。
- 是否真的发起工具调用取决于模型能力：本地模型返回 `tool_calls` 时走工具路径，否则自动
  退回纯生成路径，不影响既有的三步流水线演示。

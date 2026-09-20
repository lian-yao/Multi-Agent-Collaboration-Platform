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

## 修订（2026-09-20：工具轮次上限与空输出兜底，F-07）

真实 API 提供方（`deepseek-flash`）实测暴露：模型可以一直请求工具、撞上
`TOOL_CALL_MAX_ITERATIONS`（4）后仍不产出文字，阶段于是记 `completed` 但 `content` 为空，
下游拿到空上游内容、最终报告退化成「输入缺失」说明（ADR-016 F-07）。本 ADR 的
ReAct 循环因此补两条兜底（实现在 `app/orchestration/pipeline_graph.py`）：

1. **上限告警**：循环结束后若仍有待处理 `tool_calls`，落
   `event=stage.tool_iteration_limit stage=… role=… iterations=4 pending_tool_calls=N`
   （WARNING）——这是「阶段无文字产出」最常见的成因，之前不可观测。
2. **空输出补一次文字提示**：`content` 无文字时补发
   `请直接用文字给出本阶段的结论，不要再调用工具。` 重试一次（`MAX_EMPTY_CONTENT_RETRIES=1`）；
   仍为空则落 `event=stage.empty_content action=continue_with_empty` 后继续，**不阻断流水线**
   （与「工具失败不中断」的口径一致）。

判空只看 `content` 是否为空——**不把「还有待处理 tool_calls」当有产出**，否则上述
「撞上限」场景会被误判为正常完成（首版实现即踩此坑，实测 8/12 阶段载荷仍为空）。

效果（2026-09-20 真实模型实测）：同批 live 运行 12 份阶段载荷 **0 份为空**，
日志出现 7 次 `stage.tool_iteration_limit` + 5 次 `stage.empty_content action=retry`，
且**没有 `continue_with_empty`**（补提示 5/5 全部救回文字）；代价是 live 套件
168.87s → 343.69s（每个空阶段多一次模型调用）。

## 修订 2（2026-09-20：工具调用失败重试，人类要求）

口径：**工具调用失败时重试 3 次；3 次过后不再重试，按实际结果输出**。实现在
`app/orchestration/tools.py`：

- `TOOL_CALL_RETRY_LIMIT = 3`：首次调用 + 最多 3 次重试 = 最多 **4 次尝试**；
- **沿用同一个 `call_id`**：审计层 `_begin_call` 对 `failed` 行先重置为 `running` 再执行
  （U-07「失败可同 ID 重试」），因此一次逻辑调用在 `tool_calls` 表里**始终只有一行**，
  最终状态就是真实结果（成功用成功输出，耗尽重试则记 `failed`）；
- 每次尝试与重试都落结构化日志：`event=tool.call … attempt=N max_attempts=4` 与
  `event=tool.retry … attempt=N next_attempt=N+1 error=…`（WARNING）；
- 耗尽重试后把失败观察（`{"status":"failed","error":…}`）交给模型，**不中断流水线**；
  若模型仍不给文字，由前述 F-07 兜底再补一次文字提示——即「必须输出」由
  「失败观察 + 空输出补提示」两级保证，输出内容基于真实结果（含失败原因）。

**先红后绿**：新增 `tests/unit/test_pipeline_tools.py` 两条用例——
`test_failed_tool_call_is_retried_until_it_succeeds`（前两次失败、第三次成功：
`status=succeeded`、同一 ID、`content` 正常）与
`test_tool_call_stops_after_three_retries_and_stage_still_answers`（持续失败：
尝试 4 次后记 `failed`、阶段仍 `completed` 且有文字输出）。修复前实测只尝试 1 次。

**真实模型实测（2026-09-20）**：live 套件 `7 passed / 586.22s`（重试前 343.69s）；
本轮产生 81 次 `tool.retry`，出现 `attempt=4 max_attempts=4` 的最终失败记录；
`tool_calls` 表里每个 (tool_name,status) 分组 `rows == ids`，确认「一逻辑调用一行」未被破坏。

**暴露的两个新问题（未处置）**：

1. **确定性错误被无谓重试**：`calculator` 的 `不支持的表达式节点: List`、
   `sql_query` 的 `relation "sqlite_master" does not exist` 都是模型给出的非法参数/
   无效 SQL，重试 3 次不会成功，只增加延迟（本轮 live 套件多花约 4 分钟）。
   可按错误类型分类重试（仅重试超时/连接/沙箱不可用等瞬时错误），需新 ADR。
2. **容器内沙箱不可用（F-08）**：`code_execution` 在 compose 里必然失败——
   backend 容器没有挂载 `/var/run/docker.sock`，日志为
   `ToolExecutionError: 沙箱不可用: Docker 守护进程不可用`。宿主机直跑后端时可用，
  容器部署下该工具形同不可用；修法（挂 socket 或改用独立沙箱服务）需部署决策。

## 修订 3（2026-09-20：按错误类型分类重试，只重试瞬时故障）

修订 2 的「失败就重试 3 次」把模型自己造成的确定性失败也一起重试了（非法表达式、
无效 SQL、策略拒绝），日志噪声大且白等时间。本修订给重试加上分类：

- **异常携带 `retryable`**：`app/tools/base.py::ToolExecutionError` 增加 `retryable`
  关键字（默认 `True`），语义为「换个时刻同样的调用是否可能成功」；
- **确定性失败标 `retryable=False`**：入参校验失败（`BuiltinTool.invoke`）、
  计算器全部失败（表达式内容决定）、只读策略拒绝（空语句/多语句/非 SELECT/写关键字/
  未知方言）、SQL 语句本身写错（`ProgrammingError`/`IntegrityError`/`DataError`）、
  沙箱策略拒绝（`SandboxViolation`）、工具未注册（内置与 MCP 注册表）；
- **保持可重试**：服务不可达与超时（`web_search`）、沙箱/数据库暂时不可用
  （`SandboxUnavailable`、其余 SQLAlchemy 异常）、MCP 传输类错误、未知异常
  （默认 `retryable=True`，宁可多试）；
- **编排层**：`ToolCaller.invoke` 只在 `retryable` 为真时重试，日志把决策记全——
  `tool.call … retryable=<bool>`，重试时 `tool.retry`，放弃时
  `tool.retry_skipped … reason=non_retryable`（ADR-009 修订 2 的同一 ID 与
  「3 次过后必须输出」口径不变）。

分类用结构化异常类型判断（`isinstance`），不做字符串匹配；`retryable` 只影响编排层决策，
不进 `tool_calls` 表（重试明细仍在日志）。

**先红后绿**：新增 6 条用例（编排层：不可重试只尝试 1 次并落 `tool.retry_skipped`、
可重试仍尝试 4 次；工具层：计算器失败、参数非法、只读策略拒绝均为 `retryable=False`，
SQL 执行按 `ProgrammingError`/`OperationalError` 分类），实现前 6 条全红。

**真实模型实测（2026-09-20）**：live 套件 `6 passed / 1 failed / 586.46s`——
唯一失败是用例自身假设问题（见下），与分类无关。日志统计：**36 次 `tool.retry`**
（瞬时：`web_search` 超时、沙箱不可用）+ **13 次 `tool.retry_skipped`**
（非法 SQL 的 `UndefinedTable`、代码策略拒绝 `禁止导入模块: os`），即避免了 13×3 次
必然失败的重试。
**代价仍然在可重试侧**：本机无公网出口，`web_search` 每次尝试约 10s、重试 3 次 ≈ 40s/次，
是 live 套件 9–10 分钟耗时的主要来源；要压缩这段时间应把 `TOOL_SEARCH_ENDPOINT`
指向可达搜索服务，或在无网环境不向 Agent 暴露该工具（配置项，不在本次范围）。

**同轮修掉的用例假设问题**：`test_live_tool_calls_are_readable_from_postgresql`
原断言 `total == len(items)`，隐含「单次运行的调用数不超过默认单页 20 条」；
本轮真实运行出现 34–39 次调用（模型反复查库）后该断言误判为失败。改为按分页语义断言：
取 `page_size=100`，`total <= page_size` 时 `total == len(items)`，否则 `len(items) == page_size`
（其余「每行都属于本次 Workflow 且状态为终态」的断言不变）。

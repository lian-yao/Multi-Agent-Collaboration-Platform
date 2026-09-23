# ADR-038: 自动编排升级（意图识别 → 波次并行 → 合成 → 校验）

状态：已接受

## 背景

ADR-019 落地的 `dynamic`（自动编排）是「档 2」：**用 LLM 生成一份一次性计划，然后按依赖串行执行**。
它解决了「换人不换图」，但三件事一直缺着（`doc/orchestration.md` 档 3 的清单）：

1. **没有并行**：同一波里彼此无依赖的步骤一次只放行一个，等待时间没有换成收益；
2. **没有路由**：简单任务（问一个事实、解释一个概念）也要走三步，白花两次调用与一次编排；
3. **没有合成与校验**：最后一个成功步骤的正文直接当成交付物，多路结论冲突时没人收口，
   也没人核对「这份输出到底满足用户的要求吗」。

另外两件在实测里暴露的事：

- **重试是装饰性的**：`call_child_workflow(retry_policy=…)` 挂着策略，但步骤活动把异常收敛成
  `failed` 结果返回，Dapr 根本看不到异常，重试从未触发；
- **动态链路没有逐节点轨迹**：`/stages` 对动态执行返回 `not_integrated`，画布「形状齐、详情空」。

## 决策

### 1. 新流程：intake → 路由 → 编排 → 波次并行 → 合成 → 校验

| 阶段 | 做什么 | 失败口径 |
| --- | --- | --- |
| **intake** | 一次平台模型调用，同时产出「改写后的任务」（ADR-037）与结构化意图（`intent_type` / `user_goal` / `constraints` / `need_multi_subtask`） | 分段降级：文本不可用退原文、意图不可用置空；两者都不可用仍按多 Agent 走 |
| **路由** | 意图解析成功且 `need_multi_subtask=false` → **单 Agent 直答**（固定 1 步，角色 `reporter`） | 意图调用失败 / 解析不出来 → 一律按多 Agent（安全默认） |
| **编排** | 规划模型一次性产出完整 DAG（`depends_on` 表达阶段），每步带 `expected_output` / `retry` / `timeout_seconds` | 解析失败整份丢弃，回退固定三步（ADR-019 §2 不变） |
| **波次并行** | 按 `depends_on` 算层级，同波无依赖的步骤**一起派发**；并发上限 `AGENT_MAX_PARALLEL_WORKERS`（默认 3） | 单步失败只连坐依赖它的步骤（含传递闭包），同波其它分支照常产出 |
| **合成** | 把本轮所有可用子任务产出合成交付物：冲突消解、去重、按约束成稿；**失败子任务必须在正文开头说明** | 合成失败 → 退回最后一个成功子任务的产出，并在收尾时写明 |
| **校验** | 判定交付物是否满足原始意图，输出 `{satisfied, defects[], missing[]}` | 校验器不可用视为**通过**（`source=fallback`），绝不因它自己坏了推翻可用交付物 |

**单 Agent 直答跳过编排、并行、合成与校验**——省掉这些正是这个分支存在的理由；它仍然受
「固定 1 步 + 必须产出交付物」的护栏约束，护栏失败即整次失败，不自动升级为多 Agent
（避免不可预期的成本与行为）。

### 2. intake 与改写合并成一次调用

改写与意图识别的输入（长期偏好 + 会话历史 + 本轮附件名 + 本轮原话）与位置（执行最前面）
完全一样，分两次调用等于把同一份上下文喂两遍。合并后**降级仍然是分段的**：
`rewrite_source`（`model` / `original`）与 `intent_source`（`model` / `fallback`）各自独立，
旧口径「只吐任务正文、没有 JSON」也照样工作（文本照用、意图按多 Agent 处理）。

### 3. 持久化事实源仍是 Dapr Workflow，不引入 LangGraph Checkpointer

父工作流用 `call_child_workflow` × N + `when_all` 实现波内并行，子工作流实例 ID 为
**`{workflow_id}:dyn:r{round}:{step_id}`**（轮次进 ID：重编排的第二轮会有同名的 `s1`）。
进程内 LangGraph 图用 `Send` 表达同一拓扑，供评测脚本与单测直跑；**波次、依赖就绪度与
状态口径是同一份纯函数**（`app/orchestration/dynamic_graph.py` 的 `waves()` /
`pending_batch()` / `build_flow()`），两条路径不会各漂一套。

不引入 `langgraph-checkpoint-postgres` 的理由：Dapr Workflow 已经是持久化执行的事实源
（ADR-020），再叠一层 Checkpointer 等于两套恢复语义；`Send` 与并行 reducer 在现有
`langgraph==1.2.11` 里已经具备，不需要新依赖。

### 4. 重试挂在活动调用上，失败收敛在子工作流里

- 重试次数 = 计划里该步的 `retry`（重试次数，0–3）+ 1，缺省时取
  `AGENT_SUBTASK_MAX_ATTEMPTS`（默认 3，含首次）；
- 步骤活动**失败即抛错**，Dapr 才会按 `retry_policy` 重试；
- 重试耗尽后，**子工作流捕获异常并返回 `failed` 业务结果**——若让异常冒到父工作流，
  `when_all` 会在第一个分支失败时立刻结束，同波其它分支已经跑出的结果会一起丢掉。

### 5. 部分失败 = `completed` + `partial`

只要还有可用交付物，终态就是 `completed`；同时 checkpoint 记 `partial=true`、
`failed_steps`、`skipped_steps`，画布顶上一条黄标，合成器在正文开头点名缺席的子任务。
**不新增独立终态**：那要同时改 `workflow_runs` 状态机、`messages.status`、前端状态词汇与
`doc/api.md`，收益却只是让「部分完成」看起来更特别；而 ADR-016 F-05 要求的「业务终态与
Dapr 终态一致」在 `completed` 下仍然成立。

### 6. 校验不达标 → 带缺陷清单重编排，最多一轮

轮次上限硬编码 1（`AGENT_MAX_PLAN_ROUNDS` 可下调为 0）。第二轮是**新的执行轮次**：
复用第一轮的 intake 结果（不重复花那一次平台调用），带 `validation.defects + missing`
重新规划与执行，子工作流实例 ID 换到 `r2` 命名空间。第二轮仍不达标只记录结论、不再循环。

### 7. 逐节点轨迹落盘，`/stages` 对动态链路可用

- 每步活动写状态存储：key `{prefix}:workflow:{workflow_id}:dyn:r{round}:{node_id}`，
  载荷形状与静态阶段载荷对齐（`step` / `status` / `content` / `previous` / `tool_calls`，
  动态链路多一个 `error`）；
- 父工作流在**每批前后**各刷一次 `workflow_runs.checkpoint` 摘要（父工作流单点写，
  避免并行子工作流互相覆盖）。因此摘要是**波粒度**的，逐步细节在各自的 key 里；
- checkpoint 增 `flow[]`（`kind ∈ intent|plan|worker|synthesize|validate` + `depends_on` +
  `status` + `wave`），`plan[]` 仍然只装子任务；画布优先读 `flow`，读不到回落 `plan`；
- `/stages` 对带 `flow` 的动态执行返回 `availability=available`，`items` 按节点给出
  输入 / 产出 / 工具调用；**升级前发起的执行没有 `flow`，仍按原样返回
  `not_integrated`**（旧数据不碎）。

### 8. 旧计划容忍

升级瞬间在途的执行会重放旧计划（没有新字段）：缺省字段按服务端默认值与默认并发执行，
不引入 checkpoint 版本号、不排空在途任务。代价是「重试次数」在旧计划上只能取默认值——
这比让一次升级把在途任务全部判废要好。

### 8.1 重试改由子工作流显式循环，`attempts` 记**实际**次数

原先的重试挂在 `call_child_workflow(retry_policy=…)` / 活动调用上，但两个问题：
Dapr **不把「第几次尝试」告诉工作流代码**，而且父工作流的 `when_all` 会在任一分支抛错时
提前结束（同波其它分支的产出一起丢）。现在改成**子工作流里的一层显式循环**：

- 每次尝试都是一个独立的、持久化的活动调用，退避走 `ctx.create_timer`（首次 1s、系数 2、
  上限 10s——与原先 `RetryPolicy` 的参数同口径），因此重放语义不变；
- 循环由子工作流掌握，于是它知道「这是第几次」：成功时把 `attempts` 盖在结果上，
  耗尽时返回 `failed` 业务结果（带上真实次数）而不是把异常抛给父工作流；
- `StepOutcome.attempts` 随 checkpoint 的 `plan[].attempts` 落库，状态载荷里另记
  `attempt`（本次是第几次）与 `attempts`（一共用了几次）。

**「配了 2 次重试」与「真的重试了 1 次才成功」是两件事**：审计要看的是后者。

### 8.2 累计 Token 预算（成本闸门）

`AGENT_TOKEN_BUDGET`（默认 0 = 不限制）给单次执行设一条累计上限，口径是
**intake + 规划 + 子任务（含重试）+ 合成 + 校验**的模型用量之和（重编排时第二轮把第一轮
已用的量带过去，`tokens_used_prior`）。

- 用量的来源是**活动/节点结果**里的 `tokens`（从模型响应的 usage 元数据读），
  不是可观测采样：控制流必须只依赖确定性输入，否则重放会得到不同的停止点；
- 预算是**下限口径**：模型没回用量时按 0 计（不按字数估算）——宁可少算，也不编一个数字进审计；
- 用尽后的行为是**收口而不是作废**：不再派发剩余子任务、不再开新一轮重编排，
  但**照常合成**已完成的部分。剩余步骤记 `skipped`，原因写成「Token 预算已用尽」，
  合成器在正文开头点名缺席项，checkpoint 记 `tokens_used` / `token_budget` /
  `budget_exceeded`，画布顶上一条「Token 预算用尽（已用 X / 上限 Y）」。

### 8.3 一条只有真机才会暴露的教训：`when_all` 是模块级函数

`when_all` 属于 `dapr.ext.workflow` 的**模块级函数**，不是工作流上下文的方法
（真实运行时给工作流的是 `_RuntimeOrchestrationContext`，它没有这个属性）。首版把它写成
`ctx.when_all(tasks)`，单测与进程内回归网全绿——因为**替身自己造了一个同名方法**；
在真实 sidecar 上第一次运行就 `AttributeError: '_RuntimeOrchestrationContext' object has no
attribute 'when_all'`（2026-09-24 实测）。

两处教训都写进了用例与替身：替身只替**数据与环境**，不替 API 形状；而「真实部署上跑一次」
是这类错误唯一的出口——这也是 `tests/e2e/test_live_e2e.py` 补 E-06 的直接原因。

### 9. 角色提示词随工作模式更新（ADR-006 修订 2026-09-24）

自动编排改的是**流程**，但角色的 system prompt 还停在固定三步的假设上（「你是第一步」
「下游是数据分析 Agent」「使用者主要看你的输出」）。这在并行 + 合成器的新形态下会直接产生
错误行为：并行分支互相看不见却以为对方在等自己、子任务成稿抢着当最终结论。

三个角色的位置口径因此统一改成「**本次执行里的一个步骤**」：

- 上游以输入里的「上游结果 / 你这一步的职责」为准（可能零个、一个或多个）；
- 同一角色可能并行出现多次，**并行子任务互不可见**——每条要点必须自包含、带来源；
- 下游可能是其它子任务，**也可能直接进合成器**；reporter 由输入判断本次是「子任务成稿」
  还是「单 Agent 直答」，据此决定口吻；
- 提示词里「手上真有什么」的能力说明（工作区工具名、附件、MCP/搜索、覆盖删除走人工审批）
  一条没删——那是 ADR-037 §4 的既有约束。

回归钉子：`tests/unit/test_agent_roles.py::test_role_prompts_match_the_automatic_orchestration_mode`
与 `test_prompts_are_aligned_with_the_current_software`。细节与理由见 ADR-006 的同日修订。

## 备选方案

- **每波完成后回编排器重算下一批**（真自适应）。否决：计划每次重算都要解决「重放时计划变动」，
  而 ADR-019 的持久化语义要求计划一旦落盘即冻结；收益（更贴任务）在评测数据里还没被证明。
  本期只在**校验不达标**这一个明确信号上重编排。
- **引入 LangGraph Checkpointer 替代 Dapr 作为恢复事实源**。否决：与 ADR-020 冲突，
  且要新增依赖、改写部署与断点续跑演示口径，而现有能力（`Send` + reducer）已经够用。
- **给用户一个「要不要并行」的开关**。否决：ADR-019 §3 的教训——用户无法为「该由谁主导、
  要不要拆」做出有依据的判断；并行度由平台按成本闸门决定。
- **为合成器与意图识别新增协作角色**。否决：它们是**平台节点**（不产角色语义、不进团队目录、
  没有角色图标），新增 `RoleId` 会连带改 `doc/data-model.md`、角色种子、`/agents` 列表与前端
  图标解析，换来的只是名字上更像"多了一个 Agent"。

## 影响

- 新增 `app/orchestration/intake.py`（改写 + 意图）、`app/orchestration/synthesis.py`（合成 + 校验）；
- `app/orchestration/dynamic_graph.py` 的 `DynamicPipelineState` 增 `intent` / `route` / `round` /
  `flow` / `partial` / `validation` 等字段，`results` 变成 **按键合并的 reducer**（并行的前提，
  ADR-019 §3 早就把它列为前置条件）；`build_dynamic_pipeline` 重建为
  `intake →（直答 | 规划 → 波内并行 → 汇聚循环 → 合成 → 校验）→ finalize`；
- `app/workflows/dynamic.py` 重建：新增 intake / 合成 / 校验 / checkpoint 四个活动，
  父工作流改为按波 `when_all`，`app/workflows/service.py` 注册新活动；
- `app/api/stage_trace.py` 增动态分支；`AgentSettings` 增 5 个开关（并发、重试、超时、校验）；
- 前端：`types/api.ts` 增 `FlowNodeSummary` 与 checkpoint 新字段，`collaboration.ts` 改为
  「flow → plan → 静态阶段」三级回落，画布区分平台节点并显示部分失败黄标；
- `static` 固定三步链路（含其阶段活动、`/stages` 静态分支、默认
  `AGENT_ORCHESTRATION_MODE=static`）**行为不变**。
- **真机验收（2026-09-24，宿主形态 + 真实 Dapr sidecar + 真实 PostgreSQL/Redis + 真实
  `deepseek-flash`）**：`MACP_E2E_LIVE=1` 下 `test_live_e2e.py::test_live_dynamic_orchestration_shape_and_traces`
  通过——`route=multi`、flow 为 `intent / plan / 5×worker / synthesize / validate`、
  `tokens_used=531980`、报告 4505 字符落库、`/stages` 对每个节点都读得到轨迹（耗时 261s）；
  静态链路在同一次会话里回归通过（三轮 90.3s / 报告 6029 字符）。
- `doc/15` 未改（其 line 76 的「LLM 分析、任务分解、自动选择编排模式」正是本 ADR 实现的形态）。
- 若继续往前做，仍待办的是：每波回编排器重算的自适应重编、HITL、共享黑板
  （见 `doc/orchestration.md` §3.2）。

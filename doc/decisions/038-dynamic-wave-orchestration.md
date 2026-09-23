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
- 已知缺口（诚实记录）：**没有累计 token 预算**（只有并发、步数与轮次上限）；
  Dapr 不向工作流代码暴露重试的实际尝试次数，checkpoint 只记配置值；`doc/15` 未改
  （其 line 76 的「LLM 分析、任务分解、自动选择编排模式」正是本 ADR 实现的形态）。

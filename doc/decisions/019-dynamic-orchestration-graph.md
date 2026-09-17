# ADR-019: 动态编排图（按任务决定参与角色与顺序）

状态：已接受

## 背景

`doc/15 AI Native多智能体协作平台.md:76` 写明「编排引擎调用 LLM 进行分析与任务分解，根据任务
类型自动选择编排模式」；`doc/roadmap.md` 的「范围说明」也一直把「动态并行分派、依赖 DAG 与
人工介入」列为后续版本。而代码事实是：

- `app/orchestration/pipeline_graph.py` 全是 `add_edge`，拓扑固定 `START → collector → analyst
  → reporter → END`，全仓 grep `intent|router|supervisor|conditional_edges` 零命中；
- `PIPELINE_ROLE_ASSIGNMENT` 是静态字典，`role_for_stage` 只做查表；
- ADR-018 因此删掉了 composer 里的「主决策 Agent 选择器」——后端没有路由节点，那个下拉是假选择。

也就是说：「按任务动态分配 Agent」目前只有文档、没有实现。

## 决策

**并列新增一条动态编排链路，默认关闭；既有静态链路一个字节不改。**

`AGENT_ORCHESTRATION_MODE`（`AgentSettings`）取 `static`（默认）/ `dynamic`；
`POST /sessions/{id}/messages` 可用 `orchestration_mode` 字段做**单次**覆盖（§4.4）。
`WorkflowService.schedule` 据此在 `agent_pipeline` 与 `agent_dynamic` 之间选择，非法值一律
退回 `static`。

### 1. 另写一套状态，不放宽静态状态机

`PipelineState.current_step` 是单值指针，`complete_step` 强校验「只能完成当前步骤」，
`PIPELINE_STEPS` 是固定三元组。动态路径若复用它，必须放宽这套顺序校验——而静态链路正是靠它
支撑 Dapr 侧「每阶段一个固定实例 ID 的子 Workflow」的可恢复性。

因此动态链路自带 `DynamicPipelineState`（`app/orchestration/dynamic_graph.py`）：
没有 `current_step`，**用 `results` 里已有的步骤反推「下一步能跑谁」**——依赖关系才是这个图的
第一公民。静态契约零改动，回归风险为零。

### 2. 规划失败必须降级，不能失败

规划节点拿到的是一段自由文本。`parse_plan` 逐条校验（角色必须已知、id 唯一、
`depends_on` 只能引用**在它之前已声明**的步骤，由此天然排除自依赖与环、步骤数不超上限），
**任何一处不合法就整份丢弃**并回退 `fallback_plan()`（与静态流水线同构的固定三步）。

不修补半份计划，是刻意的：修补出来的计划比固定三步更不可预期，而失败模式是「用户拿到一份
没人设计过的流程」。规划调用本身抛异常同样降级，`plan_source` 字段把
`llm` / `fallback` 区分出来，`plan_rationale` 说明回退原因，前端与日志都能看出「真的规划过」
还是「降级了」。

### 3. 执行按「依赖就绪」调度，失败只连坐下游

每一步声明 `depends_on`；节点每次取首个「依赖全部完成」的步骤，把依赖步骤的正文按
`【步骤 id · 角色名】` 标注后拼进输入。所以**同一角色可以在一次执行里出现多次**
（先收集 A 再收集 B 再对比），这是静态图做不到的。

某步抛错只把该步记为 `failed`，依赖它的步骤（含**传递闭包**）记为 `skipped`，与之无关的
步骤照常执行。只有当整次执行**没有任何可用交付物**时，终态才是 `failed`。

### 4. 计划本身是持久化的，子工作流实例 ID 稳定

Dapr 链路（`app/workflows/dynamic.py`）：规划是**一个独立活动**，结果进 Dapr 状态存储；
重放时不会重新调用规划模型——否则恢复一次就得到一份新计划，续跑无从谈起。子工作流实例 ID
为 `{workflow_id}:dyn:{step_id}`，与静态链路的 `{workflow_id}:{stage}` 同样满足「同一次执行
重放得到相同实例 ID」。

### 5. 本档为串行执行，且不碰并行

图拓扑一次只放行一个就绪步骤。本档的目标是「**换人不换图** → **按任务换流程**」，
不是并行。波内 fan-out、结果聚合与反思循环属档 3，见 `doc/orchestration.md`。

## 备选方案

- **复用 `PipelineState`、放宽 `complete_step` 的顺序校验**。否决：那会同时削弱静态链路的
  既有保证（Dapr 按阶段拆子 Workflow 的可恢复性就建立在「阶段名固定」上），为一个新功能
  动一条已在验收的链路，性价比是负的。
- **只把「阶段 → 角色」从静态字典改成函数（只换人不换图）**。否决：改动最小，但本质仍是
  固定三步；对于「简单问题不该走三步」「需要两路收集再对比」这类任务没有收益，
  属于为了能说「支持动态分配」而做的表面改动。
- **做成 composer 里的运行期下拉**。暂缓：ADR-018 刚因为「假选择」删掉过一个同位置的
  控件。这次先在后端落地，用单次请求参数验证；要不要给用户一个常驻开关，等有真实评测数据
  再说——**先有可用的编排，再决定要不要暴露**。

## 影响

- 新增 `app/orchestration/dynamic_graph.py`、`app/workflows/dynamic.py`；
  `app/config.py` 增 `orchestration_mode` / `max_plan_steps`；
  `app/workflows/service.py` 增 `resolve_workflow_name` 并按模式注册/调度；
  `app/api/main.py` 的 `MessageRequest` 增 `orchestration_mode`。
- 这两个模块落在 `app/orchestration`（成员 A 边界）与 `app/workflows`（成员 B 边界）里，
  属**跨边界改动**，已在 `分工.md` §4 记明。静态链路文件只做了加法（`pipeline_graph.py` 增两个
  公开薄封装 `content_with_tools` / `invoke_role_messages`，供动态图复用同一套 content 归一化
  与 ReAct 工具循环，避免两份实现跑偏）。
- **未解决、需要跟进的**：
  - **动态模式不支持按步骤断点续跑之外的恢复语义**：规划与每步都已持久化，但「计划中途改变
    步骤集合」没有处理（计划一旦落盘即冻结，这是有意的）；
  - **没有并行**：见上「决策 5」；
  - **规划质量未评测**：`plan_source` 能区分是否降级，但没有「降级率」指标，也没有把动态模式
    纳入 `tests/e2e` 的真实模型回归。
- `doc/orchestration.md` 记录了档 3（真·多智能体自主协作）的设计草案与前置条件。

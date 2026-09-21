# ADR-030: 多智能体拓扑只做「计划驱动的 DAG」，六种拓扑其余留档 3

状态：已接受

## 背景

评审时对着 LangGraph 官方的「多智能体架构」六种拓扑图问：

> 当前项目的多 agent 架构，支持智能识别并编排如图各种架构的任务流吗？

六种拓扑是：**Single Agent**、**Network（网状）**、**Supervisor（监督者）**、
**Supervisor as tools（监督者即工具）**、**Hierarchical（分级）**、**Custom（自定义）**。

问题问的是「智能识别并编排」，所以真正要回答的是两件事，必须分开答：

1. **系统会不会判断「这个任务适合哪种拓扑」**——即有没有拓扑选型；
2. **六种拓扑各自能不能被表达出来**——即有没有对应的执行结构。

第 2 条不成立时，第 1 条无从谈起。这份 ADR 把两条都定下来，并把没做的部分**归到档 3**
（`doc/orchestration.md` §3），避免「看起来能、实际不能」的误判。

## 决策

### 1. 事实源是「计划」，不是「拓扑类型」

系统里**没有拓扑这个概念**。规划节点（`app/orchestration/dynamic_graph.py::planner_prompt`，`:256`）
产出的是一份步骤清单，每步四个字段：`id` / `role` / `instruction` / `depends_on`（`PlanStep`，`:84`）。
拓扑是从 `depends_on` 这张图上**长出来的形状**，不是被选中的类型。

这个选择决定了后面的所有边界：**能表达的拓扑，等于「能写成有向无环依赖图」的拓扑**。

### 2. 已支持的三种

| 拓扑 | 在本仓的形态 | 证据 |
| --- | --- | --- |
| **Single Agent** | 单角色问答图，一个 `agent` 节点 | `app/orchestration/graph.py:16 build_langgraph_agent` |
| **Custom（DAG 子集）** | planner 产出的任意依赖 DAG；同波并行、跨波串行 | `dynamic_graph.py:165 parse_plan`（校验依赖）、`:346 ready_steps`（按依赖放行）、`:634 build_dynamic_pipeline` |
| **静态三步流水线** | Custom 的退化情形：一条写死的链 | `app/orchestration/pipeline.py` 的 `(COLLECT, ANALYZE, REPORT)`、`pipeline_graph.py:346` |

**「动态画并行/串行」是算出来的，不是写死的**——这是决策 5 要展开的部分，也是本轮的主要交付。

### 3. Supervisor 只有半套

`planner` 只**在开头跑一次**（`app/workflows/dynamic.py:217` 的规划活动在步骤循环之外），
它做的是「一次性静态指派」，之后流程就冻结了。真正监督者架构的两个特征都不具备：

- **没有 handoff**：步骤之间不互相移交控制权，`for step in plan.steps`（`dynamic.py:224`）
  顺序推进；
- **没有二次决策**：执行期没有节点回头看「现在该不该改派给别人」的入口。

所以它是「**前置规划器**」，不是监督者。用户看图时最容易在这一条上产生误判，必须写明。

### 4. 未实现的四种，归档 3

| 拓扑 / 能力 | 为什么现在没有 | 归属 |
| --- | --- | --- |
| **Hierarchical（分级）** | 没有「父层 supervisor + 子层 worker」的第二层结构；planner 只产出平铺的步骤列表 | A |
| **Network（网状）** | 依赖在计划期一次定死且**禁止成环**（`parse_plan` 的 `dep if not in seen` → `None`，`:207`），运行期不可改写 | A |
| **Agent as tools** | 工具池是死枚举（`calculator` / `code_execution` / `web_search` / `sql_query` / `list_session_files` + MCP 工具），Agent 不能互为工具 | C |
| **反思回路（评审打回）** | 全仓 `grep 评审\|反思\|返工\|reflect\|revision`（`--include=*.py`）**零命中** | A |
| **HITL（人工中断）** | `pause` / `resume` 的语义是「暂停整个实例」，不是「在某决策点等人批准」 | B |

这五项在 `doc/orchestration.md` §3 已有设计草案与前置条件；**本 ADR 不改变它们的排期**，
只是把「现状 = 未实现」这个事实固化成可引用的落点。

### 5. 画布口径：并行与串行是**算出来的**

画布不消费「拓扑」，只消费 `checkpoint.plan`。数据契约：

| 后端字段 | 前端消费处 | 视觉 |
| --- | --- | --- |
| `plan[].depends_on` | `collaboration.ts:241 levelOf()` | 没有依赖 = 第 0 波，其余 = 「所有依赖里最深的 +1」 |
| 同波节点数 > 1 | `collaboration.ts:385 parallel` | 该波横排 + 画「并行协作区」框（`GraphCanvas.tsx:253`） |
| 边两端的波次 | `collaboration.ts:417 kind` | 上游单节点 = 串行边；上游多节点 = 并行汇入 |
| `plan[].status` | `planStepStatus()` | 已完成 / 执行中 / 待执行 / 已放弃（`skipped` 与 `pending` 语义相反，必须区分） |

**关键是这套算法对「串行」和「并行」是同一条路径**：静态三步的 `depends_on` 是一条链，
算出来就是三波各一个节点；动态计划的 `depends_on` 是扇出，算出来就有并行波。
**画布没有「特定串行节点」的写死逻辑**，静态链路只是它的一种输入。

数据源如实：动态链路的 `/stages` 返回 `not_integrated`（`app/api/stage_trace.py:210`），
所以动态画布**形状齐、详情空**——节点与并行关系来自 `plan`，每步的输入/产出/工具调用没有。
预览页与后端同口径给出说明文案（`frontend/rendercheck/preview_seed.py::DYNAMIC_TRACE_REASON`），
不把「未集成」显示成「没跑」。

### 6. 默认不启用动态

`AGENT_ORCHESTRATION_MODE` 默认 `static`（`app/config.py:65`）。按默认配置打开界面，
看到的就是那条固定三步链——**这是「画布只能画串行」这个印象的来源**，
不是画布的能力边界。要看动态画布，需要服务端设 `dynamic`，或单次请求带
`orchestration_mode`（`doc/api.md` §4.4）。前端目前没有把模式做成界面开关。

## 备选方案

- **引入拓扑选型（让 planner 先输出「用哪种拓扑」）。** 否决：六种里只有两种能被现有
  执行结构表达，选型会产出执行不了的答案。而且「该用哪种拓扑」的判据是**评测数据**，
  不是提示词（`doc/orchestration.md` §3.3 第 1 条：先有评测集）。
- **补一个 `agents-as-tools` 的通用封装。** 否决：工具池是 `app/tools` 的显式注册表，
  让 Agent 变成工具要定义「子 Agent 的失败怎么向上传播」「它的 Token 算谁的」——属档 3。
- **把画布改成「按实际执行顺序」画，而不是按依赖画。** 否决：那样并行波会被压成一条线，
  正好丢掉本 ADR 要保住的表达能力；依赖图才是这次任务的语义真相。
- **让自定义 Agent 直接进编排。** 否决（本轮）：`role` 是自由文本，而执行期只解析三个内置
  id（`dynamic.py:87` 的 `step.role`、`pipeline.py:232` 的 `role_for_stage`），
  `agent_config.py` 的 `OVERRIDE_FIELDS` 也不含 prompt。放开要连动 A/B 两线，属档 3。

## 代价

- **画布与执行的口径差。** 计划声明同波并行时，执行侧仍然一次只放行一个
  （`dynamic_graph.py:590` 的 `ready[0]`；`dynamic.py:224` 顺序循环），所以**图在说并行、
  实跑没并行**。这是本 ADR 显式保留的差距：并行执行的前置条件（`results` 并发安全合并）
  未做，属 A 线，见 `doc/orchestration.md` §3.1 第 1 条。
- **角色池只有三个**（`RoleId`：`collector` / `analyst` / `reporter`）。「按意图分发 agent」
  的粒度上限就是这个池子；`parse_plan` 遇到池外的角色会让**整份计划作废**并回退固定三步
  （`dynamic_graph.py:195`、`:319`）。所以「扇出」在当前只能表现为同角色多实例或三角色组合。
- **新增一条预览对话**（`s-preview` 下的动态工作流），预览路由 42 → 45 条。

## 影响

- `frontend/rendercheck/workspace-smoke.tsx`：「扇出 → 并行 → 汇聚」三波形状断言，154 → **159**；
- `frontend/rendercheck/preview_seed.py`：新增 `DYNAMIC_PLAN` / `DYNAMIC_TRACE_REASON` /
  `new_dynamic_workflow()`，`stage_traces()` 增加动态分支（与后端 `not_integrated` 同口径）；
- `frontend/rendercheck/build-preview.py`：注入动态工作流三条路由；
- `doc/orchestration.md` §2.6（画布数据契约）、§3（未实现清单交叉引用本 ADR）；
- `doc/roadmap.md`、`doc/testing.md`、`分工.md` §4。

## 教训

「支持哪些架构」这个问题，答案不在功能清单里，而在**数据模型能表达什么**。
本仓把拓扑降维成了「依赖图」，于是表达力的边界变得可以精确回答：**能写成 DAG 的都能画**，
需要运行期改图（Network）、需要第二层（Hierarchical）、需要 Agent 互调（as tools）的都画不出来。
反过来，画布的能力边界也同理——它消费的是 `depends_on`，所以**它早就能画并行**，
只是默认配置不产出并行的数据。判断「能不能」之前，先问「数据从哪来」。

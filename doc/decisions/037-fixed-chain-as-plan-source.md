# ADR-037：固定链是「计划来源」的一种，不是并列的第二种编排模式

日期：2026-09-23 ｜ 状态：**部分落地**（口径与入口已改；`plan` 统一为链路事实待跨线落地） ｜ 关联：ADR-019 / ADR-030 / ADR-034 / ADR-036，`doc/api.md` §4.4、`doc/testing.md` §4.25

## 背景

用户看输入区那个策略控件时提出两件事：

1. 「固定三步是不符合多 agent 协作、动态调度的」——把固定三步与自动编排并排成两个**对等策略**，
   在概念上就是错的；
2. 「不应该像当前这种固定 3 个内置 agent 来完成流水线」——建议参考
   `Multi-Agent-Playground`（五种工作流模板 + 参与 Agent 勾选）那种方案。

第 2 条的一半 ADR-036 已经解决：动态路径的 planner 候选集现在由 `agent_registry` 驱动
（`agent_config.dispatchable_agents()` → `workflows/dynamic.py::_dispatch_candidates()` →
`dynamic_graph.generate_plan(candidates=…)`，既进提示词也进 `parse_plan` 的 `allowed` 校验），
`PlanStep.role` 已是自由文本，人设走 `resolve_step_prompt(role, catalog)` 三级回退。
**「登记一个新 Agent 也永远不会被调度」在动态路径上已不成立。**

剩下的是第 1 条，也是本 ADR 要定的：**「固定三步」在项目里到底是什么。**

勘察结论（本轮实测）：

- **两条 Dapr workflow**：`static` → `agent_pipeline`（`app/workflows/pipeline.py`），
  `dynamic` → `agent_dynamic`（`app/workflows/dynamic.py`），由
  `WorkflowService.resolve_workflow_name`（`app/workflows/service.py:30`）按模式选名。
- **静态链路的 `checkpoint` 里没有 `plan`**：`pipeline_checkpoint_summary`
  （`app/orchestration/pipeline.py:176`）只写 `status` / `current_step` / `completed_steps` /
  `updated_at`；`mode` / `plan` / `plan_source` 只有动态链路才有（`workflows/dynamic.py:191`、`:334`）。
- 于是前端必须**自己知道**「静态 = 收集 → 分析 → 报告」：`App.tsx:99` 有一份 `stages` 常量，
  `collaboration.ts::buildCollaboration` 在没有 `plan` 时按它拼链（`:443`），
  `RunActivity.tsx` 也按它列步骤。**这就是「特例化」的实体——不是按钮，是数据契约。**
- `workflow_runs` **不落编排模式**（`core/checkpoint.py::create_workflow_run`），所以规划窗口内
  「这次走哪条」只有提交方自己知道 —— 这正是 `isPlanning` 必须接 `requestedMode` 的原因
  （`collaboration.ts:106`）。
- 拓扑本身在 ADR-030 已定死为**计划驱动的 DAG**，并明确否决「拓扑选型」。本 ADR 不推翻它，
  只是把「固定链」也收进同一套说法。

## 决策

### 1. 对外只有「计划从哪来」一个概念

取值三种：`planned`（规划节点产出）/ `fallback`（规划失败回退）/ `fixed`（固定链）。
「固定链」不是「另一种编排方式」，而是**计划不由规划节点产出的那一种**。

### 2. 固定链不再是用户可选的**对等策略**

入口从「自动编排 / 固定三步」两个平级分段按钮，改为**单个策略按钮 + 浮层**：主项是
「按任务规划」，固定链降级到浮层的「兜底」分组并带 `is-secondary`（层级在样式上可见，
不只是文案客气）。**本轮已落地**（`STRATEGIES` / `strategyOf` / `composer-strategy-*`）。

### 3. 画布与记录页只说计划来源

不再各自写 `mode === "dynamic" ? … : …` 的三目，统一走
`workspace/collaboration.ts::planSourceKey()` + `PLAN_SOURCE_TEXT` / `PLAN_SOURCE_HINT`。
**本轮已落地。**

### 4. `plan` 是唯一的链路事实载体（目标状态，**未落地**）

要让前端彻底删掉那段 `stages` 兜底、并让 `isPlanning` 只判「有没有 plan」，需要三处跨线改动：

| # | 落点 | 内容 | 线 |
| --- | --- | --- | --- |
| 1 | `app/core/checkpoint.py` | `workflow_runs` 落编排模式，让「这次走哪条」在规划窗口内可读 | C |
| 2 | `app/orchestration/pipeline.py` + `app/workflows/pipeline.py` | 静态链路**开工即写一份计划**（`PIPELINE_STEPS` 那条常量链，来源 `fixed`），与动态「计划即落盘」同形 | A + B |
| 3 | `app/api/stage_trace.py` | `read_stage_traces` 按 `plan` 迭代，而不是按 `PIPELINE_STEPS` 硬编码（读键静态是 `{stage}`、动态是 `dyn:{id}`） | D |

未落地期间，**库里已有的静态历史运行**仍要靠 `stages` 兜底渲染 —— 那是**读历史数据的兼容路径**，
不是「另一套编排理论」；两件事不能混为一谈。

## 备选方案

- **把两条 Dapr workflow 合并成一条（`static` 也跑 `agent_dynamic` + 预置计划）。** 否决：动态链路的
  `/stages` 目前返回 `not_integrated`（ADR-030 §5），而静态链路有完整的逐阶段推进轨迹与工具调用；
  合并会让**所有静态运行的详情变空**，是能力退化而不是简化。真要合并，得先补齐动态侧的轨迹读侧。
- **照搬 Playground 的五种工作流模板选择。** 否决：ADR-030 的备选方案已论证——六种拓扑里只有两种能被
  现有执行结构表达，选型会产出执行不了的答案；且「该用哪种拓扑」的判据是评测数据，不是提示词。
  可借鉴的是它的**注册表绑定 + 一单一次的参与 Agent 勾选**，那一层的后端 ADR-036 已做，缺前端入口。
- **只改文案，不动数据。** 否决：前端仍持有两套链路理论（`plan` 一套、`stages` 一套），
  下一次改动还会在同一个地方分叉。

## 代价

- 「兜底」项在界面上仍可点：演示与故障排查需要一条确定性链路，所以保留入口，只把它降级。
- 目标状态落地前，`isPlanning` 仍要接提交方声明的模式（`requestedMode`）。这条**不可能靠后端推断
  解决**（规划窗口内没有任何字段可读），必须落库。
- `plan_source` 多出 `fixed` 之后，前端 `PlanStepSummary` 与 `doc/api.md` §5.x 的 `plan_source`
  口径要同步。

## 影响

- `frontend/src/App.tsx`：新增 `STRATEGIES` / `strategyOf` / `STRATEGY_MENU_ID` 与策略浮层
  （状态放在 `Workspace`，不放在 `App`——控件活在输入区）、Welcome 文案；删掉 `composer-mode` 分段控件。
- `frontend/src/styles.css`：`.composer-mode*` → `.composer-strategy*`（含 `is-secondary` 与箭头旋转）。
- `frontend/src/workspace/collaboration.ts`：新增 `PlanSourceKey` / `PLAN_SOURCE_TEXT` /
  `PLAN_SOURCE_HINT` / `planSourceKey()`。
- `frontend/src/workspace/CollabCanvas.tsx`、`frontend/src/records/RecordsPage.tsx`：角标与 chip 改口径。
- `frontend/rendercheck/workspace-smoke.tsx`：249 → **255** 条。
- `doc/api.md` §4.4 的 `orchestration_mode` 口径；`doc/testing.md` §4.25。

## 教训

「固定三步」这个词在仓库里同时指三件不同的事：**一条 Dapr workflow**、**一份 checkpoint 的形状**、
**一个用户可见的策略**。三者被同一个词盖住，于是任何一层的改动都会在另一层露出破口
（`checkpoint` 没有 `plan` → 前端只能自己拼链 → 拼出来的链又反过来固化成「另一种模式」）。
**判断「这是不是特例」的标准是数据契约，不是界面文案**：只要 `plan` 还不是唯一载体，
把按钮改成什么样都只是把特例挪了个位置。

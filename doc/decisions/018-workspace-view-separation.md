# ADR-018: 工作台视图职责三分（执行台 / 协作侧栏 / 记录页）

状态：已接受

## 背景

工作台同时出现了三个问题，根因是同一块区域被两件事共用：

1. **主决策 Agent 选择器是假选择**。`doc/api.md` §3 已写明后端不接受
   `decision_agent_id`，但 composer 里仍挂着一个下拉（「自动分配 / 信息收集 Agent /
   数据分析 Agent / 报告生成 Agent」），只改前端 state。用户点下去不会产生任何效果，
   却要在提交前做一个自己没有依据的判断——比不提供该选项更伤信任。

2. **点执行台卡片会改变整块侧栏**。卡片 `onClick` 写 `selectedStage` +
   `setInspectorOpen(true)`，而侧栏装的是**任务级**内容（协作成员、阶段执行、工具调用
   链路、指标采样）。于是「看某个 Agent」变成了「切换整块任务视图」，反向路径也成立
   （侧栏点协作成员会写回 `selectedStage`）。两件事被压成一个状态。

3. **侧栏重复渲染了任务记录的明细**。侧栏内嵌 `WorkflowInspection`（工具调用链路 +
   本次任务 Token 与指标采样），而 `doc/api.md` §5.5 规定「指标采样的前端入口是
   任务记录页」。同一份数据在两个页面各渲染一遍，且侧栏空间被分页表格吃掉。

此外侧栏没有任何**协作关系**可视化：用户看不到阶段之间的先后与等待关系。

## 决策

1. **删除主决策 Agent 选择器**。参与哪些 Agent 由编排层决定，不由用户指定。
   后端在编排层落地路由/规划节点之前，前端不再提供这个入口（`doc/api.md` §3）。

2. **执行台卡片点击打开「Agent 阶段详情弹窗」**（`workspace/AgentStageModal.tsx`），
   只反映该 Agent 的身份、模型绑定、阶段状态与检查点，**不再改变侧栏内容**。
   侧栏是任务级视图，不跟随单卡点击而变。

3. **侧栏改为任务级视图**（`App.tsx::Inspector`，标题「任务协作概览」）：
   整体状态、运行时长、参与 Agent 数、协作链路、任务用量。
   删除内嵌的 `WorkflowInspection`；逐条工具调用与采样明细回到任务记录页，
   侧栏只提供跳转入口。三块视图职责互斥，不重复渲染同一份数据。

4. **协作链路用「波次」模型表达**（`workspace/CollaborationGraph.tsx`）：
   **波内并行、波间串行**。当前后端是固定串行流水线，因此每波只有一个节点；
   编排层支持 fan-out / Human-in-the-Loop 波次后，只需把同波阶段放进同一个数组，
   视图与样式无需改动。这样「并行 / 串行 / 等待」三种关系用同一个模型就能表达。

5. **用量按采样原值展示，不累加**，口径与 `doc/api.md` §5.5 一致：同一指标出现多次时
   并排列出并标注「多次采样」，而不是求和。

6. **新增 `frontend/src/workspace/`** 承载工作台视图，组件按显式 props 驱动导出
   （不自己拉数据），以便 `frontend/rendercheck` 直接挂载验证。

## 备选方案

- **把选择器接上后端**（新增 `decision_agent_id` 入参）。否决：后端根本没有路由节点，
  参数无处消费；先做接口再补编排，等于把一个假选择升级成一个假功能。正确顺序是先有
  编排层的规划/路由节点，再决定要不要把「主导视角」暴露给用户。
- **保留侧栏的 `WorkflowInspection`，只在执行台点击时折叠它**。否决：违反 §5.5 的
  入口归属，且侧栏空间不足以承载分页明细。

## 影响

- `doc/api.md` §3、§7 已同步（移除「主决策 Agent 选择仍为预览」表述，新增三块视图的
  职责边界）。
- `frontend/src/records/Inspection.tsx`：删除 `WorkflowInspection`；`ToolCallRecords` /
  `RuntimeSampling` 保留，仍由任务记录页复用。
- `frontend/src/styles.css`：`.decision-control` / `.decision-agent-selector` /
  `.decision-menu` 三条规则替换为 `.composer-hint`。
- 新增 `frontend/rendercheck/workspace-smoke.tsx`（工作台视图的离屏渲染冒烟）。
- **本决策未解决、需要其他成员跟进的部分**：
  - 逐 Agent 的思维链与阶段原始输出——当前阶段正文只存在于 Dapr State Store 与活动
    输出中，ADR-010 §4 明确行为日志不记正文。要让弹窗展示推理过程，需 **C** 在
    `app/observability` 侧把 ReAct 中间步骤按阶段落库，并新增只读端点。
  - 动态 Agent 集合与并行波次——需要 **A** 在 `app/orchestration` 增加规划/路由节点与
    conditional edge / fan-out，**B** 在 `app/core/checkpoint.py` 的 `workflow_runs`
    补波次与依赖字段。字段到位后 `CollaborationGraph` 只换 `waves` 入参即可。
  - 每阶段 Token 归属——依赖 **C** 的采集侧在 `metrics.labels` 里稳定写入 `agent_id`；
    前端已按该 label 分组，缺失时退化为「任务级」。

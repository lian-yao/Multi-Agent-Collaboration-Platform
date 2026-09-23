# ADR-032: 运行期实时进度的两处补齐（dynamic 维护 `current_step`、画布画出规划节点）

状态：已接受

## 背景

§5.22 上一轮定了「计划即落盘 / 每步完成即推进 / 工具调用即落库」三条契约，前端据此做
「近似流式」的执行活动。用户复核实测时逐条回查，发现契约与实现之间有两处缺口——两处都是
**「代码写了、但判定永远不成立」**，而不是「没写」：

1. **`current_step` 动态链路从来不写。** 静态链路由 `_record_checkpoint` 写
   `current_step=step.value`（`app/workflows/pipeline.py`）；动态链路的
   `dynamic_plan_activity` / `dynamic_progress_activity` 两处都硬写 `"current_step": None`，
   而 `workflow_runs.current_step` 这一列**全仓没有任何地方为 dynamic 写过**。
   偏偏 `RunActivity.tsx:164` 的
   `activeStep = workflow.checkpoint?.current_step ?? workflow.current_step ?? ""`
   是两处判定的**唯一输入**：

   | 判定 | 落点 | 指针缺席时的实际后果 |
   | --- | --- | --- |
   | `isLiveStep` | `RunActivity.tsx:246` | §5.22 第四条「轮询即可让工具调用逐条长出」**一条都不显示** |
   | `isOpen` | `RunActivity.tsx:174` | ADR-031 §4「正在跑的那一步默认摊开」**不生效** |

   上一轮按 `dynamic_graph.py:118` 的 docstring「不设单值指针，改用 `plan[].status`」实现。
   那个说法对**步骤状态**成立，但 `current_step` 承担的是「**现在轮到谁**」——而
   `plan[].status` 里没有这一位：进度活动只写**已完成**的步骤，正在跑的那一步仍是
   `pending`。所以它不是冗余字段，是漏了。

2. **画布只画了分配的结果，没画分配的依据。** 动态链路的画布已经用「计划步骤 → 角色节点」
   表达了谁参与、谁依赖谁，但「任务分配（planner）」这件事有两半：**分给谁**（已有）与
   **凭什么这么分**（`checkpoint.plan_rationale`）。后者此前只出现在对话流的执行活动卡片里，
   画布的图上因此读不出「这一步为什么派给它」——而画布本来就是「任务级」那个时间面
   （ADR-031 的四视图分工）。

## 决策

### 1. `current_step` 是两条链路都要维护的契约字段

写进 `doc/api.md` §5.22，作为独立一条（不再是「每步完成即推进」的附注）。
判据很简单：前端只认**指针相等的那一步**，指针缺席时「默认摊开」与「实时工具调用」两条
同时失效——而这两条正是 §5.22 与 ADR-031 承诺的东西。

### 2. 指针的写法是「上一步落盘时推到下一步」，不是「下一步开跑时写」

静态链路已经是这个口径（`_record_checkpoint` 在阶段跑完后写，写的是**下一个**阶段）。
动态链路照抄，**不新增活动调用**：

- `dynamic_plan_activity`：写首步（计划刚产出，第一步就是「现在轮到谁」）；
- `dynamic_progress_activity`：写「**第一个还没出结果的步骤**」（`next(... not in results)`），
  跑完最后一步时为 `None`（终态归零）。

跳过的步骤在父工作流里已经写进 `results`，因此不会被算成「下一个」——这一点不能靠
「按顺序取下一个」实现，否则指针会停在跳过的那一步上。

### 3. 画布引入第四种节点 `planner`

`PlacedNode.kind` 从 `agent | start | end` 扩成四种。规划节点排在**任务端子之后、首个角色
节点之前**，是一条 `任务 → 任务分配 → 各角色 → 交付` 的链。

它是**决策环节**，不是一个 Agent：所以圆面固定用「规划」图标（复用
`AgentGlyph` 的 `plan` 键，与配置页、与 ADR-029 §4 的「其余 → 角色图标」那一档不冲突），
状态恒为 `completed`（计划画得出来就说明它已经跑完了），不参与 ADR-029 §4 的状态优先规则。

> **规划节点什么时候才画出来：** 它的存在依赖 `checkpoint.plan`，而计划是规划成功后
> 一次性落盘的。在那之前画布**一个节点都不画**、改报「正在规划」，不拿写死的固定三步
> 顶上——见 [ADR-034](034-no-chain-until-plan-lands.md)。

### 4. 规划节点不进波次计算

`graph.waves` 仍由 `depends_on` 算出，规划节点只是**多占一行**。理由：波次是「哪几步能同时
跑」的表达（ADR-030），规划节点不属于任何一个可执行波次；把它塞进第 0 波会让所有根步骤
看起来落在同一波、把「并行」画成假的。布局里的补偿仅两处——`rows + 1` 与
`waveRow(i) = i + 2`。

### 5. 静态链路不画规划节点（给 `null`，不是空壳对象）

静态链路的步骤是写死的三步常量，没有「谁被派了活」这个决策。`CollabGraph.planner` 因此是
`null`，画布据此决定**画不画**这个节点。给一份空的 `assignments` 会得到图上多一个点不开的
节点——比没有更糟。

## 备选方案

- **在前端用 `plan[].status !== "completed"` 反推「正在跑哪一步」。** 否决：同时待跑的有
  多步（串行链路上全都没跑），反推会同时点亮多步；且这一步是「猜」，而 `current_step` 是
  服务端**已经掌握的事实**（编排循环里 `step` 就是它）。契约字段该写就得写。
- **每步开跑前额外调一次进度活动（写 `current_step`）。** 否决：白多一次 Dapr 活动调用，
  而且与静态链路的口径不一致（静态不等下一步开跑）。「上一步落盘时推到下一步」已经满足
  「运行中的那一步指针指向自己」。
- **把规划节点的理由放进画布顶部当作一行文字，不加节点。** 否决（本轮用户选择加节点）：
  那样理由是画布的**注脚**，不是图的一部分；「谁被派了活」有了节点而「凭什么」没有，
  读图时仍然要离开图去找。节点 + 悬停浮层让两者同处一个位置。
- **规划节点参与波次（当作第 0 波）。** 否决：见决策 4，会把串行画成并行。
- **把 `plan_rationale` 也塞进每个角色节点的浮层。** 否决：同一句话重复 N 遍，而且它讲的
  是**整份计划**，不是某一步。

## 代价

- **`current_step` 多了一处需要两条链路同时记得维护的写入。** 漏写不会报错，只会让前端的
  两条判定静默失效——这正是本轮要修的病根。缓解：动态链路的写入点收敛在
  `dynamic_progress_activity` 一处（父工作流只负责调用它），并有单测钉住「首步写入」
  「下一步写入」「全部跑完归零」三种情形。
- **`workflow_runs.current_step` 终态不清零**（`update_workflow_run` 忽略 `None`）。
  与静态链路一致：终态时 `checkpoint.current_step` 为 `null`，行上残留最后一个步骤 id，
  而前端两条判定都要求 `status` 处于运行态，因此无影响。要真正清零得改 B 的
  `update_workflow_run` 语义（用哨兵值区分「不改」与「置空」），不值当。
- **画布多一行，侧栏也高了一行**（`dense` 与 `wide` 共用同一份布局）。这不是「顺便」，
  是同一张图的两种密度，只改一侧会让两个视图结构不一致。
- **第四种节点种类会让后续任何「按 kind 分派」的代码多一个分支**（`NodeGlyph`、
  `tabIndex`、`aria-label`、浮层均已分派）。新节点加入时漏改某处是静默的，因此冒烟里三条
  断言分别钉住「模型带出 planner」「布局多一行且波次不变」「浮层写出理由与逐条分工」。

## 影响

- `app/workflows/dynamic.py`：`dynamic_plan_activity` 增 `current_step=<首步>`（行 + checkpoint）；
  `dynamic_progress_activity` 增 `pending_step` 计算与 `current_step=` 写入。跨 B 线，
  已登记 `分工.md` §4。
- `doc/api.md` §5.22：三条契约扩成四条，新增「`current_step` 指现在轮到谁」，并把
  「工具调用逐条长出」与它显式挂钩。
- `frontend/src/workspace/collaboration.ts`：新增 `CollabPlanner` / `CollabAssignment`，
  `CollabGraph` 增 `planner` 字段。
- `frontend/src/workspace/GraphCanvas.tsx`：`PlacedNode` 增 `planner` kind 与字段；
  布局增一行与规划节点；新增 `PlannerDetail` 浮层；`NodeGlyph` 增规划分支。
- `frontend/src/workspace/workspace.css`：`.cv-node.kind-planner`（虚线边 + 中性石板灰，
  **不跟角色节点抢蓝色**）。
- 冒烟：`workspace-smoke` 201 → **210**（新增 9 条：模型 2、布局 3、渲染 4）。
- 测试：`tests/unit/test_workflow_dynamic.py` 增 1 例、两例加断言（73 → 79，与本轮其它文件同跑）。
- `doc/testing.md` §4.17；`分工.md` §4。

## 教训

两处缺口的形状是同一个：**「实现存在」被当成了「功能生效」**。`isLiveStep` 是写好的、
`plan_rationale` 是落库的、桥接它们的字段却没人写——而缺的是**一个布尔条件的输入**，
不是一段逻辑，所以代码审查与单元测试都很难发现它：测试断言的是「活动写了什么」，
而出问题的是「前端读的字段有没有值」。

判据应该换一种问法：**这条链路上有没有一个字段是「所有人都读、但没人写」的？**
`current_step` 恰好就是——两条链路都读它、静态链路写它、动态链路从没写过，
而两个视图的这一半功能就都静默地关着。回查时按「读方 → 写方」的对照表走一遍，
比按功能逐个点要快得多。

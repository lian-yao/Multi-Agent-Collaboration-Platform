# ADR-034: 计划未落盘时不画链路、减动画下加载指示器保留动感

状态：已接受

## 背景

用户 2026-09-22 的第二轮反馈有两条，指向两个不同的东西：

1. **加载图标是静态的**——「效果不好，需要体现动感」。
2. **画布先画一版错的，再换成对的**——「画布任务编排似乎是默认绘制经过 3 步？我测试了单
   agent 问题，页面依然默认绘制了 3 步 agent 的画布链路，之后才更新」。

第 2 条比第 1 条严重：它不是「样式不好看」，而是**界面在事实产生之前先给了一版假事实**。
用户的原话已经把要求说全了——「按理来说没有规划之前，应该没有画布，而是应该显示正在规划，
agent 规划完毕才根据事实工作流的数据渲染画布」。

### 病根 1：`@media` 把全站的动画一刀切了

`styles.css` 里有一条给减动画偏好用的全局规则：

```css
@media (prefers-reduced-motion: reduce) {
  *, *:before, *:after { animation:none!important; transition:none!important; ... }
}
```

它本意是给「不想看动效」的人用，但 `!important` 加通配符的写法把**功能性动画**也一起杀了——
加载指示器（`.spin`）靠 `animation` 转圈，被掐掉之后就是一张静止的图标，「正在跑」看起来
和「卡住了」一模一样。CDP 实测坐实了这一点（打的是线上容器）：

| 模拟媒体 | `.spin` 的 `getComputedStyle().animationName` |
| --- | --- |
| 不覆盖（默认 `no-preference`） | `spin` |
| `prefers-reduced-motion: reduce` | `none` |

### 病根 2：静态无 `plan` 与「计划还没落盘」被读成了同一件事

`buildCollaboration` 的既定行为是：`checkpoint.plan` 缺席时退回 `STAGE_META` 的固定三步
（collect / analyze / report）——这是给**静态链路**写的，静态链路确实没有规划环节，那三步
就是它的全部。

但动态编排的 `checkpoint` 是**规划节点规划成功后一次性写入**的（计划与 `mode` 同一次落盘）；
在那之前 `create_workflow_run` 只建了行、`checkpoint` 是 `null`。于是前端看到的「对象里没有
`plan`」同时对应两种完全不同的实情：

- **静态链路**：没有规划环节，固定三步就是事实 → 该照画。
- **动态链路、规划窗口内**：连「几步、派给谁」都还不知道 → **不该画**。

把后者当前者，界面上就是「先画一版错的、等计划落盘再换成对的」。用户单 agent 的测试之所以
「默认画了 3 步 agent」，正是因为退回逻辑拿固定三步顶上了。

## 决策

### 1. 规划窗口内不画链路，报「正在规划」

新增判据 `isPlanning(workflow, requestedMode)`（`frontend/src/workspace/collaboration.ts`），
三条同时成立才算规划窗口：

| 判据 | 为什么是它 |
| --- | --- |
| 提交时声明的是动态编排（`requestedMode === "dynamic"`） | 规划窗口内服务端**没有任何字段**能说明这次走的是动态编排——`mode` 是随 `checkpoint` 一起写的，而 `checkpoint` 还是 `null`。唯一知道这件事的只有提交方自己 |
| `workflow.checkpoint` 为空 | 一旦有值就说明链路已定型（动态链路的 `mode` 与 `plan` 同一次写入，有 `mode` 必有 `plan`） |
| 状态仍在 `pending` / `running` | 终态却没有计划属于数据残缺，不该被读成「还在规划」 |

命中时 `CollabGraph` 返 `{ planning: true, nodes: [] }`，**一个节点都不画**：侧栏画布、全屏
画布、执行台三处都报「正在规划」，等计划落盘后自然切换成按 `planSteps` 画出的真实链路。

### 2. 「没有链路」与「还不知道链路」在界面上要分开说

`planning: true` 时 `nodes` 为空，但这**不是**「这次没有节点可画」（那是终态），而是「现在还
不知道要画什么」（那是过程）。所以不复用空状态的文案（「暂无」），而是单独写
「正在规划：任务分配还没产出，链路等计划落盘后再画」。

### 3. `requestedMode` 只给实时工作流传

判据一依赖「提交时声明」，而当前那个编排模式开关反映的是**此刻**的选择。历史工作流的模式要
按它自己的数据判断，借用当前开关会把静态老任务读成「正在规划」。因此全屏画布传
`requestedMode: isLive ? mode : undefined`——只有正被跟随的那次才传。

### 4. 减动画下加载指示器是唯一例外

`@media (prefers-reduced-motion: reduce)` 的通配规则保留（正文渐进揭示等仍要降级），但给
`.spin` 开一个口子，改用**不引动前庭反应的呼吸式透明度**：

```css
.spin { animation:spin-breathe 1.4s ease-in-out infinite!important }
@keyframes spin-breathe { 0%,100% { opacity:1 } 50% { opacity:.35 } }
```

理由是「减动画」的意图是**避开位移、缩放、旋转**这类会引起不适的运动，而不是「屏幕上的任何
东西都不许变」；静态的转圈图标会被读成「卡住了」，把「正在跑」这个状态本身弄丢——那是功能性
信息，不是装饰动效。

## 备选方案

- **照画固定三步，但画成灰色 / 虚线「占位」。** 否决：位置、数量、角色名都还是错的，读图的
  人仍会把它当成这次的链路；「先给一版错的」这个病根一点没动。
- **用服务端某个字段判「是不是动态」。** 否决：规划窗口内服务端没有这样的字段——这正是本
  ADR 要绕开的地方。要服务端先说话，得让 `create_workflow_run` 就写 `mode`，属数据契约变更，
  不该由前端等待。
- **减动画下干脆把 `.spin` 藏掉 / 换成「加载中」文字。** 否决：藏掉就是没有指示器；换文字要
  为每处调用方各写一套，且「在动」这个信号仍然缺失。
- **让 `.spin` 走 `transition` 而不是 `animation`（绕开通配规则）。** 否决：`transition` 加在
  `transform` 上一样是位移 / 旋转，同样该被减动画偏好排除；换成透明度才是真正的「不引动前庭」。
- **把判据三（终态）也去掉，只看 `checkpoint` 为空。** 否决：终态为空是数据残缺，报「正在
  规划」会把残缺说成进行中，掩盖真问题。

## 影响

- 改动前端文件：`workspace/collaboration.ts`（`isPlanning` + `CollabGraph.planning` +
  `buildCollaboration` 的 `requestedMode`）、`workspace/CollaborationGraph.tsx`、
  `workspace/CollabCanvas.tsx`、`workspace/RunActivity.tsx`、`App.tsx`（三处接线 + 执行台）、
  `styles.css`（`.spin` 例外 + `.run-chain-head.is-planning`）、`workspace/workspace.css`
  （`.cv-planning`）。
- `frontend/rendercheck/workspace-smoke.tsx`：加 `draftWorkflow`（`status: running`、
  `checkpoint: null`）与规划窗口 / 静态 / 未声明三组断言，以及减动画 `.spin` 例外、`.cv-planning`
  两条静态断言（214 → **224**）。
- `doc/api.md` §7 对话流、执行台与任务协作侧栏三条同步；`doc/testing.md` §4.19 记本轮验收；
  `doc/decisions/032-live-progress-current-step-and-planner-node.md` §3 加一行交叉指引
  （规划节点什么时候才画出来），**那条决策本身未改**。
- **未改的**：ADR-028 的画布几何与浮层口径、ADR-032 的 planner 节点与 `current_step`、
  §5.17 轨迹契约、后端与 Dapr 链路——本次是前端的「什么时候画」与一个 CSS 例外。
- 判据一用到的 `orchestration_mode`（§4.4）是既有提交字段，未新增接口字段；`checkpoint`
  `null` 的语义是既有行为，本 ADR 只是第一次让它决定「画不画」。

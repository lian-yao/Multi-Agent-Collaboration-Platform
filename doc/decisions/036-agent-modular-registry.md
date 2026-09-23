# ADR-036：Agent 模块化 —— 角色目录驱动调度、人设可配置、目录字段与覆盖值分端点

日期：2026-09-23 ｜ 状态：已实现 ｜ 关联：ADR-013/017/034/035，`doc/api.md` §5.7

## 背景

三个内置角色（collector / analyst / reporter）事实上是**内置化**而非**模块化**的：

1. 动态编排的 planner 候选集写死为 `RoleId` 三选一，登记一个新角色也永远不会被调度；
2. 角色没有可编辑的 system prompt 与图标——执行期人设只能来自 `app/agents/roles.py`；
3. `agent_registry` 表里 `system_prompt` 已有列但 API 不暴露、前端不可编辑。

## 决策

### 1. 内置三角色降级为普通种子

`builtin` 只保留一个含义：「不可删除」（`DELETE` 返回 `409`）。`seed_builtin_agents`
缺失即插入、已存在**只回填空值**（不回填 `enabled`，避免覆盖用户的停用操作）。

### 2. planner 候选集由注册表驱动

`agent_config.dispatchable_agents()` 返回目录里 `enabled=True` 的条目；workflows 层把它
作为 `candidates` 传给 `generate_plan`（进提示词，也进 `parse_plan` 的 `allowed` 校验——
模型编造候选之外的角色整份计划丢弃）。`PlanStep.role` 从 `RoleId` 改为自由文本键。
候选集为空（目录不可用/全部停用）时回退内置三角色候选——动态编排不能因为目录一层
不可用就失去候选。**停用只作用于动态调度**：固定三步流水线的角色是拓扑的一部分，
不受 `enabled` 影响（fallback 三步同理）。

### 3. 人设三级回退：目录 > 内置定义 > 通用兜底

`resolve_step_prompt(role, catalog)`：目录条目的 `system_prompt` 非空即用；否则内置角色
用 `ROLE_DEFINITIONS`；自定义角色没写过 prompt 用通用兜底。**不在 API/执行层编默认值**
——「没配置」必须与「配置了默认值」分得开，响应里 `null` 就是 `null`。

### 4. 目录字段与覆盖值分两个端点

| | `PATCH /api/v1/config/agents/{id}`（覆盖组） | `PATCH /api/v1/config/agents/{id}/profile`（目录组，新增） |
| --- | --- | --- |
| 写哪张表 | `agent_configs` | `agent_registry` |
| 字段 | 模型绑定 / 调参 / 工具白名单 | name / description / system_prompt / icon / enabled |
| 显式 `null` | 清除覆盖，**回退下一层** | 清空那一列，**没有回退链** |

`null` 的含义在两组里读法一致（省略 = 不动），但**后果**不同（回退下一层 vs 清空），
前端要给的提示文案也不同；且 UI 的两个保存动作不该互相覆盖对方未提交的字段。
分开端点让语义由结构承担。`icon` 只校验形状（`^[a-z][a-z0-9_]*$`，≤32 字符）不查
图标集白名单——图标集在前端，后端维护枚举会把「加一个图标」变成两处改动。

### 5. 工具目录的「内部工具」升为一等分区

`/api/v1/tools`（内置 + 已发现）原本藏在 MCP 页底部且需手动「读取目录」。现在配置页
新增「内部工具」分区，进入即读取；MCP 页只管 Server。分组口径与角色工具授权面板共用
`buildToolCatalogGroups`。

## 后果

- 新增列迁移：`agent_registry.icon VARCHAR(32)`（`_REGISTRY_COLUMN_MIGRATIONS`，第 7 条）。
- `AgentResponse` 新增 `system_prompt` / `icon`（可缺键，旧响应兼容）。
- 前端角色弹窗改为「设定 / 调度 / 工具」副路由；卡片 `minmax(380px, 1fr)`。
  - **副路由的互斥靠 `hidden` 属性**，而「设定」与「调度」都挂着 `.cfg-form-grid{display:grid}`：作者样式里的 `display` 优先级高于 UA 样式表的 `[hidden]{display:none}`，所以 `config.css` 必须显式写 `[hidden]{display:none!important}`，否则两个分区会一直同时可见（2026-09-23 用户报「副路由根本没区分」的真实原因；「工具」那段没挂这个类，才显得只有它「多了一块」）。
  - **图标入口是弹窗头部的头像**：点头像在同一处弹出浮层挑图标（`iconPickerOpen`），不再把 15 格常驻在「设定」里。浮层要自己在**捕获阶段**拦 Esc 并 `stopPropagation` —— `Modal` 在 window 上挂了冒泡期的 Esc 关整个弹窗，不拦就会「想收浮层、结果连弹窗带草稿一起关」。
  - 头像位的 `.cfg-agent-avatar` 在**卡片网格里也有一个**（纯展示），所以「可点」那套样式挂在 `.cfg-agent-avatar-slot` 槽位下，不能裸写。
- 停用全部角色时动态编排仍可用（回退内置候选），这是有意的取舍而非遗漏。

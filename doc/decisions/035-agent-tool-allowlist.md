# ADR-035: 角色工具白名单的接缝放在注册表层

状态：已接受

## 背景

用户 2026-09-22 的反馈里有一条是关于**工具配置的缺失**：

> 「该项目和 obsidian-yolo-reference 的 agent 配置都有工具配置，当前项目进度并没有，
> 按理来说应该这样设置……似乎只有 mcp 工具，缺少 agent 内置工具的启动。」

这句话里有三层意思，前两层是事实、第三层是误判，值得分开说：

1. **参考项目确实有 per-role 工具配置**：`Multi-Agent-Playground` 的 `AgentDefinition` 用
   `skill_ids` + `builtin_capabilities`（`filesystem` / `fs_list` / `fs_read` / `fs_write`）
   限定每个角色能碰什么。这不是「界面缺一个开关」，而是**数据模型里少一个维度**。
2. **本平台确实只有全局目录**：`app/mcp/registry.py::build_tool_registry()` 返回全量注册表，
   `GET /api/v1/tools`（§5.3）列的是平台全部工具。**执行期每个角色拿到的都是同一份注册表**
   ——「collector 能用哪些工具」这件事在库里、在 API 里、在编排层里都不存在。
3. **「只有 MCP 工具」是误判，但误判有依据**：内置工具（calculator / web_search /
   code_execution / sql_query）一直都在注册表里、也在 `/api/v1/tools` 里；但「工具与配置」
   页展示它们的容器是 **MCP 面板**（`.cfg-tool-card` 那一套是为 Server 分组写的），
   所以内置工具读起来像是「某个 MCP Server 的工具」。缺的不是内置工具本身，是
   **一个以「角色」为主语的工具视图**。

### 病根：授权这件事没有落点

要做 per-role 授权，直觉做法是在调用点上过滤——`ToolCaller` 拿到调用请求后查一次白名单，
不在名单里就拒绝。这个做法有一个**静默的旁路**：

> 重放与历史消息走的是另一条入口。`ToolCaller.invoke` 被复用时（恢复、重放、历史轨迹回看），
> 白名单过滤不在这条路径上，于是「已经发生过的越权调用」会被原样放行；更糟的是，
> **模型侧看到的工具清单**仍是全量——它照样会去调一个它其实没被授权的工具，
> 然后拿一个错误回来，把「没授权」变成「调了但失败了」。

另一个直觉做法是把白名单塞进 `resolve_agent_settings` 的层级合并链。这会把
`tool_names` 的 `NULL` 变成「回退到下一层的值」，而这条链的下一层
（`provider_configs` 的 legacy 五列）里根本没有工具概念——**语义对不上**：
「未配置」在数值字段里是「继承上层」，在授权里只有一种意思：不限制。

## 决策

### 1. 收窄发生在注册表层，不是在调用点上

新增 `ToolAllowlistRegistry`（`app/orchestration/tools.py`），实现既有 `ToolRegistry` 协议，
把 `list_tools` 收窄到白名单、`call` 对白名单外的名字抛 `ToolNotAuthorizedError`。

这一个选择同时解决了两件事：

- **模型看到的清单就是白名单**：`list_tools` 收窄之后，工具定义根本不会进模型上下文，
  不会出现「看得见但调不动」的错配；
- **没有旁路**：任何走这张注册表的调用（包括重放）都受同一层约束，因为约束在表上，
  不在某个调用点。

`ToolNotAuthorizedError` 继承 `PermissionError`，被 `ToolCaller.invoke` 归一化成一条
`failed` 的工具调用记录——越权尝试会**留下痕迹**，不是静默消失。

### 2. 三态语义：`None` / `[]` / 非空，`None` 必须与 `[]` 分开

| 值 | 含义 | 执行期 |
| --- | --- | --- |
| `None`（未配置） | 不限制 | 沿用全量注册表 |
| `[]` | 显式取消全部授权 | 一个工具也用不了 |
| 非空数组 | 白名单 | 只放行这些 |

`None` 与 `[]` 的区别是这次改动里**最容易做错的一处**：前端把「不受限」渲染成
「全部勾选」，后端如果读成 `[]`，用户点一次保存就把角色的全部工具收掉了。
所以：

- `restricted_registry(registry, None)` **原样返回同一个对象**（不是包一层恒真的白名单），
  加这个字段之前的行为逐字保留；
- `PATCH` 里 `null` 与「省略」都是「未配置」，「恢复不受限」必须显式传 `null`，
  不能指望传空数组（§5.7）；
- 界面上「不受限」与「按名单选了全部」是两种状态，且第二种会**说明自己是快照**
  （新登记的工具不会自动授权）。

### 3. `tool_names` 不进层级合并链

`OVERRIDE_FIELDS` 里加了它（这样 `override_keys` 会如实列出「这个角色配了白名单」），
但 `resolve_agent_settings` 不读它，它只被 `resolve_agent_tools` 读取。理由见背景最后一节：
授权没有「上一层」可继承。

### 4. 不校验名字是否存在于当前工具目录

`_validate_tool_names` 只做形状校验（数组、去重保序、单名 ≤ 120、总量 ≤ 60），
**不查目录**。这与 `llm_model_id` 的处理刻意不同（后者悬空要报 422）。

差别来自「悬空」的成因：`llm_model_id` 悬空意味着用户引用了一个被删掉的条目，是**配置错误**；
工具名的「找不到」绝大多数是 **MCP Server 此刻离线 / 还没被发现**，是**运行期状态**。
按目录校验的话，用户保存一份完全合法的白名单会因为它依赖的服务当前不可达而失败——
这是把运行期状态混进了配置校验。

代价要写明：名字拼错会以「配置成功但工具没出现」的形式潜伏。界面对此的兜底是把
「名单里有、目录里没有」的名字**单独成组列出来**，并且**保存时原样保留**（不能悄悄丢掉，
否则用户点一次保存就改了没碰过的配置）。

### 5. 会话级附件工具不进白名单

`list_session_files` / `read_session_file`（ADR-025）**不受 `tool_names` 管辖**，
因此收窄必须发生在 `session_scoped_registry` **之前**。理由是它们不进静态工具目录
（§5.3），所以也不会出现在白名单编辑界面里——如果它们受管辖，界面上就会出现一个
「关不掉、又只在有附件的会话里存在」的开关。

## 备选方案

- **在 `ToolCaller` 上加过滤。** 否决：见背景「授权这件事没有落点」——重放与历史消息
  是旁路，且模型仍看得见全量工具。
- **把 `tool_names` 并进 `resolve_agent_settings` 的层级合并。** 否决：那条链的下一层
  （legacy 五列）没有工具概念，`None` 会被解成「继承上层」，语义对不上。
- **在 `AgentResponse` 里只给布尔 `tool_restricted`，不给名单。** 否决：前端要把
  「不受限」渲染成整份目录的勾选态，就需要知道名单内容；而且用户要能核对「到底授权了哪几个」。
- **按目录校验名字（有目录就 422）。** 否决：见决策 4，会把运行期状态混进配置校验。
- **把白名单做成角色表的列而不是 `agent_configs` 的覆盖字段。** 否决：它会与其余六个
  覆盖字段的「省略 / `null` / 值」三态语义不一致，而同在一张表、同一套 PATCH 契约下
  反而更容易讲清楚；代价是必须在文档里单独说明它不参与回退（已写进 §5.7）。

## 影响

- **后端**：`app/core/checkpoint.py`（增列 + 迁移）、`app/core/agent_config.py`
  （校验 + `resolve_agent_tools`）、`app/orchestration/tools.py`（纯新增三个符号）、
  `app/workflows/pipeline.py` 与 `dynamic.py`（各一行收窄）、`app/api/main.py`
  （`tool_names` 的读写 + `WorkflowResponse.error` 的补入）。
- **前端**：新增 `frontend/src/config/AgentTools.tsx`；`AgentPanel.tsx` 接面板、
  卡片增授权摘要；`config/config.css` 增授权面板样式（行样式复用既有 `.cfg-tool-card`，
  **没有另造一套**）。
- **契约**：`doc/api.md` §5.7（三态表、字段说明、错误码、解析优先级）、§5.3
  （加一句「目录 ≠ 某个角色能用什么」）。**`doc/api.md` 的 `PATCH` 语义未变**：
  仍是「省略 = 不改动、显式 `null` = 清除」。
- **测试**：`test_pipeline_tools.py` 7 例、`test_agent_config.py` 5 例、
  `test_config_api.py` 2 例；`config-smoke` 增 19 条断言。验收见 `doc/testing.md` §4.20。
- **未做**：`GET /api/v1/tools` 仍是全局目录（白名单只在执行期生效）；
  「白名单名字拼错」不做校验（决策 4）；自定义角色仍不进固定三步流水线（既有约束）。
- **跨线登记**：改到了 B 的 `app/core/agent_config.py` 与 `agent_configs` 表结构、
  A 的 `app/orchestration/tools.py` 与两条 workflow 链路，已记入 `分工.md` §4。

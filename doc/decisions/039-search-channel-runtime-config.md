# ADR-039：搜索渠道提升为运行期可写配置，环境变量退为兜底

日期：2026-09-24 ｜ 状态：**已落地** ｜ 关联：ADR-037（搜索渠道与豆包契约）、ADR-032（本地搜索网关）、ADR-034（出网策略）、ADR-015（配置页无令牌）、`doc/api.md` §5.24、`doc/testing.md` §4.28

## 背景

用户反馈（附报错截图）：`web_search` **经常**报

```
未配置豆包搜索 API key（TOOL_SEARCH_API_KEY）
```

而按设计意图，一键部署本该走本地网关（ADR-032）、不需要任何凭据。两个问题叠在一起：

1. **部署默认值错了，而且错在版本库外面**。`deploy/.env` 里是
   `TOOL_SEARCH_PROVIDER=volcengine` 且 key 为空，于是每次部署出来都是「选了豆包、没给
   key」，工具按 ADR-037 §2 直接拒绝执行。而 `.env` 被 `.gitignore` 的 `.env` 规则挡掉，
   **不在版本控制里** —— 改本机那份只救本机，新克隆 / 新机器照样错。
   **修 `.env` 不算修好**：部署默认值必须落在仓库里。
2. **渠道不可运行期改**。ADR-037 把渠道做成了 `TOOL_SEARCH_PROVIDER` 这**一个环境变量**，
   而 env 只在容器创建时注入 —— 换渠道 = 改 `.env` + 重建容器。界面上既看不到当前渠道、
   也换不了，配错只能靠报错发现。

用户确认的方向：**默认走本地（修根因）+ 加一个渠道开关（运行期可改）**。

## 决策

### 1. 两层配置：环境兜底，覆盖优先

新增单行表 `tool_configs`（`id='default'`，`app/core/checkpoint.py::ToolConfigRecord`），
三列 `search_provider` / `search_endpoint` / `search_api_key`。合并规则一句话：

> **生效值 = 环境配置（`TOOL_*`） ← 覆盖行中非空的字段。**

`app/tools/config.py::get_tool_settings()` 改为 `effective_search_settings(env_tool_settings())`；
`env_tool_settings()` 保留**纯环境**基线，供界面回答「这个值从哪来」
（`env_provider` / `env_endpoint` / `env_api_key_configured`）。拿 `get_tool_settings()`
当基线会把生效值显示成环境值，界面就再也分不清来源了。

### 2. 一键部署的默认值写进 compose，不写 `.env`

`deploy/compose.yaml` 的默认值改为 `duckduckgo` + `http://search-gateway:8800/search` + 空 key，
`.env` 只用于**覆盖**它。理由就是背景第 1 条：`.env` 进不了版本库，写在那里等于没有默认值。

验收时把本机遗留的 `deploy/.env` 删掉再测一遍 —— **留着它就分不清是 compose 默认生效了
还是 `.env` 生效了**，而那正是本节要证明的事。

### 3. 写入必须失效**两层**进程级缓存

`get_tool_settings()` 是 `@lru_cache` 的；`app/mcp/registry.py::build_tool_registry()` 又按进程
缓存**已经构造好的工具实例**，而 `WebSearchTool.__init__` 在构造时就把当时的 settings 存进了
实例。只失效前者，注册表里那个旧实例还攥着旧 settings —— 结果是**界面显示「已保存」而实际
不变**。这种「保存成功但不生效」最难排查，所以 `_invalidate_tool_caches()` 两层一起失效，
做法与 `app/core/mcp_registry.py::_sync_orchestration_tools` 保持同一套。

### 4. 换渠道必须同时换端点

`search_endpoint` 只是**地址**，解析契约由 `search_provider` 决定（`duckduckgo` 走 GET +
`Abstract`/`RelatedTopics`；`volcengine` 走 POST + `Bearer` + `Result.WebResults`）。而部署期的
环境端点恰恰是给**某一条**渠道用的（本地网关就是 DuckDuckGo 契约）。所以写入时若「改了渠道」
且「没有给端点」，服务端替它落一个：

| 情形 | 落哪个端点 |
| --- | --- |
| 环境渠道 == 新渠道 | **环境端点** —— 它一定是给这条渠道用的，所以「从豆包切回 DuckDuckGo」能正确落回本地网关，而不是没被墙的公网 DDG |
| 否则 | 该渠道的**官方默认端点** |

并且 `endpoint` 的**缺省 / 空串 / 显式 `null` 在换渠道时是同义的**，都当作「用这个渠道的默认
地址」。前端每次保存都会带上 `endpoint` 字段，若把空输入框原样发成 `""` 而不当一回事，换渠道
就会留下**上一条渠道**的端点 —— 拿着豆包的 key 去打只认 DuckDuckGo 契约的网关，而错误信息会
指向完全无关的地方。

还有一处由 compose 语义带来的错配要交代：`${VAR:-default}` 在变量为**空串**时也取默认值，
于是「只把 `TOOL_SEARCH_PROVIDER` 改成 volcengine、端点不动」会拿到那个只认 DuckDuckGo 契约的
网关地址。它照用只会以**解析错**失败，而报错信息指向完全无关的地方。

**这里刻意不做静默纠正。** 第一版曾在校验器里把端点改写成豆包默认，结果踩掉了成员 C 已冻结的
显式值优先契约（`tests/unit/test_search_tool_volcengine.py::test_explicit_endpoint_wins_over_provider_default`）
—— 校验器分不出「有意把这条渠道接到自建网关上」和「端点只是取了默认值」，做不到区分就不该替人
改。改为 `ToolSettings.endpoint_provider_mismatch()` 诊断 + `WebSearchTool` 构造时记一条警告：
**显式值一律尊重**，错配留下可检索的线索。运行期那条路径本来就由上面第 4 条的端点联动兜住了。

### 5. 「有没有覆盖」看三列，不看行在不在

清空覆盖（界面上「恢复环境配置（清除覆盖）」）之后**行还在**，只是三列都成了 `None`。
若按 `bool(row)` 判，界面会永久停在「已覆盖环境配置」—— 覆盖值一个都没有，用户却再也回不到
「跟随环境配置」，那个按钮也永远点不完。判据收在 `_has_override()` 一处；`updated_by` /
`updated_at` 也只在真有覆盖时上报，否则会把**上一次写入的残留**显示成「最近写入」。

这条不是推演出来的，是**实机验收时踩出来的**：`PUT` 三列 `null` 之后 `GET` 仍回
`overridden=true`、`updated_by=probe`。

### 6. 凭据只写入不回读

`GET` 永不返回 `api_key`，只回 `api_key_configured`；日志只记 `set`/`unset`。沿用 ADR-015 与
§5.8 的既有口径。

**已知残余**：库里是明文（与本项目其余凭据一致），因此该接口不做鉴权，只适合本机 / 内网部署。
这一点没有改变，也没有变差。

### 7. 前端只表达「改了什么」

`SearchChannelPanel` 的保存体按「省略 = 不改动、显式 `null` = 清除」构造；**换渠道时端点输入框
会被清空**，免得把上一条渠道的端点当作显式覆盖发出去 —— 那正好会顶掉第 4 条的联动。端点框
回显的是**生效值**，所以「什么都没改」时保存按钮是禁用的。

## 刻意没做

- **没做「每个渠道各存一份凭据」**。单层覆盖更贴合「我现在要用哪条出口」；多份凭据会引出
  「切回来时用哪份」的歧义，而这里的渠道切换频率极低。
- **没做渠道连通性自检**（界面上点一下试搜）。它要一次真实外呼，成本与配额都归用户，
  且失败原因归一（DNS / 超时 / 契约不符）本身是另一个专项。
- **没碰权限模型**。检索配置无令牌，与 §5.8 的 Provider 配置同一档，见 ADR-015。
- **没有改 `timeout_seconds` / `max_results` 的可配置性**：它们仍只由环境决定，本轮只解决
  「渠道与凭据」这一件被反馈的事。

- **没有让界面显示「端点与渠道错配」的黄色告警**。后端只记日志；界面上正常路径换渠道时会自动
  落对端点，所以看得见的错配只剩「手改 env」这一种，而那种情况使用者本来就在编辑 env。

## 验收

| 手段 | 结果 |
| --- | --- |
| `tests/integration/test_search_config_api.py` | **21 项通过**（含「清空覆盖后不得再报已覆盖」与「端点错配只诊断不篡改」两条回归） |
| `tests/unit/test_search_tool_volcengine.py` | **全绿**（回归：显式端点优先的契约不得被静默改写） |
| `frontend/rendercheck/config-smoke.tsx` | **130/130** |
| CDP 真浏览器（`.workbuddy/_cdp_search_channel.py`） | **11/11**：区块有真实几何、两条渠道、key 框 `type=password` 且可见、端点回显本地网关、切渠道后端点清空 + 缺 key 告警 + 保存可点 |
| 容器内实跑（不经 LLM / 编排） | 默认渠道**实搜返回 3 条**；切豆包不给 key ⇒ 端点自动换为 `https://open.feedcoopapi.com/search_api/web_search` 且报「未配置豆包搜索 API key」（`retryable=False`）；清空覆盖 ⇒ 回到环境配置、`overridden=false` |

**前置条件**：`deploy/compose.yaml` 的容器是 `COPY` 源码、**没有挂载**，改了源码不重建不生效。
上面两次实机验证都是在重建过 `backend` 与 `frontend` 两个镜像之后做的。

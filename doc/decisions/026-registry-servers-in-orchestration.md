# ADR-026: 用户登记的 MCP Server 接入编排层

状态：已接受

## 背景

配置页允许用户登记外部 MCP Server（`mcp_server_registry` 表）、点「发现」拉到工具清单，
目录接口（`doc/api.md` §5.11）也会把它们列出来。但**编排层从来不读那张表**：grep
`mcp_server_registry` 的引用者只有 `app/api/main.py` 的 CRUD 与 `app/mcp/client.py` 的一句
注释；`build_tool_registry()` 只按 `MCP_TRANSPORT` 选内置工具的暴露方式。

结果是**界面上存在的功能是死的**：用户登记、发现、看到工具卡片，Agent 那边永远只有
`calculator` / `web_search` / `sql_query` / `code_execution` 四个。协议侧其实是齐的
（`build_registry_session_factory` 支持 stdio/http/sse 三种传输，`discover_registry_server`
能握手并列出工具），缺的只是「读注册表 → 合成聚合注册表 → 交给流水线」这一步。

这是这个项目里唯一一处「文档承诺了、界面展示了、功能却没接上」的地方，优先级高于继续做
档 3（`doc/orchestration.md` §3.3）。

## 决策

**在 `build_tool_registry()` 里叠一层 `RegistryServersToolRegistry`，把启用的注册表 Server
按名字合成进工具集；目录取自已发现的缓存，条目走进程内快照。**

### 1. 合成，而不是二选一

`MCP_TRANSPORT` 决定的是「**平台自己**的工具怎么暴露」（进程内 / stdio 子进程 / 远端
MCP Server），登记表决定「**用户另外**挂了哪些 Server」。这是两件正交的事：一个部署完全
可以既用进程内内置工具，又挂一个外部 Server。所以是叠加，不是分支。

### 2. 目录来自已发现的缓存，不重新握手

`list_tools()` 展开的是 `discovered.tool_names` / `tool_descriptions` / `tool_schemas`
（`app/core/mcp_registry.py::discover_server` 落库时就存了这三样），**不连接**。

理由与 §5.11 完全一致——「目录是配置的函数」：用户点过「发现」之后目录就定了；Server
离线时它的工具仍在目录里，真被调用才报连接错误。这样列工具**不产生任何超时**，而流水线
**每次执行**都要列一次工具。

### 3. 条目走进程内快照，编排层的工具枚举路径上不能有 IO

这一条是本 ADR 最关键的取舍，也是实现过程中被实测打回来的一版：

第一版让 `list_tools()` **每次现读** `mcp_server_registry`。看起来更"新鲜"，实际上是给
热路径接了一个可能挂住的依赖——存储不可达时（实测：Docker Desktop 停掉后 TCP 包被丢弃，
失败形态是 `TimeoutError` 而不是 `ConnectionRefused`）**每次**工具枚举都要卡满一个超时，
整条流水线一起停摆。而「读不到配置」的正确含义应该是「没有额外工具」。

所以改为：模块级快照 `_registry_servers`，**只加载一次**，之后热路径纯内存；
配置变更由配置面主动告知：

- `refresh_registry_server_entries()`（`app/mcp/registry.py`）重读并替换快照；
- 调用点全在配置面（`app/core/mcp_registry.py`）：Server 的增删改、「发现」之后，
  以及 `list_servers()`——用户打开「工具与配置」页就是在看配置，此刻顺带同步一次最不容易漏
  （它把已经读到的行直接传给刷新，省一次查询）；
- 加载失败**不重试**（置空）。生产中真正让人看不到新 Server 的场景是"进程重启后没人碰过
  配置面"，而用户下一次打开配置页或保存配置就会补上。

### 4. 名字冲突时内置优先，且 `disabled` 真生效

- 与内置工具同名的注册表工具**不进目录**，并记一条 `mcp.registry_tool_name_clash`。
  内置工具是平台自身能力，被用户登记的 Server 顶掉属于静默改变既有行为。
- `tool_options.disabled` 生效：工具不进目录；万一被直接按名字调用，报「已在配置里停用」
  而不是含混的「无此工具」——排障时要能一眼看出是配置问题还是名字写错。
- **`tool_options.allowAutoExecution` 仍然不消费**。它的语义是「这个工具要不要人工确认」，
  而人工确认（HITL）还没做（档 3 的一部分）。既然没有审批环节，"不允许自动执行"就无法
  被正确表达，继续留着它不消费，比把它硬解释成「不暴露给模型」诚实——后者会让用户以为
  自己设的是"需要审批"，实际效果却是"工具消失了"。前端也照旧不给它做开关（ADR-020 同一取向）。

## 备选方案

- **让 `build_tool_registry()` 按注册表条目建实例并长期缓存**。否决：缓存与配置变更之间要
  引入失效钩子，而跨进程（API 与未来可能的独立 workflow worker）失效广播更复杂；
  更重要的是它没解决"首次加载要读库"这件事，只是把频率降低。
- **把注册表内容推进 Redis 或配置文件，编排层读那份**。否决：引入第二份事实源与同步问题，
  而收益（避免一次加载）在"只加载一次"之后已经不存在了。
- **给注册表 Server 的工具加 `server_id` 前缀避免重名**。否决：模型看到的名字会变成
  `filesystem.read_file` 这类形式，与用户在配置页看到的名字不一致；而冲突本身很少见，
  记日志提示即可。
- **每次 `list_tools()` 都重新握手拿最新 schema**。否决：见「决策 2」，代价是每次执行的
  超时风险。目录陈旧的问题由「重新发现」按钮解决，那是用户可理解的时机。
- **`allowAutoExecution=false` 时把工具从目录里摘掉**。否决：见「决策 4」——语义混淆，
  是"假开关"的另一种形式。
- **在 `discover_server` 之外再提供一个"启用即自动发现"**。暂缓：它会让"保存配置"隐含一次
  外部连接，失败时用户要面对一个"保存了但不知道成没成"的状态。当前要求用户显式点「发现」，
  目录为空的产品语义是清楚可解释的。

## 代价（如实记录）

- **重启后到第一次刷新之间存在窗口**：进程起来的快照是空的，用户登记的 Server 要等到
  有人碰配置面（打开「工具与配置」页、增删改、发现）或触发一次懒加载才生效。懒加载兜住了
  最常见的情形，但"重启后一次都没碰过配置面"这段时间里，Agent 看不到那些工具。
- **目录可能陈旧**：`discovered` 是上次「发现」的结果，Server 端后加的工具要重新发现才出现。
- **`input_schema` 取自 `discovered.tool_schemas`**，与真实 Server 当前的定义可能有偏差。
  这与 §5.11 目录的性质相同，不是新引入的问题。
- **工具调用链里的 `source` 仍只标 `MCP_TRANSPORT` 的值**（如 `inprocess`），
  不区分"这个工具来自哪个登记 Server"。要看归属得读日志里的 `mcp.registry_servers_listed`
  与重名记录。做成逐调用标注需要给 `InstrumentedToolRegistry` 透传来源，本轮没做。
- **单测必须隔离这条读取**：`tests/unit/conftest.py` 的 `memory_mcp_registry`（autouse）
  把快照固定为空。这与既有的 `memory_redis`、`MemoryMetricSink` 是同一条约定
  ——单元测试不连真实 PostgreSQL。**不隔离的测试会在存储不可达时卡满 TCP 超时**，
  这正是本轮实测踩到的坑。

## 影响

- `app/mcp/registry.py`：新增 `RegistryServersToolRegistry`、`registry_server_entries()`、
  `refresh_registry_server_entries()`、`reset_registry_server_cache()`、`_entry_tools()`；
  `build_tool_registry()` 改为在基础后端上叠合成层（**不读表、不连接**，保持廉价）；
  `registry.ready` 日志不再调用 `list_tools()`，改为 `_base_tool_count()`
  （探测失败记 0 而不是把构建带崩）；
- `app/core/mcp_registry.py`：新增 `_sync_orchestration_tools()`，
  在 `create_server` / `update_server` / `delete_server` / `discover_server` / `list_servers`
  之后刷新快照；
- `app/mcp/__init__.py`：转出 `RegistryServersToolRegistry` / `registry_server_entries`；
- `tests/unit/conftest.py`：新增 `memory_mcp_registry`（autouse）；
- 测试：`tests/unit/test_registry_servers.py`（13 例：合成、缓存目录、停用、重名、
  路由、传输不支持、读表失败只试一次、刷新生效、`close` 释放）；
- `GET /api/v1/tools`（§5.3）的目录**从此包含注册表 Server 的工具**——这份目录描述的是
  "平台当前可用的全部工具"，之前漏了它们；未发现过的 Server 仍不出现（与 §5.11 前置条件一致）。

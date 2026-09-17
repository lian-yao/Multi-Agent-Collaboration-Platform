# 测试策略与测试计划

> 本文件将原“测试策略”细化为开发前可执行的测试计划。每个里程碑验收时必须能给出本文件
> 对应用例的运行结果。

## 1. 测试层级与运行环境

| 层级 | 范围 | 目录 | 运行前提 |
| --- | --- | --- | --- |
| 单元测试 | 图节点、状态、模型工厂、工具函数、Schema | `tests/unit` | 无需外部服务 |
| 集成测试 | Dapr、Redis、PostgreSQL、MCP、API | `tests/integration` | 本地 Dapr + 服务 |
| 端到端测试 | REST API + Web UI + Dapr + 部署 | `tests/e2e` | 完整 compose 环境 |

常用命令：

```bash
uv run pytest -m "not integration"        # 单元
uv run pytest                              # 全部可用用例
cd deploy; .\start.ps1                     # 起完整环境
```

> **`uv run pytest` 需要可达的 PostgreSQL 与 Redis**（本机 compose 的 `postgres` 与
> `redis` 即可，见 `doc/deployment.md`）。这两者不可达时，用例**不会快速失败**，而是
> 长时间阻塞在连接重试上——曾出现 30 分钟仍无结果、起栈后 14.51s 跑完（成员 D D9-10
> 实测）。跑全量前先确认 `localhost:5433` 与 `localhost:6380` 可连。

> **配置类用例要求「无既有覆盖」的干净存储**（2026-09-15 补充）。`tests/integration/`
> 里 `test_config_api.py`、`test_inspection_api.py` 断言的是「环境配置 + 无覆盖」的生效值，
> 而 `provider_configs` 与 Redis 镜像 `provider:config` 都是**跨运行持久**的：只要开发环境
> 里通过配置页保存过 provider/model，这些用例就会以「存储值优先于环境配置」而失败
> （表现为 `provider`/`model` 断言不符、`status` 由 `missing_model` 变成 `configured`）。
>
> 这是**环境隔离问题，不是代码缺陷**。跑全量前把测试指向独立的库与 Redis DB：
>
> ```bash
> # 一次性：准备空库
> docker exec <postgres 容器> psql -U postgres -c "DROP DATABASE IF EXISTS macp_test;" \
>                                          -c "CREATE DATABASE macp_test;"
> docker exec <redis 容器> redis-cli -n 15 flushdb
>
> DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/macp_test" \
> REDIS_URL="redis://localhost:6380/15" \
> uv run pytest
> ```
>
> 不要为了让用例通过而清空开发环境里的 `provider_configs` 或 Redis 的 `provider:config`
> ——那是真实配置（含在用凭据）。

测试替身：

- LLM 使用 `FakeChatModel`（已有实现），按用例注入固定回复；
- MCP 工具使用内存版注册表或本地 Fake MCP Server；
- 不依赖真实模型的用例禁止请求 Ollama/OpenAI。

## 2. 用例矩阵

### 2.1 单元测试（U）

| 编号 | 用例 | 断言要点 | 对应里程碑 |
| --- | --- | --- | --- |
| U-01 | 单 Agent 图返回回复 | 末条消息为 AI 消息 | M1 |
| U-02 | 多轮状态累积 | 历史消息不丢失、顺序正确 | M1 |
| U-03 | 模型工厂按配置创建 | Ollama/OpenAI 分支正确、非法 provider 报错 | M1 |
| U-04 | 会话/AgentRun/Workflow 状态迁移 | 非法迁移被拒绝 | M2/M3 |
| U-05 | 图状态 Schema 序列化 | LangGraph 状态可无损转 JSON 并还原 | M3 |
| U-06 | 工具函数独立行为 | 计算器/SQL 只读校验等返回值正确 | M4 |
| U-07 | 工具调用幂等键 | 同 ID 重复投递返回缓存结果；失败可同 ID 重试；`running` 拒绝并发重放；装饰器记录 running→终态（ADR-011） | M4 |
| U-08 | 沙箱边界拒绝越权 | 代码执行工具拒绝网络/危险命令 | M4 |
| U-09 | 流水线接入 MCP 工具 | 注册表工具被发现，按模型 `tool_calls` 调用并回填观察；失败记为 failed 且不中断；阶段载荷可序列化 | M4 |
| U-10 | 行为日志事件 | 阶段开始/结束/失败与工具调用输出 `event=... field=value` 结构化日志，含 workflow_id 与耗时 | M4 |
| U-11 | 动态编排计划与调度 | 计划解析只收合法形态（未知角色 / 重复 id / 自依赖 / 前向依赖 / 超步数一律整份丢弃）；规划模型抛错或返回垃圾文本时回退固定三步；依赖就绪度调度、失败连坐（含传递闭包）与最终交付物取值；Dapr 侧先规划后执行、子工作流实例 ID 稳定、业务终态与实例终态一致（ADR-019） | M5 |
| U-12 | 多模态附件：分类 / 限额 / 解析 / 提示词 | 类型白名单（图片、文本与代码、pdf/docx/xlsx）与三类拒绝理由（空文件 / 超限 / 格式不支持）**都指名到具体文件**；文本解码的 `gb18030` 回退与可打印率闸门能挡住二进制；docx/xlsx 用标准库抽出段落与单元格；PDF 质量闸门把扫描件与 CID 字体判为 `failed`，且吃得下真实形态的 **FlateDecode 压缩流**与多页内容流；`build_human_content` 无图片时返回**纯字符串**（不改变既有链路），有图片时升级为 content block；失败项必须出现在附件清单里；**原件对所有类型留档**（ADR-021 / ADR-024） | M5 |
| U-13 | 沙箱可用性探测与容器硬化 | 套接字连不上时原因要点到 `/var/run/docker.sock` 并给出可照做的动作；**只 `ping` 通不算可用**（镜像不在宿主机同样报不可用）；`SANDBOX_AUTO_PULL_IMAGE` 打开时自拉一次并重试、拉取失败还原为 `SandboxUnavailable`；探测自身抛异常时仍返回原因而不冒泡；容器参数逐条钉住（`cap_drop=['ALL']`、`read_only`、`network_disabled`、`user=nobody`、`security_opt=['no-new-privileges']`、`/app` 与 `/tmp` 的 tmpfs、`macp.role` 标签）（ADR-023） | M5 |

### 2.2 集成测试（I）

| 编号 | 用例 | 断言要点 | 里程碑 |
| --- | --- | --- | --- |
| I-01 | Workflow 调度与状态查询 | 调度后状态 running，完成后 completed | M2 |
| I-02 | 会话消息写入 PostgreSQL + Redis | 双写一致、Redis 可重建 | M2 |
| I-03 | Workflow 断点持久化 | 阶段结果可在 State Store 查到 | M2 |
| I-04 | 暂停/恢复 API | paused 时新消息 409，resume 后续跑 | M3 |
| I-05 | 多 Agent 三步流水线 | collector → analyst → reporter 顺序完成 | M3 |
| I-06 | MCP 工具发现与调用 | 工具可发现、可调用、审计落库 | M4 |
| I-07 | 可观测数据输出 | 关键 Span 与指标可在 Jaeger/Prometheus 查到 | M4 |
| I-08 | 配置热更新 | PATCH agent 后新执行使用新配置 | M4 |
| I-09 | D7-D8 只读巡检接口 | Provider/Agent/工具/调用/指标接口的分页、`availability` 区分「未接入」与「零条记录」、404/422/503 契约 | M4 |
| I-10 | Provider 配置读写 | `GET/PUT /api/v1/config/provider`：200 生效值与回退、422 校验、503 写失败、裸请求可写（不鉴权，ADR-015）；覆盖值落 `provider_configs` 并镜像 Redis，响应与日志不含密钥（ADR-014） | M4 |
| I-11 | 多模态附件接口契约 | `POST /api/v1/attachments` 的 201、四类 400（非法 base64 / 空 / 超限 / 格式不支持）；发消息带上 `attachment_ids` 后附件归属回填且**只挂一次**（重复提交进 `unattached_attachment_ids`）；「只带图不带文字」可发送；`GET .../content` 对图片回 `inline`、对文本与文档（**含解析失败项**）回 `attachment` 原件、对无字节的旧行回 404；`DELETE` 未归属 204 / 已发出 409；删会话级联清附件（ADR-021 / ADR-024） | M5 |
| I-12 | 执行边界诊断接口 | `GET /api/v1/config/sandbox`：200 时 `available` / `reason` 的语义；原因**原样透传**后端给出的文案（不再由接口层编兜底话术）；探测自身抛异常时仍回 200 + 原因；`PUT` / `POST` 一律 405（ADR-020 / ADR-023） | M5 |

### 2.3 端到端测试（E）

| 编号 | 用例 | 步骤 | 通过标准 | 里程碑 |
| --- | --- | --- | --- | --- |
| E-01 | 单 Agent 问答 | 创建会话 → 发消息 → 轮询/取结果 | 返回结构化回答 | M5 |
| E-02 | 多 Agent 协作 | 提交三步任务 → 观察状态流转 | 三步依次完成并生成报告 | M5 |
| E-03 | 故障恢复 | 执行中断掉 backend → 重启 | 任务从断点续跑成功，无状态丢失 | M3 起可演练，M5 验收 |
| E-04 | Web 会话管理 | UI 创建会话、发消息、暂停/恢复 | UI 与 API 状态一致 | M5 |
| E-05 | 一键部署 | `start.ps1` → 健康检查 → `stop.ps1` | 全部服务健康 | M5 |

E-04/E-05 目前的自动化程度（成员 D D9-10）：会话生命周期（创建 → 暂停 → 暂停期提交被拒
→ 恢复 → 回读）与「前端容器 + nginx `/api` 反代」走 `tests/e2e/test_live_e2e.py` 的
Web 侧用例，部署脚本按 `deploy/start.ps1` / `stop.ps1` 实跑并以退出码与三段健康检查判定。
浏览器里的**渲染效果**仍需人工按 `doc/deployment.md`「演示与验收」的核对清单确认；
发消息后的三步流水线需要可用的模型凭据（提供方前提见 §4.2）。

## 3. 关键链路测试设计

### 3.1 Workflow 恢复测试（I-03 / E-03）

最小可复现方案：

1. 启动流水线到固定阶段（如执行 collector 后）；
2. 记录 Dapr Workflow `instance_id`；
3. 杀掉 backend 进程/容器；
4. 重启 backend；
5. 轮询 `GET /workflows/{id}`，断言从 `analysis` 阶段继续并最终 `completed`；
6. 记录从服务可用到实例恢复执行的耗时，目标 `< 5s`。

自动化路线：已提供 `scripts/fault_recovery.ps1` 固化手工演练步骤；

现状（2026-09-15，成员 C D9-10）：已升级为**可重复的脚本化测量**——
`uv run python scripts/measure_recovery.py` 按上述步骤自动执行并输出 JSON
（不可用时长、服务可用→实例恢复执行的耗时、扣除定时器等待后的值、业务状态是否保留、
Dapr orchestration 终态）。实测数据见 §4.2；`tests/unit/test_workflow_pipeline.py`
仍覆盖活动重放、子 Workflow 实例 ID 稳定、终态回写等**单元级**恢复语义，
被杀的进程改由 pytest 用例控制（需容器控制权）仍未落地。

注意：该演练路径曾有缺口——`app/workflows/poc.py` 用 `session_id="demo-session"`
触发 `finalize_activity` 的报告消息写入抛 `badly formed hexadecimal UUID string`，
业务行是 `completed` 而 Dapr orchestration 是 `FAILED`（缺口 F-05，见 ADR-016）。
**F-05 已由成员 B 修复**（`session_id` 不再硬编码，且 CLI 对运行时终态非 `COMPLETED`
即非零码退出）；`scripts/measure_recovery.py` 的日志侧终态校验**保留为守卫**
（不因对方修好而撤掉）。§4.2 的 E-03 数据取自修复前，且走 poc 的确定性（假模型）路径，
修复后的演练需重跑一遍确认两个终态同时为成功。

### 3.2 并发会话测试（E 系列性能）

- 使用 `locust` 或 `hey` 模拟 10 个并发会话；
- 断言：全部会话完成、无 5xx 比例超过阈值、平均延迟记录在报告；
- 指标从 Prometheus 导出 Token 消耗与工具调用成功率。

实现（成员 C D9-10）：未引入 `locust`/`hey`，用 `scripts/perf_concurrency.py`
（标准库线程池 + `httpx`）实现同等测量，避免新增依赖：

```bash
uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10 --json perf.json
```

- 每个虚拟会话：创建会话 → 发消息（记录 202 受理延迟）→ 轮询 Workflow 到终态
  （记录端到端延迟），汇总成功率、终态分布、HTTP 错误数、延迟 p50/p95/max；
- Token 消耗与工具调用成功率：事实源要求从 Prometheus 导出，但**测量当时** backend 未
  暴露 Prometheus 文本端点（D 侧）且 `metrics` 表未建（B 侧），因此改从**行为日志**采样
  （`event=llm.finish` 的 `input_tokens`/`output_tokens`/`total_tokens`/`duration_ms`、
  `event=tool.call` 的 `status`）——数值是真实测量值，通道与事实源的差异在本文件与
  ADR-016 中显式标注；工具调用 0 次时成功率输出 `null`（不谎报 0%）。
  这两条通道随后都已由 B/D 落地（F-04），**事实源通道现已可用**，
  可另取一轮从 `/metrics` 或 `metrics` 表取数并与此处数据对比。
- 模型名：`event=llm.finish` 的 `model` 字段在 F-03 修复后为真实模型名
  （修复前是集成类名或缺失），因此按模型归因 Token 效率具备前提。
- 失败率超过 `--max-failure-rate`（默认 0）时脚本以非零码退出。
- 脚本纯逻辑（百分位口径、日志解析、报告汇总）由 `tests/unit/test_perf_tooling.py` 固定。

### 3.3 前端验证现状（成员 D）

前端当前**没有测试框架**，自动化门禁是类型检查 + 构建：`npm --prefix frontend run build`
（`tsc --noEmit && vite build`）。因此前端改动按「构建通过 + 真实后端冒烟」两步验证，
不把构建通过当作功能验收。

Provider 配置面板的验证步骤（ADR-017 之后：`frontend/src/config/` 下的
「默认路由」分区 = `DefaultRoutePanel.tsx`）：

1. 起后端（真实 PostgreSQL + Redis）与 `npm run dev`；
2. 「工具与配置 → 默认路由」应显示生效的 provider/model/地址/温度与凭据状态；
3. 填入非法取值（例如 Temperature 填 3）提交 → 页面给出取值错误，配置不变；
4. 改模型或地址后提交 → 提示已保存，页面回读生效值，响应与页面都不出现密钥；
5. 点「清除覆盖并回退环境配置」→ 页面回到环境配置值。

注册表配置页（ADR-017）的分区与验证要点。**三个页面各管一段**，不要在同一页里既写
配置又做观测：

| 页面 | 组件 | 分区 / 验证要点 |
| --- | --- | --- |
| 工具与配置 | `config/ConfigPage.tsx` | 放写配置的三个分区 + 一个**只读**的执行边界，见下表；四个分区用 `components/PageTabs.tsx` 副路由切换，标题与副路由左对齐（**不整页居中**） |
| Agent 团队 | `App.tsx::AgentTeamPage` → `config/AgentPanel.tsx` | 角色路由：一次 `GET /config/agents` 取回角色与 `available_models`；角色**一行多个方块**，方块只显示摘要（名字 / `role · 状态` / 生效模型 / Temperature / 覆盖项数），点击方块在网格下方展开 `AgentTuningPanel` 编辑，再点一次或「收起」关掉；`override_keys` 高亮「已覆盖」字段；「清除全部覆盖」发 6 个 `null`；`activeAgentId` 只做当前阶段高亮 |
| 任务记录 | `records/RecordsPage.tsx`（容器与行渲染在 `records/Inspection.tsx`） | 四分区副路由：`runs` 运行记录、`calls` 工具调用（§5.5）、`metrics` 指标采样（§5.6）、`sessions` 历史会话（§5.13）。采样有 Workflow 时按 `workflow_id` 取并轮询（终态停），无 Workflow 时退回全局采样；历史会话调 `GET /api/v1/sessions` 分页列出，点选按 `latest_workflow_id` 恢复执行台并回工作台；`Records` 统一「加载中 / 失败 / 未接入 / 无记录 / 有数据」五态，分页仅在多页时出现 |

「工具与配置」页的分区（前三个可写，第四个只读）：

| 分区 | 组件 | 验证要点 |
| --- | --- | --- |
| Provider | `ProviderPanel.tsx` + `ModelSection.tsx` | 预设目录预填 `preset_type`/`api_type`/`base_url`；`api_key` 输入框留空 = 不修改；删除仍有启用模型的 Provider 时先用 `409 PROVIDER_IN_USE` 拦一次，再让用户确认 `?force=true`；`openai-compatible`（自定义）的**图标位渲染加号**（`.cfg-mark-add`），不再把「自定义」当 monogram 文字塞进方块 |
| 同上 · 批量引入 | `ModelSection.tsx::BatchImportModal` | 「从远端发现」**只在点击时**发起（不在挂载时调用）；已登记模型置灰计入 `existing`；重复提交返回 `skipped` 而不报错 |
| 同上 · 特化调参 | `ModelSection.tsx::ModelTuningForm` | 只提交被改动字段；清空数字输入 = 显式 `null`（回到未设置）；`PATCH` 不发 `provider_id`；改模型名时按 `modelCapabilities.ts` 带出常见模型的**上下文上限**，用户手改过（`contextTouched`）之后不再覆盖 |
| 同上 · 手动登记 | `ModelSection.tsx::ModelCreateModal` | 与特化调参同一套自动带出与文案（`describeContextHint`）；识别不到常见模型时**留空**由用户手填，不做正则猜测 |
| 默认路由 | `DefaultRoutePanel.tsx` | `default_llm_model_id` 非空时展示解析出的注册表来源；悬空 id 给出提示而不是报错 |
| MCP 工具 | `McpPanel.tsx` | 工具卡片只显示 `name` / 截断后的 `description` / 开关与可用性；完整 `input_schema` 收进**默认折叠**的 `<details>`，展开时才调 §5.3。工具级选项里**只有 `disabled` 有界面出口**：`allowAutoExecution` 虽然在 `tool_options` 与 `doc/api.md` §5.11 里，但执行链路没有任何地方消费它（见 §3.4 的漂移清单），所以**不给它做界面开关**——否则就是一个点了没效果的假开关 |
| 同上 · 粘贴导入 | `McpImportModal.tsx` + `mcpConfig.ts` | 粘贴外部客户端的配置 JSON（`mcpServers` 映射/数组、裸映射、单条参数四种形态）后**立即解析并预览**，不自动提交；已登记的 ID 在预览里标黄「会失败」；提交是**前端逐条**调 `POST /config/mcp/servers`（§5.11 只有单条创建接口），一条失败不拖垮其余，成功与失败分别汇报，**全部成功才关闭弹层** |
| 同上 · Server 表单 | `McpPanel.tsx::ServerFormModal` + `KeyValueFields.tsx` | 传输下拉按**远程 / 本地**分组；环境变量与请求头是**键值对行编辑器**（可增删、逐项校验空键与重复键），不再是「每行 KEY=VALUE」文本域；表单校验与粘贴导入**共用** `mcpConfig.ts::draftProblem`，两处不会各漂一套口径 |
| 执行边界 | `SandboxPanel.tsx` | **只读**：展示沙箱后端、镜像、可用性探测与不可用原因，以及 7 项生效限额；`available=false` 时原因行必现（`GET /config/sandbox` §5.15）。**没有任何可编辑控件**，后端也没有写接口（`PUT` → `405`）。为什么不给开关：这些是部署期安全边界，且当前 `available=false` 的成因是缺 docker.sock，改限额不会让它变可用（ADR-020） |

浏览器端的自动化用例（Playwright 之类）尚未引入，属后续增量。在此之前，配置页与工作台
各有一层**无浏览器渲染冒烟**（不引入新依赖，只用项目已有的 react / esbuild）：

```bash
cd frontend
node_modules/.bin/esbuild rendercheck/config-smoke.tsx --bundle --platform=node \
  --format=cjs --jsx=automatic --loader:.css=empty --outfile="$TEMP/config-smoke.cjs" \
  && node "$TEMP/config-smoke.cjs"        # 退出码 0 = 通过
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx --bundle --platform=node \
  --format=cjs --jsx=automatic --loader:.css=empty --outfile="$TEMP/workspace-smoke.cjs" \
  && node "$TEMP/workspace-smoke.cjs"     # 退出码 0 = 通过
```

> `--outfile` 必须落在仓库外。写成 `--outfile="/c/..."` 会被当成相对路径，在
> `frontend/` 下造出 `frontend/c/Users/...` 这种路径形状的垃圾目录（已踩过一次，见
> 2026-09-15 日志）。用 `C:/...` 或 `$TEMP`。

`config-smoke` 用 `react-dom/server` 渲染整棵配置页，断言配置页只剩 4 个分区入口、已迁走
的分区不再出现在这里，另外**单独挂载**一次 `AgentPanel`（Agent 团队页）、
`RuntimeSampling` 与 `RecordsPage`（任务记录页）——它们不在 `RuntimeConfig` 的树里，
不单独挂就等于换页后无人验证。再加三层用显式 props 驱动、effects 够不到的区块：
`AgentRoleCard`（角色方块：摘要 / 当前阶段 / 覆盖项数齐全，**整块是 `<button>` 且表单
不在方块里**）、`AgentTuningPanel`（六个覆盖字段 + 层次来源 + 保存/清除入口 + 已绑定
模型回显）与 `ModelSection`（两条模型、特化徽标、开关、批量引入入口）。

`workspace-smoke`（ADR-018）覆盖工作台三块新视图，同样是「显式 props 驱动」那一类：
`CollaborationGraph`（串行 / 并行波次两种排布、等待态文案、空态，以及**未传 `onSelect`
时渲染为 `<div>` 而不是 `<button>`**）、`AgentStageModal`（身份与绑定、`pending` 说
「等待前置阶段」而不是 workflow 词汇「排队中」、角色未就绪时的降级、明示推理过程尚未
对外暴露）与 `TaskUsagePanel` + `groupUsage`（**同一指标多次采样并排列出、断言不求和**）。
另有一组读源文件的静态断言，固定「假选择已删除」：`styles.css` 不含 `.decision-*`、
`App.tsx` 不含「主决策 / 自动分配」、卡片点击走 `setDetailStage` 而非 `setInspectorOpen`、
`WorkflowInspection` 已从 `Inspection.tsx` 删除。

副路由（2026-09-15 增加，`components/PageTabs.tsx`）另有三类断言：
`role="tablist"` / `role="tab"` 语义与 `aria-selected`、只有选中项 `tabindex="0"`、
`variant="inline"` 落在 `ui-tabs-inline` 上。**版式回归**则读文件断言：
`src/**/*.css` 里 `.config-page` 不得再有 `max-width` + `margin:0 auto`
（用户明确报过的「配置页居中、与其它页不一致」），且 `.ui-tabs` 必须是
`width: fit-content`（否则副路由会撑满一行，重新变成居中观感）；
`.config-page > :not(.page-heading):not(.ui-tabs)` 必须带 `max-width:1180px` +
`margin-inline:auto`（内容限宽居中——副路由左置但内容拉满整行同样是用户报过的问题）。
因这两条断言按 `process.cwd()` 找样式表，**脚本必须在 `frontend/` 下运行**。

行内二次确认（2026-09-16 增加，`components/InlineConfirm.tsx`）另有一组断言：**未点击时只渲染
触发按钮**（不得出现 `ui-confirm-yes` 或「取消」）、宿主 slot 类名落在外层（各区域靠它复用
绝对定位）、确认条的 `role="group"` 与「确认 / 取消」两键，以及两条**读文件断言**：

1. `src/**/*.ts(x)` 不得再出现 `window.confirm|alert|prompt`。这条是用户实报问题的固化——原生
   对话框由宿主提供，沙箱 iframe（无 `allow-modals`）与浏览器「阻止此页面创建更多对话框」都会
   屏蔽它；被屏蔽时 `confirm()` 不弹窗、直接返回 `false`，挂在返回值上的删除逻辑就静默失效
   （表现为「点了删除没反应」）。**新增删除/清空类入口时先过这一条断言。**
2. `.ui-confirm` 容器规则必须与触发按钮**同形**：`border-radius: 6px`（与
   `.sidebar-user-item-delete` / `.cfg-quiet` 同款）、`border: 0`（按钮自带的边会和容器边叠成
   「框套框」）、`padding: 2px` 且 `gap: 2px`。用户 2026-09-16 反馈过「外层容器不融洽、间距
   太大」，改确认条样式时别把这条改红。

Provider 图标位与模型上下文识别（2026-09-17 增加）另有三条断言：

3. **渲染断言**：`ProviderMark` 传 `presetType="openai-compatible"` 时必须渲染 `cfg-mark-add`
   且 HTML 里**不得出现「自定义」文字**（把中文当 monogram 会缩到很小、且与品牌白底方块不同
   形）；传品牌预设时仍走品牌标、不得带 `cfg-mark-add`。
4. **读文件断言**：`src/config/modelCapabilities.ts` 必须有 `KNOWN_CONTEXT_TOKENS` 与
   `resolveKnownContextTokens`；`ModelSection.tsx` 里 `resolveKnownContextTokens(` 至少两处
   （两个表单入口各一）、`contextTouched` 至少四处（声明 + 判断 + 提示 + 写值），保证「自动带
   出」与「手改后停手」两个语义都还在。
5. **纯函数断言**：`resolveKnownContextTokens` 对 `openai/gpt-4o`（带厂商前缀）、
   `claude-sonnet-4-5-20250929`（带快照日期）、`gemini-2.5-pro` 要命中，对不认识的模型名返回
   `null`——**宁可留空也不猜**，猜错会让用户以为上限比真实值大，从而攒出必然超限的请求。

MCP 粘贴导入与键值对编辑（2026-09-17 增加）另有一条纯函数断言、一条渲染断言与一条读文件断言：

6. **纯函数断言**（`mcpConfig.ts` 是与后端 `app/core/mcp_registry.py` 同口径的纯函数层）：
   四种输入形态各测一遍——`mcpServers` 映射 / `mcpServers` 数组 / 裸映射 / 单条参数——外加
   `type: "streamable-http"` 归一成 `http`、按 `command`/`url` 推断传输、非法条目**逐条给出
   原因**而不是静默丢弃、JSON 语法错误向上抛出给弹层转文案、请求体按传输裁剪字段（本地不带
   `url`、远程不带 `command`/`args`/`env`）。`KeyValueFields.tsx` 的取值规则同批覆盖：
   空行忽略、空键报错、重复键报错、比较按项不看引用。
7. **渲染断言**：`McpImportModal` 按显式 props 单独挂一次——它平时只在点击后才挂载，静态渲染
   够不到。有内容时必须出现逐条预览与 `cfg-mcp-import-item dup`（与已有条目撞车的 ID 标黄）；
   空态必须能渲染且**不出现条目行**。
8. **读文件断言**：`McpPanel.tsx` 必须调 `draftProblem(`，且**不得再出现** `parsePairs(` /
   `formatPairs(`——校验只写在 `mcpConfig.ts` 一处，防止「表单」与「粘贴导入」两条入口各漂一套
   口径；三个入口（`<KeyValueFields`、`TRANSPORT_GROUPS.map`、`<McpImportModal`）都要接在
   渲染树里。另有一条**类名不串台**断言：导入弹层必须用 `cfg-mcp-import-*`，因为 `cfg-import-*`
   已被「模型批量引入」占用（`ModelSection.tsx` 的 `.cfg-import-block` / `.cfg-import-toolbar`），
   同名复用会让两处布局互相带崩。

执行边界只读（2026-09-17 增加，ADR-020）另有三条断言，都指向同一件事——**这块不能有写入口**：

9. **渲染断言**：`SandboxBoundary` 按显式 props 单独挂一次（面板本体靠 `useEffect` 拉数据，
   静态渲染够不到）。不可用时必须同时出现原因行与逐项限额（`15 秒`、`0.5 核`、`256m`、
   `禁用`、`4000 字符`）；可用时**不得**渲染 `cfg-alert`。
10. **只读断言**：`SandboxBoundary` 的渲染结果里**不得出现** `<input` / `<select` /
    `<textarea`；`ui-preview.html` 的 jsdom 自检也断言「执行边界分区内可编辑控件数为 0」。
11. **契约断言**：`tests/integration/test_api.py` 断言 `PUT /api/v1/config/sandbox` 返回
    **405**。写接口是被显式测掉的——以后要加必须先改这条用例和 ADR-020。

多模态附件与欢迎区引导卡（2026-09-17 增加，ADR-021 / ADR-022）另有一组断言：

12. **纯函数断言**（`workspace/attachments.ts` 与后端 `app/attachments/spec.py` 持同一份口径）：
    扩展名表命中图片 / 代码 / 文档三类、`MAX_ATTACHMENT_COUNT === 4`、
    `MAX_ATTACHMENT_BYTES === 5 * 1024 * 1024`、`classifyLocal` 对四类文件的归类、
    `rejectionReason` 的三类拒绝**必须指名到文件**、`formatBytes` 的 KB/MB 换算、
    `shortenName` 折叠后仍保留扩展名。
13. **渲染断言**：`PendingFileChips` 与 `MessageAttachmentList` 按显式 props 单独挂一次
    （空态必须渲染成空字符串，不能留一个空容器）。三种状态的**可见文案**要各就各位
    （上传中 / 失败原因 / 体积）；图片附件渲染 `<img>` 且指向附件正文地址，非图片
    **不得**出现 `img`；解析失败项必须写出原因并带 `failed` 标记。
14. **读文件断言**：`App.tsx` 必须包含三条协作形态标签、`onChoose(p.text, "dynamic")`
    （点卡同时切模式）、`promptMode` + `setMode(promptMode)`；`styles.css` **不得再有**
    `.suggestions button`（旧横排规则残留会把新卡的图标撑成整宽），且新增界面引用的
    类名（`.suggestion-icon` / `.suggestion-shape` / `.welcome .welcome-note` /
    `.composer-attach` / `.composer-files` / `.composer-file.failed` /
    `.conversation-composer.dragging` / `.composer-mode button.active` /
    `.message-attachments` / `.message-attachment-thumb`）**逐个都要有定义**。
    最后一条治的是真实事故：`config.css` 曾因残缺注释让 esbuild 压缩器**静默丢掉约 3KB 规则**
    （只打 WARNING、退出码仍是 0），所以「类名有定义」不能只靠肉眼看构建输出。
15. **选择器口径断言**：附件条目的**类名留给状态**（`uploading` / `ready` / `failed`），
    类型写在 `data-kind` 上。必须出现 `.message-attachment[data-kind="text"]` 与
    `.composer-file[data-kind="image"]`——写成 `.message-attachment.text` 不会报错，
    只是永远不命中（初版就是这么写的，靠预览自检里数 `[data-kind="document"]` 的条数才发现）。

**边界要说清**：它只跑不依赖 `useEffect` 的路径，跑不到「点击 → 请求 → 回填」的交互
链路；那部分仍是人工浏览器验收（上面两张表就是人工清单），或退到 §3.4 的静态预览。
为什么需要它：本机 `npm install agent-browser` 长时间无产物（要拉 ~500MB Chromium），
起不了浏览器，只能退到这一层。

为了让它够得着卡片内部，`AgentRoleCard` / `AgentTuningPanel` 与 `ModelSection` 都按
「显式 props 驱动」的形状导出；新增复杂卡片时沿用这个约定，别把可测的部分藏在 effect 后面。

### 3.4 UI 评审预览（不需要后端，2026-09-15 增加）

§3.3 的冒烟只到「渲染不崩」，看不到版式，也点不动副路由。后端起真需要
PostgreSQL + Dapr，评审一次「左对齐有没有改对」不该被环境卡住，所以另加一个
**单文件、离线、可点击**的预览：

```bash
python frontend/rendercheck/build-preview.py      # 产出 rendercheck/ui-preview.html
```

三个文件，职责分开：

| 文件 | 作用 |
| --- | --- |
| `rendercheck/preview_seed.py` | 评审用的种子数据。既能被 `build-preview.py` 摊平成静态路由表，也能直接跑起来当临时后端（监听 8000，配合 `npm run dev` 的 `/api` 代理联调） |
| `rendercheck/preview.tsx` | 把**真实的 `App`** 挂进浏览器，并接管 `fetch`。路由表是 `"<METHOD> <path>"` 的扁平映射，动态段在 Python 侧已固定成常量，所以这里只做一次查表 |
| `rendercheck/build-preview.py` | 摊平数据 → esbuild 打浏览器 IIFE（带真实 CSS）→ 把 JS/CSS/种子 JSON 内联成一个 HTML |

要点与坑：

- `preview.tsx` 必须**自己** `import "../src/styles.css"`。那一行只在 `src/main.tsx` 里，
  而预览不经过 `main.tsx`；漏了会得到一个没有全局令牌与外壳版式的空壳页面。
- 变更类请求（保存 / 删除）预览不模拟落库，统一回空成功体，免得评审版式时被红字带偏。
  **例外是附件（ADR-021）**：`POST /api/v1/attachments` 在内存里造一条真附件、
  `DELETE` 真删、`POST /messages` 真把消息推进该会话的消息表——因为前端发完立刻重拉
  `GET /messages`，不真推进去「刚发出去的那条会人间蒸发」，评审时会以为是 bug。
- 图片附件在预览里用**内联 SVG 顶替**正文地址（`preview.tsx` 换掉
  `api.attachmentContentUrl` 的返回值）。`<img src>` 不走 `fetch`，mock 拦不住它，
  预览又是 `file://` 单文件，不替换必然裂图，「缩略图长什么样」就评审不出来。
- `build-preview.py` 里 `POST /messages` 的响应必须带 `attachments` 与
  `unattached_attachment_ids` 两个字段：前端会读后者 `.length`，缺了就在「发送成功」之后
  抛 `TypeError`（预览里点一次发送即可复现）。
- **注释里不能出现 `*/`。** `preview.tsx` 的一条注释写了 `/api/v1/attachments/*/content`，
  其中的 `*/` 把块注释提前闭合，后半段代码被当成语法碎片，`tsc` 报出一串
  「Module declaration names may only use ' or " quoted strings / Unterminated template literal」
  这类与真实位置无关的错。写路径通配时改成 `<id>` 或 `xxx`。
- **`tsconfig.json` 的 `include` 只有 `src`**，`rendercheck/` 不在里面，所以
  `npm run build` 覆盖不到这三个文件（esbuild 只转译、不做类型检查）。改完要单独过一遍：

  ```bash
  cd frontend && npx tsc --noEmit --jsx react-jsx --module esnext \
    --moduleResolution bundler --target es2022 --lib es2022,dom,dom.iterable \
    --strict --skipLibCheck --esModuleInterop --isolatedModules \
    rendercheck/preview.tsx rendercheck/workspace-smoke.tsx rendercheck/config-smoke.tsx
  ```

  （会剩下 `node:fs` / `process` 找不到声明的报错，那是项目没依赖 `@types/node`，不是代码问题；
  把这几类报错滤掉，剩下的才是真的。）
  **这条命令比「`npm run build` 通过」更有意义**：`Attachment` / `Agent` 这类类型的必填字段一改，
  `rendercheck` 里的夹具不会在构建时报错。2026-09-17 给 `Attachment` 加 `has_original` 时，就是靠
  这条命令才发现 `preview.tsx` 的夹具漏了它（漏了会让预览里新上传的附件全部退化成「没有下载
  入口」的形态，而这恰恰是要评审的东西）。
- 产物 `ui-preview.html` 与中间产物 `.preview-bundle.*` 已进 `.gitignore`，不入库。

预览产物本身可以离线自检（`jsdom` 挂载 + 点一遍侧栏、副路由、四个配置分区、会话生命周期，
以及 2026-09-17 新增的欢迎卡与附件：三张卡在且竖排居中、**先手动切「固定三步」再点卡片**
验证模式被切回「自动编排」、切开会话后气泡里的**四种**附件形态（图片 / 文档 / 解析失败 /
无原件的历史行）与「有原件的才是链接」，共 45 项断言）。
这条链路**不进仓库**，因为项目刻意不引前端测试依赖；本机想跑就临时装：

```bash
npm i jsdom && node verify_preview.mjs frontend/rendercheck/ui-preview.html
```

> 为什么「先手动切到固定三步」这一步不能省：模式默认就是 `dynamic`，不先改一次的话
> 「点卡片后是自动编排」这条断言**在功能坏掉时也会通过**。凡是断言「某动作导致了状态变化」，
> 都要先把状态置于另一个值。

> **纠正一条旧结论**：早前记录「`npm install jsdom` 长时间无产物」被当成网络问题。
> 真实原因是 **npm 在缺少 `package.json` 的目录里会挂住**——补一个最小
> `package.json` 后 `jsdom` / `linkedom` 都在 4 秒内装完。以后遇到 npm 无输出，
> 先确认目标目录有没有 `package.json`，再怀疑镜像。

## 4. 里程碑验收清单

| 里程碑 | 必须通过的用例 |
| --- | --- |
| M1（单 Agent） | U-01、U-02、U-03 |
| M2（Dapr 持久化） | I-01、I-02、I-03 |
| M3（多 Agent） | U-04、U-05、I-04、I-05、E-03 手工版 |
| M4（工具 + 可观测） | U-06、U-07、U-08、U-09、U-10、I-06、I-07、I-08、I-09、I-10 |
| M5（交付） | E-01 至 E-05 全部 |

### 4.1 M4 当前状态（2026-09-15）

成员 C 的 D7-8（内置工具/沙箱/可观测接入）落地后的实测状态。
**M4 已完成（2026-09-15 验收通过）**（口径与 `doc/roadmap.md` D7-D8 一致）：
真实 Dapr Workflow + OpenAI 兼容 API（`deepseek-flash`）产出了工具调用并落审计表，
详见文末验证记录与 `doc/roadmap.md`。
**注意判定标准**：M4 的完成依据是「真实模型路径下的验收证据」，而不是用例数量——
测试全绿曾是 M4 未闭环时的状态，因此不能用测试结果替代验收。

| 用例 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| U-06 工具函数独立行为 | 通过 | `tests/unit/test_builtin_tools.py`：计算器返回值与拒绝面、只读 SQL 校验与真实只读执行、注册表发现/调用、`web_search` 注入 fetcher 的离线解析（ADR-012） |
| U-07 工具调用幂等键 | 通过 | `tests/unit/test_tool_audit.py`（ADR-011） |
| U-08 沙箱边界拒绝越权 | 通过 | `tests/unit/test_sandbox_policy.py`：Python/Shell 越权拒绝、策略先于后端、`denied` 后端不降级执行（ADR-012） |
| U-09 流水线接入 MCP 工具 | 通过（含真实模型验收） | `tests/unit/test_pipeline_tools.py`（15 例全绿，含「阶段活动消费默认注册表」，ADR-009）。真实路径已验收：API 模型下 collector/analyst 两阶段各产生一次 `calculator` 调用（`21*2`、`21+21`，`succeeded`） |
| U-10 行为日志事件 | 通过 | `tests/unit/test_observability.py`（ADR-010） |
| U-11 动态编排计划与调度 | 通过（单元级；未接真实模型） | `tests/unit/test_dynamic_pipeline.py`（43 例：计划解析的 12 类非法输入、规划降级、就绪度调度、连坐跳过、交付物取值、`resolve_workflow_name`）与 `tests/unit/test_workflow_dynamic.py`（13 例：规划/步骤活动、父工作流先规划后执行、子工作流实例 ID、终态一致性）。**缺口**：规划质量与「动态相对静态的净收益」没有任何度量，也没有把动态模式纳入真实模型回归（见 `doc/orchestration.md` §3.3） |
| I-06 MCP 工具发现与调用 | 通过（缺跨进程 stdio） | `tests/unit/test_mcp_tools.py` 走 `mcp.shared.memory` 的**真实 MCP 协议往返**（发现、调用、错误还原、目录）；真实流水线验收（2026-09-15）：Workflow 内 2 次 `calculator` 调用落 `tool_calls` 并读回（`21*2`、`21+21` → `42`）；仍缺跨进程 stdio 传输的端到端用例 |
| I-07 可观测数据输出 | 通过 | `tests/unit/test_observability_metrics.py`：Span 与属性/异常、指标去重、Prometheus 文本、`metrics` 表写入（SQLite 与表缺失两种路径）、降级不阻塞；`metrics` 表已由 `app/core/checkpoint.py::MetricRecord` 建出，2026-09-15 在真实 PostgreSQL 上跑通采样落库与 `/api/v1/metrics` 读回（16 条采样），真实 Prometheus 上抓到 `backend`/`dapr-sidecar` 两个 `up` target（43 条 `macp_*` 序列），真实 Jaeger 上查到 `stage.run`/`llm.chat` span；Token 按真实模型名归因（F-03 回归，见 §4.2） |
| I-08 配置热更新 | 通过 | `PATCH /api/v1/config/agents/{agent_id}` 已实现（`doc/api.md` §5.7、ADR-013）：覆盖写 `agent_configs`，阶段活动执行时解析生效配置，无需重启；单元用例 `tests/unit/test_agent_config.py`（合并/校验/回退/表契约），集成用例 `tests/integration/test_config_api.py`（404/422/503/200 与生效值，裸请求可写见 ADR-015）；真实 PostgreSQL 上已跑通写入—读回—新执行生效 |
| I-09 只读巡检接口 | 通过 | `tests/integration/test_inspection_api.py`；覆盖分页、`availability` 区分「未接入」与「零条记录」、默认工具目录接线（`/tools` 返回 4 个注册工具）与 Prometheus 文本端点（`/metrics`）；用 SQLite 内存表与注入目录数据，不等于真实 PostgreSQL/MCP 验收 |
| I-10 Provider 配置读写 | 通过（含真实环境冒烟） | 单元与集成用例：`tests/unit/test_provider_config.py`（26 例：表契约、合并顺序、Redis 命中/回源/回填、镜像失败降级、密钥脱敏、字段校验）与 `tests/integration/test_provider_config_api.py`（11 例：422/503/200、显式 null 清除、provider 切换、裸请求可写）。2026-09-15 真实环境冒烟（本机 PostgreSQL 5433 + Redis 6380 + uvicorn）：`PUT` 不带任何令牌 → `200` 且响应不含密钥；`GET` 回读生效值；`/providers` 反映新值；删除 `provider:config` 后 `GET` 仍返回存储值并自动回填缓存。测试数据已清理 |

2026-09-15（M4 收口后）：`uv run pytest -q` → **294 passed / 1 failed**，
失败项与上面同一处，仍属成员 A 的过期前置条件；新增
`tests/unit/test_metrics_table_schema.py` 校验 `metrics` 表 DDL 与索引契约。

2026-09-15（I-08 落地后）：`uv run pytest -q` → **322 passed / 1 failed**，
失败项仍是上面同一处。新增 `tests/unit/test_agent_config.py`（13 例：表契约、
合并回退、provider 字段映射、读取失败回退、校验与审计日志）与
`tests/integration/test_config_api.py`（15 例：403 fail-closed、200 生效值、
局部更新、显式 null 清除、404、422、503）。

2026-09-15（A 修复过期前置条件后）：`uv run pytest -q` → **323 passed / 0 failed**。
上面唯一失败项已按本文档约定改为 `set_tool_registry_factory(lambda: None)` 显式构造
「没有注册表」的前置条件，用例意图与两条断言未变；未跳过或删除任何用例。
M4 未闭环的剩余缺口只剩真实流水线的工具调用行为（模型把调用当文本输出，
见 `doc/roadmap.md`），不在测试层面；因此测试全绿不构成 M4 验收通过。

2026-09-15（API 优先接入落地后）：`uv run pytest -q` → **366 passed / 0 failed**。
新增 I-10（Provider 配置读写）的单元与集成用例；默认提供方改为 OpenAI 兼容 API
（ADR-014），覆盖值落 `provider_configs` 并镜像 Redis。剩余缺口不变：**尚未在真实
API 模型下跑通工具调用**，因此 M4 仍是「代码完成、验收未闭环」。

2026-09-15（取消写入令牌后，ADR-015）：`uv run pytest -q` → **359 passed / 0 failed**。
删除 7 个令牌相关用例（`test_config_api.py` 4 例、`test_provider_config_api.py` 4 例，
其中两例为参数化），改为各留 1 例「裸请求即可写入」；`AdminSettings` 与
`get_admin_settings()` 一并移除，因此用例总数下降属于预期，不是跳过或删除有效断言。
`npm --prefix frontend run build` 通过（前端同步删除令牌输入框与 403 分支）。

2026-09-15（M4 真实验收，缺口二关闭）：compose 容器栈 + `deepseek-flash` 真实运行。
提交「用 calculator 计算 21*2」后 Workflow `completed`（collect → analyze → report），
`GET /workflows/{id}/tool-calls` 返回 **2 条** `calculator` 记录（`21*2`、`21+21`，
均 `succeeded`，输出 `{"value": 42}`），报告正文引用 `42`；
`GET /metrics?workflow_id=...` 返回 **20 条**采样，含各阶段 Token
（943/77/1020、1216/128/1344、1858/1193/3051）与 `tool_calls` 指标。
同一任务在 `deepseek-v4-pro` 上也产生 1 条成功调用，可作对照。
据此 M4 由「代码完成、验收未闭环」转为**已完成**；U-09/I-06 的剩余缺口只剩
跨进程 stdio 传输与浏览器端自动化，不影响 M4 结论。

### 4.2 E 系列当前状态（2026-09-15，成员 C D9-10）

三层证据与完整数据见 ADR-016；命令（真实环境验收与并发测量需先配好提供方凭据，
默认提供方已是 OpenAI 兼容 API，缺凭据 fail-fast，见 ADR-014）：

```bash
uv run pytest tests/e2e -q                          # 无容器回归网（6 passed）
MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s   # 真实 compose 验收
uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10
uv run python scripts/measure_recovery.py --hold-seconds 15 --restart-lead-seconds 1
```

**提供方前提**：下表除 E-01/E-02 另有注明外，数据都测于本轮 D9-10 落地时的
Ollama `qwen2.5-coder:7b`（E-03 走 poc 的确定性假模型路径）；此后默认提供方改为
OpenAI 兼容 API，表中「未达成」的结论已在 API 模型下复测通过（见上一条记录），
2026-09-15 晚又用 `gpt-5.5` 把依赖模型的 4 条整体补跑通过（见 §4.3.1），
但**并发与恢复两组性能数据仍未在 API 模型下重取**。

| 用例 | 状态 | 证据 / 缺口 |
| --- | --- | --- |
| E-01 单 Agent 问答 | 通过（结论分提供方） | 真实环境跑通：会话→消息→轮询→`completed`，报告消息落库。答复内容在 Ollama `qwen2.5-coder:7b` 下**未达成**——报告正文是 `{"name": "web_search", ...}` 这样的工具调用 JSON 文本（缺口 F-02）；换 OpenAI 兼容 API（`deepseek-flash`）后达成：报告 1877 字并引用工具返回值 `42`（见 §4.1 与 `doc/roadmap.md` 验证记录） |
| E-02 多 Agent 协作 | 通过（结论分提供方） | 真实环境三步依次完成（`checkpoint.completed_steps=[collect, analyze, report]`，2-4s/条）；同受 F-02 影响（Ollama 下 `tool_calls=0`，工具未真正执行），API 模型下 collector/analyst 各产生一次 `calculator` 调用。2026-09-15 晚以 `gpt-5.5`（API 提供方、`MACP_E2E_TIMEOUT=900`）复测：workflow `completed` 330.9s，报告 3983 字符结构化 Markdown（见 §4.3.1） |
| E-03 故障恢复 | 通过（旧数据有缺口） | `scripts/measure_recovery.py`：不可用 2.62s、**恢复耗时 ≤0.2s（目标 <5s）**、业务状态保留 `completed`/三步齐全；该次演练的 Dapr 终态为 FAILED（缺口 F-05，**已由 B 修复**），修复后的演练需重跑以确认两个终态同时成功。数据取自 poc 的确定性（假模型）路径 |
| E-04 Web 会话管理 | 部分（Web 侧数据路径已验，渲染未验） | API 侧通过：暂停 → 新消息被 409 `SESSION_PAUSED` 拒绝 → 恢复 → 原 Workflow 续跑 `completed`（2026-09-15 晚 `gpt-5.5` 下复测 88.3s 到 `completed`，见 §4.3.1）。Web 侧由成员 D 补自动化（§4.3）：经 nginx 反代跑完「新建任务 → 暂停 → 暂停期提交被拒 → 恢复 → 回读」六步；浏览器**渲染效果**仍需人工按 `doc/deployment.md` 的核对清单确认 |
| E-05 一键部署 | 通过（2026-09-15，成员 D 实跑） | `start.ps1` 退出码 `0` 且 Frontend / Backend / Dapr Sidecar 三段健康检查全部打印 `is healthy`，随后打印 7 行访问地址；`stop.ps1` 退出码 `0` 且 `docker ps -a` 中项目容器全部移除。过程中修复了两个脚本在 Windows PowerShell 5.1 下被 `docker compose` 的 stderr 中断的缺陷（见 §4.3）。容器侧 `/metrics`、`/tools` 与 Prometheus/Jaeger 已由 B/D 验收（`doc/roadmap.md`「M4 代码落地情况」） |
| 并发会话（§3.2） | 通过 | 10 会话/并发度 10：成功率 1.0、HTTP 5xx 0、受理延迟 p50 0.46s、端到端 p50 16.54s / p95 18.61s、Token 合计 18650（621.7/次调用）、工具调用成功率**无样本**（0 次；Ollama 下模型未发起工具调用，F-02 之外的模型行为差异，API 模型下重测后应不再为 0） |

合入 `master` 后（2026-09-15）：`uv run pytest -q` → **371 passed / 0 failed / 5 skipped**
（5 skipped 为需要 compose 的 `test_live_e2e.py`；比先前 378 例少，是 master
删掉 7 个令牌用例所致，不是跳过或删除有效断言）。

缺口状态（详见 ADR-016 F-01～F-06）：
F-01 跨阶段同工具调用被审计主键合并且返回首个结果（A+B，**仍未清**，影响工具链路正确性）；
F-02 真实模型不产出结构化 `tool_calls`（**已关闭**：该现象实测于 Ollama
`qwen2.5-coder:7b`，默认提供方改为 OpenAI 兼容 API 后已复测通过——
`deepseek-flash` 下两阶段各产生一次 `calculator` 调用，报告引用返回值 `42`）；
F-03 Token 采样缺 `model` 标签（C 侧，**已修复**：改为从回调 `metadata["ls_model_name"]`
取真实模型名并在 `run_id` 上传递，回归用例见 `test_observability_metrics.py`）；
F-04 `metrics` 表（B）、`/tools` 接线与 Prometheus 文本端点（D）、A 的失败用例与 I-08
——**三项均已落地**（测试全绿、真实库与容器侧均已验收）；
F-05 poc/故障演练路径业务终态与 Dapr 终态不一致（B，**已修复**：`session_id` 不再硬编码，
CLI 对运行时终态非 `COMPLETED` 即非零码退出）；
F-06 会话/长期记忆未接入编排（`app/memory/` 只有 Protocol，历史消息既不落记忆也不回注
Prompt，仅 `GET /messages` 读取）——**本轮只记录，未处置**。

### 4.3 Web UI 与部署编排（2026-09-15，成员 D D9-10）

`分工.md` §3 里 D 的 D9-10 是「UI 收尾、录制演示、部署文档」，对应 §4.2 表里 E-04/E-05
的 Web 侧与部署脚本两行。本轮落地情况：

| 分工 | 状态 | 落地内容 |
| --- | --- | --- |
| Web 控制台 | 代码未改动，验收补齐 | master 的工作台已覆盖团队状态、任务记录、调用链路与 Token 采样、会话管理；本轮没有改前端代码，补的是验收（见下两行的自动化与核对清单） |
| `start.ps1` / `stop.ps1` 全流程 | 已完成 | 修复两个脚本在 Windows PowerShell 5.1 下被 `docker compose` 的 stderr（构建/停止进度）中断的缺陷：`$ErrorActionPreference = "Stop"` 会把原生命令的 stderr 当成终止性错误，`start.ps1` 因此不做健康检查、不打印地址就退出（容器其实已经起来），`stop.ps1` 对已完成的停止报错并返回非零码。两处都改为在该调用期间临时切到 `Continue` 并以 `$LASTEXITCODE` 判定成败，与文件里 `docker info` / `compose version` / `compose config` 的既有写法一致 |
| 部署文档 | 已完成 | `doc/deployment.md` 补「演示与验收」：E-05 的通过标准（退出码 + 三段健康检查 + stop 后容器为空）、E-04 的自动化命令与浏览器核对清单；并修正模型前置条件——默认提供方已是 OpenAI 兼容 API（ADR-014），原文「宿主机需要运行 Ollama」已过时 |

验证（本机 compose，2026-09-15）：

```bash
uv run pytest -q -p no:cacheprovider
# 359 passed / 0 failed，14.51s（前置：PostgreSQL 5433 + Redis 6380 可达）

MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s \
  -k "frontend or web_ui_session or health"
# 3 passed（E-05 健康与目录、E-05 Web 侧、E-04 Web 侧）

npm --prefix frontend run build
# 通过：tsc --noEmit && vite build，3134 modules，产物 index-*.js 251.80 kB
```

`deploy/start.ps1` → `deploy/stop.ps1` 实跑：两次都得到 `SCRIPT_OK=True / LASTEXITCODE=0`，
中间打印 `Frontend is healthy.` / `Backend is healthy.` / `Dapr Sidecar is healthy.` 与
7 行访问地址；`stop.ps1` 打印 `Services stopped.` 后
`docker ps -a --filter "name=multi-agent-collaboration-platform"` 为空。

#### 4.3.1 模型侧补跑（2026-09-15 晚，真实 API 提供方）

上表下面 item 1 里「依赖模型的 4 条未跑」已于同日补跑。环境：`deploy/start.ps1` 起的
compose 全栈（9 个服务全部 Healthy），模型走宿主机的 OpenAI 兼容网关
（`PUT /api/v1/config/provider` 写入 `provider=openai`、`model=gpt-5.5`、
`base_url=http://host.docker.internal:3000/v1`）。

```bash
MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest tests/e2e/test_live_e2e.py -q -s
# 7 passed, 1 warning in 714.75s（无 skip）
```

结果：E-01/E-02 三步流水线 `completed`（workflow `9b48b152`，330.9s，
`completed_steps=['collect','analyze','report']`、`current_step=None`），报告消息 3983 字符、
是结构化 Markdown 正文而非工具调用 JSON——ADR-016 F-02 在 API 提供方下确认关闭；
I-06 `/workflows/{id}/tool-calls` 返回 `availability=available`；E-04 API 侧
「暂停 → 恢复 → 续跑」88.3s 到达 `completed`。

**两个必须记住的环境约束**：

1. **`MACP_E2E_TIMEOUT` 默认 300s 偏紧。** 单次 LLM 调用实测 24–35s（prompt ≈5000 tokens），
   三步流水线叠加工具轮次后可达 330s。用默认值跑会**偶发**判为超时失败——首轮实测一个
   workflow 用了 318s，恰超 300s 上限。按 API 提供方验收时请显式放大该值。
2. **本机整体没有公网出口，`web_search` 必然失败。** 实测容器内与宿主机访问
   `https://api.duckduckgo.com` 都超时（宿主机 10s 返回 `000`），而 `host.docker.internal:3000`
   的模型网关正常 200。模型若选中 `web_search`，该工具会以
   `ToolExecutionError: 搜索服务不可达: timed out` 落库并重试，进一步拉长耗时。
   要稳定复现，需把 `TOOL_SEARCH_ENDPOINT` 指向可达的搜索服务，或在该环境下不向 Agent
   暴露 `web_search`——两者都属工具层配置（成员 C 范围），本轮**未改**。

**另一个已发现的测试隔离缺口**（本轮未修，仅记录）：把 Provider 覆盖写进 PostgreSQL 之后，
`uv run pytest` 会有 8 条转红——`tests/unit/test_agent_config.py` 3 条、
`tests/integration/test_config_api.py` 2 条、`tests/integration/test_inspection_api.py` 3 条。
原因是这些用例 monkeypatch 了 `AgentSettings` / `list_agent_configs`，却没有屏蔽数据库里
*活的* Provider 覆盖（例如 `test_missing_model` 期望 `missing_model`，实际拿到 `configured`）。
显式 `PUT` 全 `null` 清除覆盖后这 8 条立即恢复通过（38 passed），全量回到
**371 passed / 7 skipped**。建议后续给这批用例加一个「清空 Provider 覆盖」的 fixture。

**未完成 / 未验**（不隐瞒）：

1. 依赖模型的 4 条**已补跑通过**（见 §4.3.1）。仍属未验的是环境性路径：本机无公网出口，
   `web_search` 不可能成功，所以「模型选中 web_search 时流水线仍能产出报告」这条路径
   在本机**无法**验收——E-01/E-02 目前的通过依赖模型当次未选该工具。
2. 浏览器渲染，以及「发消息 → 观察 Agent 执行台推进 → 展开协作详情」仍是人工步骤：
   本轮只给核对清单，没有引入浏览器自动化（前端门禁是类型检查 + 构建，见 §3.3）。
3. 「任务记录」页只显示当前会话最近一次执行（`App.tsx::History`，页面已标注
   「完整历史查询尚未接入」）。做完整历史需要新增 `GET /api/v1/workflows` 一类的列表
   接口，属新增能力，本轮未做；因此**不据此宣称** `分工.md` §7 的「Web UI 可展示任务历史」
   已闭环。

### 4.4 多模态附件、欢迎区引导卡与沙箱可用性（2026-09-17，成员 D）

对应 ADR-021 / ADR-022 / ADR-023 / ADR-024。新增与改动：`app/attachments/`、`attachments` 表、
三个附件接口、发送消息契约加 `attachment_ids` 与 `orchestration_mode`、编排侧四处 `attachments`
透传、前端附件交互与欢迎区重做；同一天第二轮把沙箱在部署里真正启用（`deploy/compose.yaml`
挂宿主机套接字）并把附件原件改为**全类型留档**。

**1. 后端（隔离存储，跑法与 §1 一致）**

```bash
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/macp_test" \
REDIS_URL="redis://localhost:6380/15" ./.venv/Scripts/python.exe -m pytest -q
# 682 passed, 7 skipped, 6 warnings in 10.50s

DATABASE_URL="..." REDIS_URL="..." ./.venv/Scripts/python.exe -m pytest -q \
  tests/unit/test_attachments.py tests/integration/test_attachments_api.py \
  tests/unit/test_dynamic_pipeline.py
# 97 passed in 1.88s
```

两轮合计新增 61 例：附件 48 例（39 单元 + 9 集成）、「附件只进根步骤」的动态编排 3 例、
`tests/unit/test_sandbox_docker_runtime.py` 10 例（探测与容器硬化，用假 client）。
`7 skipped` 与 `6 warnings` 均为既有状态（需真实模型 / Starlette 弃用告警），本轮未新增跳过项。

- `test_attachments_api.py` 覆盖：上传 201、四类 400、挂消息后归属回填、**重复提交只挂一次**
  （第二次进 `unattached_attachment_ids`）、只带图不带文字可发送、`GET .../content` 的三类分支
  （图片 `inline` / 文档 `attachment` / 无字节 `404`）、`DELETE` 的 204/409、删会话级联清附件。
- `test_dynamic_pipeline.py` 新增：附件只进 `depends_on` 为空的根步骤、每个根步骤都拿到、
  静态 `collect` 收到附件。
- `test_sandbox_docker_runtime.py`：套接字连不上时原因里必须点到 `/var/run/docker.sock`；
  **只 `ping` 通不算可用**（镜像不在宿主机也要报出来）；镜像缺失给可行动的错误；
  `SANDBOX_AUTO_PULL_IMAGE` 打开时自拉一次并重试；容器参数逐条钉住（`cap_drop=['ALL']`、
  `read_only`、`network_disabled`、`user=nobody`、`security_opt`、`/app` 被 tmpfs 遮住）。
- `test_attachments.py`：PDF 样本升级为「每个入参一个内容流」，新增 **FlateDecode 压缩流**与
  **多页合并**两条——真实 PDF 的内容流几乎都是压缩的，只留未压缩样本测不到 `zlib.decompress`
  分支。

**2. 前端（无测试框架，仍按「构建 + 无浏览器冒烟 + 预览自检」三步）**

```bash
npm --prefix frontend run build
# 通过：tsc --noEmit && vite build，
# 产物 index-xd77pDwS.css 78.51 kB / index-BRyVa_SR.js 402.86 kB
```

- `rendercheck/workspace-smoke.tsx` → **78/78**（本轮 +3：非图片条目也能打开原件并带下载文件名、
  图片不带 `download`、无原件的历史行不给任何入口）
- `rendercheck/config-smoke.tsx` → **69/69**（无回归）
- `rendercheck/ui-preview.html` 的 jsdom 自检 → **45/45**（本轮 +3：有原件的条目是 `<a>`、
  无原件的历史行是 `<div>` 且没有 `href`、图片不带 `download`）

**3. 构建产物核对（防止样式被静默丢弃）**

构建退出 0 **不等于**样式完整：`config.css` 曾因残缺注释让 esbuild 压缩器静默丢掉约 3KB 规则
（只打 WARNING）。所以改完 CSS 要回查 `dist/assets/*.css` 里类名还在。
本次核对结果是**注意压缩器会把属性选择器的引号去掉**：`[data-kind="text"]` 在产物里是
`[data-kind=text]`，用带引号的形式去 `grep` 会得到 0 命中，误判成规则被丢
（初版就误判过一次）。查的时候用去引号形式。

**4. 真实环境验收（第二轮补齐）**

第一轮欠下的四件事，第二轮逐条做了：

1. **容器已重建 + 真机全链路**（`docker compose up -d --build frontend backend`）：
   - 上传真实 PNG（31220 字节）→ 动态模式发消息 → 25.2 s 完成，`checkpoint.mode=dynamic`、
     `plan_source=llm`；模型**读到了图**：答出「执行边界」与「docker 不可用」——图片块确实进了
     根步骤的提示词。
   - 静态链路：上传一份含代号「青鸢-7」的 `现场记录.md`，报告里原样写出该代号，证明文档正文
     进了 `collect` 步骤。
   - 原件下载（ADR-024）：文本附件 44 字节与上传**逐字节一致**、`text/markdown` +
     `attachment`；解析失败的 PDF 同样能取回 `%PDF-1.4` 开头的原件；图片为 `inline`。
2. **图片理解已接真实视觉模型**：见上，当前 Provider（`openai-compatible` → 本机 3000，
   `gpt-5.5`）确实能看图。**这只证明这条通路是通的**，不构成对模型视觉能力的评测。
3. **沙箱在部署里真正可用**（ADR-023）：`GET /config/sandbox` → `available=true`、`reason=null`；
   容器内探针实测真实执行 `print(sum(range(1,101)))` → `exit=0, stdout=5050, 317 ms`；
   `import os` → `SandboxViolation`（在起容器**之前**被拒）；超时用例 → `timed_out=true,
   exit=124`、3.4 s 后容器被终止；工具层 `CodeExecutionTool.invoke` 同样返回 `docker/exit 0`；
   运行后 `docker ps -a --filter label=macp.role=tool-sandbox` 无残留。

**5. 仍未做 / 仍未验（不隐瞒）**

1. **PDF 仍只有构造样本**：本轮补了 FlateDecode 压缩流与多页合并，但**没有真实扫描件或真实
   排版 PDF**（本机无 PDF 生成库、也无公网样本），「生产 PDF 的抽取成功率」没有数据。
2. **视觉能力没有评测集**：只证明通路可用（上面第 2 条），没有「多少张图能读对」的度量。
3. **`list_attachments_for_messages` 会把 `data` 列读出来再丢掉**：ADR-024 记录的既有低效，
   本轮只加了纯内存判断（没有变差，也没修好）。会话内附件多时这条查询会变重。
4. **动态编排的净收益仍无度量**：与 §4.2 里 U-11 的缺口同源。

## 5. 失败处理约定

- 任一用例失败：先复现，再定位，修复后将失败模式固化为新的测试或本文档约束；
- 对 Dapr/编排等共享行为，先写测试或同步补测试，不允许“看起来正确”代替；
- 每次里程碑结束时在报告记录：运行命令、通过数/失败数、失败原因。

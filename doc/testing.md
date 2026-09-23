# 测试策略与测试计划

> 本文件将原“测试策略”细化为开发前可执行的测试计划。每个里程碑验收时必须能给出本文件
> 对应用例的运行结果。

## 1. 测试层级与运行环境

| 层级 | 范围 | 目录 | 运行前提 |
| --- | --- | --- | --- |
| 单元测试 | 图节点、状态、模型工厂、工具函数、Schema | `tests/unit` | 无需外部服务 |
| 集成测试 | Dapr、Redis、PostgreSQL、MCP、API | `tests/integration` | 默认无需外部服务（可显式指向真实服务） |
| 端到端测试 | REST API + Web UI + Dapr + 部署 | `tests/e2e` | 默认无需外部服务（live 验收需 compose） |

常用命令：

```bash
uv run pytest -m "not integration"        # 单元
uv run pytest                              # 全部可用用例
cd deploy; .\start.ps1                     # 起完整环境
```

> **`uv run pytest` 默认不依赖外部服务**（2026-09-20 收口）。三层 conftest
> （`tests/unit`、`tests/integration`、`tests/e2e`）都在导入 `app.*` 之前把 DSN 钉成内存
> SQLite，并用 autouse fixture 把 Provider 配置的 Redis 镜像换成内存替身，所以单元层、
> 集成层与无容器 E2E 回归网既不需要数据库、也不需要 Redis 即可跑完；只有
> `tests/e2e/test_live_e2e.py` 的验收用例需要完整 compose
> （由 `MACP_E2E_LIVE=1` 显式开启，默认 skip，见 `doc/deployment.md`）。
> 实测：把 `REDIS_URL` 指向不可达端口后 `uv run pytest -q` → **615 passed / 7 skipped，
> 15.15s**（隔离前同一命令 89.24s，多出的时间是每个 E2E 流水线用例约 12s 的连接重试）。
> 历史口径保留：起栈之前 `uv run pytest` 会阻塞在连接重试上（曾出现 30 分钟无结果）、
> 且集成层与 E2E 回归网会读到开发环境的真实配置——这两条在 `eda7758` 修单元层、
> 2026-09-20 补集成层与 E2E redis 替身之前成立。

> **配置类用例要求「无既有覆盖」的干净存储**。`tests/integration/` 里
> `test_config_api.py`、`test_inspection_api.py` 断言的是「环境配置 + 无覆盖」的生效值，
> 而 `provider_configs`（PostgreSQL 事实源）与镜像 `provider:config`（Redis）都是
> **跨运行持久**的：开发环境里通过配置页保存过 provider/model 之后，这些用例会以
> 「存储值优先于环境配置」而失败（表现为 `provider`/`model` 断言不符、`status` 由
> `missing_model` 变成 `configured`）。
>
> 这是**环境隔离问题，不是代码缺陷**，已由 `tests/integration/conftest.py` 结构性消除：
> 集成层默认既不读开发库的 `provider_configs`，也不读开发 Redis 的 `provider:config`
> （DSN 钉内存 SQLite + Provider 配置的 Redis 客户端换内存替身）；同一替身
> 2026-09-20 也补进了 `tests/e2e/conftest.py`（`doc/testing.md` §4.5）。
> 确需对着真实服务跑时，
> 用独立空库与空 Redis DB（`MACP_INTEGRATION_DATABASE_URL`），并**不要为了让用例通过而
> 清空开发环境里的 `provider_configs` 或 `provider:config`**——那是真实配置（含在用凭据）。

> **2026-09-20 收敛**：`tests/integration/conftest.py` 补齐后，「集成层直连开发库/Redis」
> 这一最后缺口关闭。三层 DSN 的覆盖变量为 `MACP_UNIT_DATABASE_URL`、
> `MACP_INTEGRATION_DATABASE_URL`、`MACP_E2E_DATABASE_URL`，均优先于运行环境里已有的
> `DATABASE_URL`。

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

| U-11 | 会话与长期记忆的 Redis 读写 | `session:{id}:messages` List 追加与「读最近 N 条」、`agent:{id}:memory` Hash 覆盖写、序列化往返、客户端不可用时按契约降级（ADR-005 修订，2026-09-16 补） | M4（增量） |
| U-12 | 沙箱 Docker 后端隔离与 fail-closed | 容器隔离边界参数齐全、网络仅在显式允许时打开、超时杀容器且不报成功、Docker 不可用时报 sandbox unavailable、输出超限截断并标记（2026-09-16 补，`38200cc` 只加测试） | M4（增量） |
| U-13 | 会话记忆接线 | 阶段提示词带上会话历史且剔除本轮自己的消息、无历史时提示词逐字不变、终态把报告正文追加进记忆、受理消息时写用户消息（ADR-019，2026-09-20 补，关闭 F-06 的接线部分） | M4（增量） |
| U-14 | 工具失败重试与空输出兜底 | 工具失败沿用同一 `call_id` 重试（首次 + 最多 3 次，最多 4 次尝试）；**只有瞬时故障重试**——入参/策略/无效 SQL 等确定性失败标 `retryable=False`、只尝试一次并落 `tool.retry_skipped`；成功即采用真实输出、耗尽重试记 `failed` 且阶段仍输出；撞工具轮次上限时落 `stage.tool_iteration_limit`，空输出补一次文字提示、仍空则告警并继续（ADR-009 三次修订，2026-09-20 补） | M4（增量） |

| U-11 | 动态编排计划与调度 | 计划解析只收合法形态（未知角色 / 重复 id / 自依赖 / 前向依赖 / 超步数一律整份丢弃）；规划模型抛错或返回垃圾文本时回退固定三步；依赖就绪度调度、失败连坐（含传递闭包）与最终交付物取值；Dapr 侧先规划后执行、子工作流实例 ID 稳定、业务终态与实例终态一致（ADR-019） | M5 |
| U-12 | 多模态附件：分类 / 限额 / 解析 / 提示词 | 类型白名单（图片、文本与代码、pdf/docx/xlsx）与三类拒绝理由（空文件 / 超限 / 格式不支持）**都指名到具体文件**；文本解码的 `gb18030` 回退与可打印率闸门能挡住二进制；docx/xlsx 用标准库抽出段落与单元格；PDF 质量闸门把扫描件判为 `failed`，吃得下真实形态的 **FlateDecode 压缩流**与多页内容流；**内嵌子集字体靠 `/ToUnicode` 解回文字**（十六进制 CID 与字面量 CID 两种写法），**字体码冲突时安全拒绝**而不是产出通顺的乱码；`build_human_content` 无图片时返回**纯字符串**（不改变既有链路），有图片时升级为 content block；失败项必须出现在附件清单里；**原件对所有类型留档**（ADR-021 / ADR-024） | M5 |
| U-13 | 沙箱可用性探测与容器硬化 | 套接字连不上时原因要点到 `/var/run/docker.sock` 并给出可照做的动作；**只 `ping` 通不算可用**（镜像不在宿主机同样报不可用）；`SANDBOX_AUTO_PULL_IMAGE` 打开时自拉一次并重试、拉取失败还原为 `SandboxUnavailable`；探测自身抛异常时仍返回原因而不冒泡；容器参数逐条钉住（`cap_drop=['ALL']`、`read_only`、`network_disabled`、`user=nobody`、`security_opt=['no-new-privileges']`、`/app` 与 `/tmp` 的 tmpfs、`macp.role` 标签）（ADR-023） | M5 |
| U-14 | 真实二进制附件夹具 | 6 份**真实库产出**的文件（fpdf2 / python-docx / openpyxl）逐份跑过抽取：多页 PDF 读到第三页附录、内嵌子集字体靠 `/ToUnicode` 读到正文、两字体冲突**一个字正文都不给**（改走页面图像，ADR-027）、扫描件不填正文且原因点到「文本层」、docx 表格单元格出来、xlsx 声明「公式未求值」；每份都断言 `prepare_upload` 后的 `data` 与磁盘原件**逐字节相同**；夹具目录不允许有没人测的文件 | M5（§4.5） |
| U-15 | 会话级文件工具（Agent 读回附件） | 清单返回可读性（图片按类型算，不只看 `status`）；按 id / 完整名 / **唯一**子串解析附件，歧义时拒绝并列出候选；超 `session_file_max_chars` 截断并标注 `truncated`；图片与解析失败各给可行动提示；`SessionFileRegistry` 按会话追加且 `GET /tools` 静态目录不变；`session_scoped_registry` 在无会话/无条目时空转；**接线到 `advance_pipeline_stage` 后模型确实拿到这两个工具且调用被审计记录**（ADR-025） | M5（§4.5） |
| U-16 | 附件回读的列契约 | 编译 `list_attachments_for_messages` 用的语句，断言选中列里**没有** `data` / `text_content`，`has_original` 是库侧 `data IS NOT NULL`，且 filter / order 仍在（ADR-024 遗留低效的回归）（§4.5） | M5（§4.5） |
| U-17 | 编排层工具枚举不产生 IO（ADR-026） | 注册表 Server 的工具与内置合成、目录只取已发现的缓存（不重新握手）、`disabled` 摘除、重名内置优先、调用路由到真 Server（走真实 MCP 协议往返）、读表失败**只尝试一次**、刷新后目录跟随 | M5（§4.6） |
| U-18 | 扫描版 PDF 的页面渲染（ADR-027） | 真实夹具渲出**结构自校验的 PNG**（签名 + 逐块 CRC + IHDR + 解压行长 + filter 字节全 0），多页按文档序；页数上限生效且**如实报告**少带了几页；单页超体积先重渲再丢页、合计到顶提前停；畸形 / 截断 / 渲染组件缺失一律降级而不抛；展开成逐页图片载荷（名字带「第k页/共N页」、id 沿用父附件）后附件计数**不虚高**；执行时渲染失败 → **这一次执行**降级为 `failed` 而不沿用上传时的说明 | M5（§4.7） |
| U-19 | 阶段执行轨迹的读取契约（§5.17） | 从阶段状态里取回产出、按序的工具调用（含失败调用的原因）与**上游输入**（`input` + `input_from`）；三种「没有轨迹」给三句不同的话（尚未开始 / 正在执行 / 已完成但状态被清理）；动态链路报 `not_integrated` 并说明原因；超长正文与超大工具载荷**截断并标记**（不静默剪）；坏载荷只说明「无法解析」不抛异常；读取器异常**继续上抛**（由接口层归一化成 503，不伪装成「这个阶段没有轨迹」） | M5（§4.8） |
| U-20 | 工作区：路径守卫、登记、目录树、配额（ADR-033 阶段 1/2） | 路径逃逸表 34 例（`..`、绝对路径/盘符/UNC、Windows 保留名与非法字符、NTFS 数据流、超长、段内尾随空格/点、**符号链接指向工作区外一律拒绝**、指向区内允许）；登记默认绑 `sessions/<id>/` 且平台建目录、重复路径 409 语义、`mode` 取值校验；目录树目录优先排序、跳过 `.trash`、**越界符号链接只标记 `outside=true` 不跟随**（用字面路径算相对位置，避免 `resolve()` 跟随越界链接时抛异常）；用量扫描按 `scan_limit` 截断并标记；配额三项（单文件 / 总字节 / 条目数）在**写之前**校验且错误里带当前用量 | M5 后（§4.16） |
| U-21 | 工作区写档位与破坏性动作审批（ADR-033 阶段 2/3） | 只读档位下**写工具不进工具集**（档位决定可用动作集合）；写新文件免审批并自动建父目录、`overwrite=true` 在目标存在时提交审批而新建时仍算新建；配额与越界写各自拒绝；删除进 `.trash/<时间戳>-<名>`（软删除）；审批四条不变量——**一次一授权**（放行后置 `consumed`，再动同一目标要重新申请）、**不覆盖既成决定**（二次决策 409）、**过期不放行**（TTL 后置 `expired`）、**不刷屏**（同一目标复用同一条 pending）；审批按 `workspace_id + kind + target` 匹配（不同目标/工作区/动作互不放行） | M5 后（§4.16） |
| U-22 | 沙箱挂载会话工作区与宿主路径反查（ADR-033 §7） | 沙箱容器参数多一条：只挂**本次会话**的工作区且挂到与 backend 相同的路径；**档位决定 `rw`/`ro`**；`SANDBOX_WORKSPACE_MOUNT=none` 时退回 `/tmp` 不挂；`uid/gid` 配置生效；**不变量——沙箱容器绝不挂宿主 Docker socket**（专例钉住）；宿主路径反查按真实 mountinfo 数据测三种形态（Linux bind 取 source、Docker Desktop 9p/drvfs 取「盘符 + root」拼 `/run/desktop/mnt/host/<盘符>/…`、最长挂载点优先），翻不出来时显式报错并指向 `SANDBOX_WORKSPACE_HOST_ROOT`；`open`/`io`/`pathlib`/`glob` 放行而 `os`/网络/进程执行仍禁 | M5 后（§4.16） |
| U-23 | 出网策略：判定、钉扎与接线（ADR-034） | SSRF 表 24 例（各私网与保留网段边界值、`169.254.169.254`、IPv6 与 IPv4-mapped、十进制与十六进制 IP 写法、`user:pass@`、非 http(s)、端口白名单）；域名规则（`*.` 按 **label 边界**、黑名单优先、allowlist 模式、非法规则的配置错误）；解析失败必须**拒绝而非放行**；内部服务与模型流量两类豁免的边界（同一内网地址：`purpose=model` 放行、`purpose=tool` 拒绝；豁免私网判定**不等于**豁免端口）；**真起本地 HTTP 服务**验钉扎连接、逐跳重校验重定向、重定向上限、响应体上限、被拒时不开 socket；接线两处——工具侧默认取数拒绝映射为 `retryable=false`、MCP `http`/`sse` 在**打开会话时**判定（构造会话工厂保持零 IO，ADR-026） | M5 后（§4.16） |
| U-24 | 强制出网代理与联网沙箱（ADR-034 §4 四项补齐） | 代理两种形态各自成例：`CONNECT` 到私网目标回 403 且在**代理侧**计数、`CONNECT` 到允许目标真的把字节转过去（回显验证）、绝对 URI 转发可到达允许目标、私网目标 403、相对 URI 400（不被误当自建服务）；**代理模式**下客户端不本地解析 DNS（用"解析就失败"的替身钉住）但仍执行 scheme/端口/域名规则；**客户端 → 代理 → 上游**真跑一遍（客户端只连代理，判定与取数在代理侧）、代理侧拒绝如实回给客户端；联网沙箱只接内部网络并带 `HTTP_PROXY`/`NO_PROXY`，未配网络名时不假装受约束、默认仍然禁网；网关的 `_build_opener` 在配了代理环境变量时才挂 `ProxyHandler` | M5 后（§4.16） |
| U-25 | 工作区文件导入（「选择文件夹」的服务端一侧，§5.19） | 嵌套路径落盘并逐项回执（`imported` / `skipped` / `failed`）；文本按 UTF-8 写、**二进制按字节写**；越界路径（`..`）一律拒绝且**不落盘**；同名默认跳过、`overwrite=true` 才覆盖；**整批配额在动盘之前一次判掉**（超限时目录里一个字节都没有）；单次文件数与单文件字节上限各自拒绝；坏 base64 与缺 `path` 报可读错误；端到端另起一例走**真实 HTTP 请求 → 服务层 → 真实磁盘**（TestClient + `tmp_path`），断言导入后目录树随即能看到、越界回 422 `WORKSPACE_PATH_REJECTED`、配额回 409 | M5 后（§4.16） |

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

| I-11 | 注册表与 Agent 目录 API | `doc/api.md` §5.7、§5.9–§5.12：Provider / 模型 / MCP Server 注册表与 Agent 角色目录的 CRUD 契约、404/409/422 校验、预设目录、批量引入去重与 `existing` 计数（ADR-017，2026-09-16 补） | M5 后（ADR-017） |

| I-11 | 多模态附件接口契约 | `POST /api/v1/attachments` 的 201、四类 400（非法 base64 / 空 / 超限 / 格式不支持）；发消息带上 `attachment_ids` 后附件归属回填且**只挂一次**（重复提交进 `unattached_attachment_ids`）；「只带图不带文字」可发送；`GET .../content` 对图片回 `inline`、对文本与文档（**含解析失败项**）回 `attachment` 原件、对无字节的旧行回 404；`DELETE` 未归属 204 / 已发出 409；删会话级联清附件（ADR-021 / ADR-024） | M5 |
| I-12 | 执行边界诊断接口 | `GET /api/v1/config/sandbox`：200 时 `available` / `reason` 的语义；原因**原样透传**后端给出的文案（不再由接口层编兜底话术）；探测自身抛异常时仍回 200 + 原因；`PUT` / `POST` 一律 405（ADR-020 / ADR-023） | M5 |
| I-13 | 会话的历史工作流列表（§5.18） | `GET /api/v1/sessions/{id}/workflows`：未知会话 404、草稿态会话回空列表（**不建会话**）、**乱序写入也必须按 `created_at` 升序返回**（对话编号由位置决定，倒序会让回看的编号整体错位）、只含本会话的工作流、运行中的那条要带出 `current_step` 与 `checkpoint` | M5（§4.9） |
| I-14 | 工作区与审批接口契约（§5.19 / §5.20） | 登记 201 与默认路径、列表按会话过滤、目录树出参形状与 `depth` 越界 422；**错误码逐一**：404 `WORKSPACE_NOT_FOUND` / 409 `WORKSPACE_EXISTS` / 422 `WORKSPACE_PATH_REJECTED` / 409 `WORKSPACE_QUOTA_EXCEEDED` / 409 `WORKSPACE_APPROVAL_REQUIRED` / 503 `WORKSPACE_DISABLED` / 503 `WORKSPACE_ROOT_UNAVAILABLE`；`session_id` 非空时**先校验会话**（非法 id 回 404 而不是把存储层 UUID 解析错误冒成 500）；提档 PATCH 200 且回显操作者、非法档位由 Pydantic 收口；审批列表的 `status` 过滤与 `pending` 计数、决策 200、二次决策 409、非法 decision 422 | M5 后（§4.16） |
| I-15 | 出网策略只读投影（§5.21） | `GET /api/v1/config/egress` 回配置口径（模式、白/黑名单、内部服务、端口、模型豁免、跳数）与进程内拒绝计数；**没有写接口**（能改策略就是绕过边界的路）；策略构造失败（配置写错）返回 503 而不是静默用默认值 | M5 后（§4.16） |

### 2.3 端到端测试（E）

| 编号 | 用例 | 步骤 | 通过标准 | 里程碑 |
| --- | --- | --- | --- | --- |
| E-01 | 单 Agent 问答 | 创建会话 → 发消息 → 轮询/取结果 | 返回结构化回答 | M5 |
| E-02 | 多 Agent 协作 | 提交三步任务 → 观察状态流转 | 三步依次完成并生成报告 | M5 |
| E-03 | 故障恢复 | 执行中断掉 backend → 重启 | 任务从断点续跑成功，无状态丢失 | M3 起可演练，M5 验收 |
| E-04 | Web 会话管理 | UI 创建会话、发消息、暂停/恢复 | UI 与 API 状态一致 | M5 |
| E-05 | 一键部署 | `start.ps1` → 健康检查 → `stop.ps1` | 全部服务健康 | M5 |
| E-06 | MCP 跨进程 stdio 链路 | 真实启动 `python -m app.mcp.server` 子进程，经 stdio 握手并发现工具 | 子进程可启动、stdout 不被日志污染、工具目录跨进程一致（2026-09-16 补，关闭 I-06 的遗留缺口） | M4（增量） |
| E-07 | 多轮上下文继承 | 同一会话提交两次 → 第二轮阶段的 collector 提示词含第一轮用户消息与助手报告，会话记忆按轮次累积 user/assistant（ADR-019，2026-09-20 补） | 第二轮回答建立在第一轮之上 | M5 后（ADR-019） |

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

样式实现为**手写 CSS**（按工作台/配置/记录三个视图分区组织，约 6800 行），
不是建议方案里的 TailwindCSS，偏差与原因见 ADR-021。

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
| Agent 团队 | `App.tsx::AgentTeamPage` → `config/AgentPanel.tsx` | 角色路由：一次 `GET /config/agents` 取回角色与 `available_models`；角色**一行多个方块**，方块只显示摘要（**角色图标**（按 `role` 解析，ADR-029）/ 名字 / `role · 状态` / 生效模型 / Temperature / 覆盖项数），点击方块在网格下方展开 `AgentTuningPanel` 编辑，再点一次或「收起」关掉；`override_keys` 高亮「已覆盖」字段；「清除全部覆盖」发 6 个 `null`；`activeAgentId` 只做当前阶段高亮 |
| 任务记录 | `records/RecordsPage.tsx`（容器与行渲染在 `records/Inspection.tsx`） | 四分区副路由：`runs` 运行记录、`calls` 工具调用（§5.5）、`metrics` 指标采样（§5.6）、`sessions` 历史会话（§5.13）。采样有 Workflow 时按 `workflow_id` 取并轮询（终态停），无 Workflow 时退回全局采样；历史会话调 `GET /api/v1/sessions` 分页列出，点选按 `latest_workflow_id` 恢复执行台并回工作台；`Records` 统一「加载中 / 失败 / 未接入 / 无记录 / 有数据」五态，分页仅在多页时出现 |

「工具与配置」页的分区（前三个可写，第四个只读）：

| 分区 | 组件 | 验证要点 |
| --- | --- | --- |
| Provider | `ProviderPanel.tsx` + `ModelSection.tsx` | 预设目录预填 `preset_type`/`api_type`/`base_url`；`api_key` 输入框留空 = 不修改；删除仍有启用模型的 Provider 时先用 `409 PROVIDER_IN_USE` 拦一次，再让用户确认 `?force=true`；`openai-compatible`（自定义）的**图标位渲染加号**（`.cfg-mark-add`），不再把「自定义」当 monogram 文字塞进方块 |
| 同上 · 批量引入 | `ModelSection.tsx::BatchImportModal` | 「从远端发现」**只在点击时**发起（不在挂载时调用）；已登记模型置灰计入 `existing`；重复提交返回 `skipped` 而不报错 |
| 同上 · 特化调参 | `ModelSection.tsx::ModelTuningForm` | 只提交被改动字段；清空数字输入 = 显式 `null`（回到未设置）；`PATCH` 不发 `provider_id`；改模型名时按 `modelCapabilities.ts` 带出常见模型的**上下文上限**，用户手改过（`contextTouched`）之后不再覆盖 |
| 同上 · 手动登记 | `ModelSection.tsx::ModelCreateModal` | 与特化调参同一套自动带出与文案（`describeContextHint`）；识别不到常见模型时**留空**由用户手填，不做正则猜测 |
| 默认路由 | `DefaultRoutePanel.tsx` | `default_llm_model_id` 非空时展示解析出的注册表来源；悬空 id 给出提示而不是报错 |
| MCP 工具 | `McpPanel.tsx` | 工具卡片只显示 `name` / 截断后的 `description` / 开关与可用性；完整 `input_schema` 收进**默认折叠**的 `<details>`，展开时才调 §5.3。工具级选项里**只有 `disabled` 有界面出口**：`disabled` 现在真的会把工具从 Agent 的工具集里摘掉（ADR-026），`allowAutoExecution` 则**仍然不消费**——它要表达的是「需要人工确认」，而 HITL 还没做，所以**不给它做界面开关**，否则就是一个点了没效果的假开关 |
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

`workspace-smoke`（ADR-018 + ADR-031）覆盖工作台**四块**视图，同样是「显式 props 驱动」那一类：
`GraphCanvas` / `layoutCollaboration`（ADR-028：串行 / 并行波次两种排布的**坐标**与首尾端子、
弧线确为三次贝塞尔、连线激活口径、悬停详情与工具胶囊、`minHeight` 撑高与**不满压**，
以及侧栏紧凑档不写任务与产出）、`AgentStageModal`（身份与绑定、`pending` 说
「等待前置阶段」而不是 workflow 词汇「排队中」、角色未就绪时的降级、明示推理过程尚未
对外暴露）、`RunActivity`（对话流内联执行轨迹：跑完的步骤收成一行且摘要仍带阶段名与工具次数、
执行中只摊开正在跑的那一步、失败调用连着原因、轨迹缺席时转述服务端给的原因且整条链路
只重复一次）与 `TaskUsagePanel` + `groupUsage`（**同一指标多次采样并排列出、断言不求和**）。
另有 `Markdown`（ADR-031：标题 / 加粗 / 行内代码 / 表格滚动容器 / 围栏代码块语言角标 /
任务列表只读勾选框六类结构，且断言**正文里不再出现未渲染的 `**` 与 `| ---`**——
只查元素存在会漏掉「记号原样吐出」这个真正的病根）与 `Disclosure`（展开态带
`aria-expanded` + `aria-controls`、收起态**不写**指向空元素的 `aria-controls`）。
另有一组读源文件的静态断言，固定「假选择已删除」：`styles.css` 不含 `.decision-*`、
`App.tsx` 不含「主决策 / 自动分配」、卡片点击走 `setDetailStage` 而非 `setInspectorOpen`、
`WorkflowInspection` 已从 `Inspection.tsx` 删除；并固定「正文不再走 `<p>{content}</p>`」、
「`.run-event` 规则已清干净」、「轨迹渲染只有一份实现（弹窗与对话流同调 `TraceParts`）」。

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
- **`file://` 下 Windows 的盘符会混进 `pathname`（2026-09-21 修）**。mock 用
  `new URL("<请求路径>", location.href).pathname` 查种子表，而 `file:///C:/Users/.../ui-preview.html`
  下 `/api/v1/agents` 会解析成 `file:///C:/api/v1/agents`，`pathname` 变成 **`/C:/api/v1/agents`**
  —— 种子里一条都命中不了，整页空数据，界面上只剩「预览未收录：GET /C:/api/v1/agents」。
  已按 `^\/[A-Za-z]:(?=\/)/` 剥掉盘符前缀（HTTP 下是空操作）。
  **这个缺口藏了很久**：离线自检（jsdom）用的 `location` 是 HTTP 形状，`pathname` 本来就对；
  而「双击打开」这条真实用法从来没被自动检查覆盖。**声明的用法与自动检查的用法不一致，
  缺口就会一直留着** —— 改完预览后请用 CDP 打开 `file://` 实跑一遍（见 §4.11 的「取证手段」），
  不要只用 jsdom 自检通过就收工。
- 产物 `ui-preview.html` 与中间产物 `.preview-bundle.*` 已进 `.gitignore`，不入库。

预览产物本身可以离线自检（`jsdom` 挂载 + 点一遍侧栏、副路由、四个配置分区、会话生命周期，
以及 2026-09-17 新增的欢迎卡与附件：三张卡在且竖排居中、**先手动切「固定三步」再点卡片**
验证模式被切回「自动编排」、切开会话后气泡里的**四种**附件形态（图片 / 文档 / 解析失败 /
无原件的历史行）与「有原件的才是链接」，共 45 项断言）。

> **别把它当成预览页的完整验收。** jsdom 里的 `location` 是 HTTP 形状，`file://` 特有的问题
> （盘符混进 `pathname`，见上一节）它照不到；而「双击 HTML」才是这个预览的真实用法。
> 改过 `preview.tsx` 就走一遍 CDP + `file://` 实跑（2026-09-21 实测：仅 jsdom 自检通过时，
> 预览页在浏览器里是**整页空数据**）。
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

| I-06 MCP 工具发现与调用 | 通过（跨进程 stdio 已补齐） | `tests/unit/test_mcp_tools.py` 走 `mcp.shared.memory` 的**真实 MCP 协议往返**（发现、调用、错误还原、目录）；真实流水线验收（2026-09-15）：Workflow 内 2 次 `calculator` 调用落 `tool_calls` 并读回（`21*2`、`21+21` → `42`）。原先遗留的「跨进程 stdio 传输端到端用例」已由 E-06（`tests/e2e/test_mcp_stdio_e2e.py`，2026-09-16）补齐 |

| U-11 动态编排计划与调度 | 通过（单元级；未接真实模型） | `tests/unit/test_dynamic_pipeline.py`（43 例：计划解析的 12 类非法输入、规划降级、就绪度调度、连坐跳过、交付物取值、`resolve_workflow_name`）与 `tests/unit/test_workflow_dynamic.py`（13 例：规划/步骤活动、父工作流先规划后执行、子工作流实例 ID、终态一致性）。**缺口**：规划质量与「动态相对静态的净收益」没有任何度量，也没有把动态模式纳入真实模型回归（见 `doc/orchestration.md` §3.3） |
| I-06 MCP 工具发现与调用 | 通过（缺跨进程 stdio） | `tests/unit/test_mcp_tools.py` 走 `mcp.shared.memory` 的**真实 MCP 协议往返**（发现、调用、错误还原、目录）；真实流水线验收（2026-09-15）：Workflow 内 2 次 `calculator` 调用落 `tool_calls` 并读回（`21*2`、`21+21` → `42`）；仍缺跨进程 stdio 传输的端到端用例 |
| I-07 可观测数据输出 | 通过 | `tests/unit/test_observability_metrics.py`：Span 与属性/异常、指标去重、Prometheus 文本、`metrics` 表写入（SQLite 与表缺失两种路径）、降级不阻塞；`metrics` 表已由 `app/core/checkpoint.py::MetricRecord` 建出，2026-09-15 在真实 PostgreSQL 上跑通采样落库与 `/api/v1/metrics` 读回（16 条采样），真实 Prometheus 上抓到 `backend`/`dapr-sidecar` 两个 `up` target（43 条 `macp_*` 序列），真实 Jaeger 上查到 `stage.run`/`llm.chat` span；Token 按真实模型名归因（F-03 回归，见 §4.2） |
| I-08 配置热更新 | 通过 | `PATCH /api/v1/config/agents/{agent_id}` 已实现（`doc/api.md` §5.7、ADR-013）：覆盖写 `agent_configs`，阶段活动执行时解析生效配置，无需重启；单元用例 `tests/unit/test_agent_config.py`（合并/校验/回退/表契约），集成用例 `tests/integration/test_config_api.py`（404/422/503/200 与生效值，裸请求可写见 ADR-015）；真实 PostgreSQL 上已跑通写入—读回—新执行生效 |
| I-09 只读巡检接口 | 通过（本轮 3 条转红，见 §4.4） | `tests/integration/test_inspection_api.py`；覆盖分页、`availability` 区分「未接入」与「零条记录」、默认工具目录接线（`/tools` 返回 4 个注册工具）与 Prometheus 文本端点（`/metrics`）。**2026-09-16 修正**：此前写的「用 SQLite 内存表与注入目录数据」只对巡检存储成立——该文件没有屏蔽 Provider 覆盖，而 `tests/integration/` 又没有固定 DSN 的 conftest，Provider 相关 3 条实际读的是开发库，因此不等于真实 PostgreSQL/MCP 验收 |
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
| E-03 故障恢复 | 通过 | `scripts/measure_recovery.py`：不可用 2.62s、**恢复耗时 ≤0.2s（目标 <5s）**、业务状态保留 `completed`/三步齐全；该次演练的 Dapr 终态为 FAILED（缺口 F-05，**已由 B 修复**）。数据取自 poc 的确定性（假模型）路径；**2026-09-20 已在真实 API 提供方下重测，两个终态同时成功，见 §4.6** |
| E-04 Web 会话管理 | 部分（Web 侧数据路径已验，渲染未验） | API 侧通过：暂停 → 新消息被 409 `SESSION_PAUSED` 拒绝 → 恢复 → 原 Workflow 续跑 `completed`（2026-09-15 晚 `gpt-5.5` 下复测 88.3s 到 `completed`，见 §4.3.1）。Web 侧由成员 D 补自动化（§4.3）：经 nginx 反代跑完「新建任务 → 暂停 → 暂停期提交被拒 → 恢复 → 回读」六步；浏览器**渲染效果**仍需人工按 `doc/deployment.md` 的核对清单确认 |
| E-05 一键部署 | 通过（2026-09-15，成员 D 实跑） | `start.ps1` 退出码 `0` 且 Frontend / Backend / Dapr Sidecar 三段健康检查全部打印 `is healthy`，随后打印 7 行访问地址；`stop.ps1` 退出码 `0` 且 `docker ps -a` 中项目容器全部移除。过程中修复了两个脚本在 Windows PowerShell 5.1 下被 `docker compose` 的 stderr 中断的缺陷（见 §4.3）。容器侧 `/metrics`、`/tools` 与 Prometheus/Jaeger 已由 B/D 验收（`doc/roadmap.md`「M4 代码落地情况」） |
| 并发会话（§3.2） | 通过 | 10 会话/并发度 10：成功率 1.0、HTTP 5xx 0、受理延迟 p50 0.46s、端到端 p50 16.54s / p95 18.61s、Token 合计 18650（621.7/次调用）、工具调用成功率**无样本**（0 次；Ollama 下模型未发起工具调用）。**2026-09-20 已在真实 API 提供方下重测（p50 54.63s、Token 4241.1/次、工具成功率 0.6798），见 §4.6；上表数字以 §4.6 为准** |

合入 `master` 后（2026-09-15）：`uv run pytest -q` → **371 passed / 0 failed / 5 skipped**
（5 skipped 为需要 compose 的 `test_live_e2e.py`；比先前 378 例少，是 master
删掉 7 个令牌用例所致，不是跳过或删除有效断言）。

缺口状态（详见 ADR-016 F-01～F-06）：
F-01 跨阶段同工具调用被审计主键合并且返回首个结果（A+B，**已修复** 2026-09-20：调用 ID
派生键加入阶段名，`tool_call_id(..., stage=...)` + `run_role_stage` 按阶段构造 `ToolCaller`，
同阶段重放仍得到同一 ID；回归用例见 `tests/unit/test_pipeline_tools.py`
（`test_role_stage_scopes_tool_call_ids_by_stage`、
`test_tool_caller_stage_is_part_of_the_derived_call_id`）与
`tests/e2e/test_pipeline_e2e.py::test_each_stage_call_is_audited_with_its_own_arguments`）；
F-02 真实模型不产出结构化 `tool_calls`（**已关闭**：该现象实测于 Ollama
`qwen2.5-coder:7b`，默认提供方改为 OpenAI 兼容 API 后已复测通过——
`deepseek-flash` 下两阶段各产生一次 `calculator` 调用，报告引用返回值 `42`）；
F-03 Token 采样缺 `model` 标签（C 侧，**已修复**：改为从回调 `metadata["ls_model_name"]`
取真实模型名并在 `run_id` 上传递，回归用例见 `test_observability_metrics.py`）；
F-04 `metrics` 表（B）、`/tools` 接线与 Prometheus 文本端点（D）、A 的失败用例与 I-08
——**三项均已落地**（测试全绿、真实库与容器侧均已验收）；
F-05 poc/故障演练路径业务终态与 Dapr 终态不一致（B，**已修复**：`session_id` 不再硬编码，
CLI 对运行时终态非 `COMPLETED` 即非零码退出）；
F-06 会话/长期记忆未接入编排（当时 `app/memory/` 只有 Protocol，历史消息既不落记忆也不
回注 Prompt，仅 `GET /messages` 读取）——**当时只记录，未处置**。2026-09-16 合入 C-1 后
Redis 实现已落地（`app/memory/redis_store.py`，U-11 覆盖），但编排与 API 仍无调用方，
即**「实现已就绪、接线未做」**，见 §4.4。**2026-09-20 会话记忆接线完成**（ADR-019）：
受理用户消息与终态回写报告时写记忆，阶段活动读最近 10 条注入提示词（剔除本轮）——
用例 U-13 与 E-07；长期记忆仍是「实现就绪、无调用方」，需显式「记住这个」交互或抽取流程，
届时另开 ADR。
F-07 阶段空 content 静默降级下游（C 侧，**2026-09-20 新增并同日处置**）：真实 API 提供方下
模型撞上工具轮次上限后仍在请求工具、阶段没有文字产出，但阶段仍记 `completed`，
下游拿到空上游内容、最终报告退化成「输入缺失/阻塞」说明。精确统计：21 个工作流 63 份阶段
载荷中 14 份（22%）为空。处置为「空输出补一次文字提示、仍空则告警并继续」+ 新增
`stage.tool_iteration_limit` 告警；处置后同批 12 份载荷 0 份为空。详见 §4.6、ADR-009 修订
与 ADR-016 修订。
F-08 容器部署下 code_execution 必然不可用（C 侧，**2026-09-20 新增，未处置**）：
backend 容器没有挂载 `/var/run/docker.sock`，沙箱 Docker 后端报
`沙箱不可用: Docker 守护进程不可用`（真实模型 live 运行里 10 次 code_execution 全部失败）；
宿主机直跑后端时可用。修法（挂载 socket 或改用独立沙箱服务）属部署决策，见 §4.7 与 ADR-009 修订 2。

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

**2026-09-16 更新（合入 PR #9 与 C-1 后）**：单元层那部分**已修**——`eda7758` 让
`tests/unit/conftest.py` 在导入 `app.*` 之前把 DSN 钉成内存 SQLite，
`tests/unit/test_agent_config.py` 的同类失败归零。**集成层仍未修**：实测
`uv run pytest -q` → **606 passed / 5 failed / 7 skipped**（618 例），5 条失败全部来自
`tests/integration/`（`test_config_api.py` 2、`test_inspection_api.py` 3）——该目录没有
conftest，DSN 落到 `app/core/storage.py` 的默认开发库，而这些用例只 monkeypatch 了
`get_settings` / `agent_config_reader`，没有屏蔽库里的 `provider_configs` 行。
另需修正上文计数：本轮实测的单元层失败是 **2 条**
（`test_resolve_returns_base_when_agent_has_no_override`、
`test_resolve_falls_back_when_read_fails`），不是「3 条」，以实测为准。
收敛后的建议不变：给 `tests/integration/` 补 conftest（按 §1 的 DSN 钉法）。

**未完成 / 未验**（不隐瞒）：

1. 依赖模型的 4 条**已补跑通过**（见 §4.3.1）。仍属未验的是环境性路径：本机无公网出口，
   `web_search` 不可能成功，所以「模型选中 web_search 时流水线仍能产出报告」这条路径
   在本机**无法**验收——E-01/E-02 目前的通过依赖模型当次未选该工具。
2. 浏览器渲染，以及「发消息 → 观察 Agent 执行台推进 → 展开协作详情」仍是人工步骤：
   本轮只给核对清单，没有引入浏览器自动化（前端门禁是类型检查 + 构建，见 §3.3）。

3. 「任务记录」页只显示当前会话最近一次执行（`App.tsx::History`）。**已闭环**
   （2026-09-16 回填）：PR #9 新增 `GET /api/v1/sessions` 枚举接口（`doc/api.md` §5.13）
   与删除接口（§5.14），记录页因此从三分区扩为四分区并新增「历史会话」分区，
   `doc/roadmap.md` 的 M5 状态同步记为已闭环。

### 4.4 合入 PR #9 与 C-1 后（2026-09-16）

M5 闭环后合入的两批改动（PR #9 的注册表/工作台，C-1 的记忆/MCP/沙箱/测试基座）当时没有
回填本文档，此处按实测补齐。运行环境：本机 compose 全栈在跑（backend 8000、PostgreSQL
5433、Redis 6380 等），生效 Provider 仍是 2026-09-15 保存的
`openai` / `deepseek-flash` / `https://api.deepseek.com`。

```bash
uv run pytest -q          # 618 用例：606 passed / 5 failed / 7 skipped，17.01s
uv run pytest -q -rs      # 7 skipped 全部为 MACP_E2E_LIVE=1 控制的 test_live_e2e.py
```

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 用例规模 | 371 → 618 | PR #9 的 `tests/integration/test_registry_api.py`（52 例）与 C-1 的 `test_memory_redis_store.py`（19）、`test_sandbox_docker_runtime.py`（13）、`test_mcp_stdio_e2e.py`（5）等未在旧记录中体现 |
| 单元层隔离 | 已修 | `eda7758`：`tests/unit/conftest.py` 钉内存 SQLite，`test_agent_config.py` 归零 |
| 集成层隔离 | **已修**（2026-09-20） | `tests/integration/conftest.py`：DSN 钉内存 SQLite + Provider 配置的 Redis 客户端换内存替身，5 条失败归零；根因更正见 §4.5 |
| 记忆实现 | 会话记忆**已接线**（2026-09-20） | U-11 覆盖 `app/memory/redis_store.py`；会话记忆写点（`app/api/main.py`、`finalize_activity`）与读点（`advance_pipeline_stage` → 提示词）见 ADR-019、U-13、E-07；长期记忆仍无调用方 |
| MCP 跨进程 stdio | 已补 | E-06（`tests/e2e/test_mcp_stdio_e2e.py`）真实启动 `python -m app.mcp.server` 子进程 |
| 沙箱 Docker 后端 | 只补了测试 | `38200cc` 的 diff 内无 `app/sandbox` 变更，提交信息所述的「隔离参数」没有代码改动 |
| `.env_example` | 已补、此前无文档引用 | `31d9cef` 在原文件上补 65 行（`TOOL_` / `MCP_` / `SANDBOX_` / `OBS_` 四段）；`doc/deployment.md` 的环境变量表已补上指向它的说明 |

### 4.5 集成层测试隔离收口（2026-09-20）

本节关闭 §4.4 记录的「集成层隔离 未修」，并把根因改写为实测结论。

**根因（比原记录更具体）**：失败的 5 条用例（`test_config_api.py` ×2、
`test_inspection_api.py` ×3）并非只读 PostgreSQL 的 `provider_configs` 行，**主泄漏源是
本机 Redis 的活镜像 `provider:config`**——`app/core/provider_config.py::provider_config_row()`
的解析顺序是 Redis 优先。反证：把 `DATABASE_URL` 单独指向内存 SQLite 后重跑，5 条仍然失败
（2026-09-20 实测）；只有同时替换 Redis 客户端，失败才归零。原因是 `tests/unit/conftest.py`
已有 `set_redis_factory` 内存替身，而 `tests/integration/` 既没有 conftest，也就没有替身，
于是直接读到开发环境里 2026-09-15 保存的 `openai` / `deepseek-flash`（含在用凭据）。

**处置**：新增 `tests/integration/conftest.py`（DSN 钉内存 SQLite，导入 `app.*` 之前生效；
autouse fixture 注入 Provider 配置的 Redis 内存替身）与
`tests/integration/test_isolation_contract.py`（把「看不到开发环境运行期配置」固化为用例，
删掉 conftest 任一半即转红）。

```bash
# 修复前：集成层 5 failed / 101 passed
uv run pytest tests/integration -q
# 修复后
uv run pytest tests/integration -q        # 108 passed，2.12s（Redis 不可达时同样通过）
uv run pytest -q                          # 613 passed / 0 failed / 7 skipped，15.49s
                                          # （仅集成层修复时的快照，见下方 E2E 续修）
# 反向对照（不经 conftest 的裸进程）：仍能读到开发环境的活覆盖
# uv run python -c "from app.core.provider_config import provider_config_row;
#                   print(provider_config_row()['model'])"   → deepseek-flash
```

`7 skipped` 仍全部是 `MACP_E2E_LIVE=1` 控制的 `test_live_e2e.py`；用例数 618 → 622
（集成层 2 条 + E2E 层 2 条隔离契约用例）。

**同日续修：E2E 回归网的同类泄漏（已收口）**。首轮只处理了集成层，随后实测发现
`tests/e2e/` 同样只钉了 DSN：它的 Provider 配置镜像仍连本机 Redis，语义上不是「无覆盖」，
且 Redis 不可达时该批用例从 6.63s 涨到 80.09s。修法与集成层完全一致——
`tests/e2e/conftest.py` 增加 `MemoryRedis` 替身 + autouse fixture，
`tests/e2e/test_regression_net_isolation.py` 固化契约（文件名不与集成层重名：pytest 默认
导入模式下，不同目录的同名测试模块会互相顶掉）。先写用例确认 RED（读到
`provider:config` 的 `deepseek-flash`），再上替身转 GREEN：

```bash
uv run pytest tests/e2e -q                     # 13 passed，6.31s（Redis 不可达时）
REDIS_URL=redis://localhost:6399/0 uv run pytest -q
                                               # 615 passed / 7 skipped，15.15s
```

两处契约用例的失败信息只打印 `provider` / `model`，不带 `api_key`——覆盖行里的凭据
不应因为一次断言失败就进测试日志。

### 4.6 真实 API 提供方下的重测（2026-09-20）

compose 全栈 + 真实提供方（`openai` / `deepseek-flash` / `https://api.deepseek.com`，
由 `PUT /api/v1/config/provider` 保存）上的重测，取代 §4.2 里 Ollama/poc 路径的数字
（详细口径与对照见 ADR-016 的 2026-09-20 修订）。

| 场景 | 命令 | 结果 |
| --- | --- | --- |
| live 验收（E 系列 7 条） | `MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest tests/e2e/test_live_e2e.py -q -s` | **7 passed，190.37s** |
| 并发（E 系列性能） | `uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10 --timeout 900` | 10/10 完成、成功率 1.0、总耗时 75.836s；受理延迟 p50 0.348s；端到端 p50 **54.628s** / p95 74.571s；Token 合计 432596（4241.1/次）；工具调用成功率 **0.6798（138/203）** |
| 恢复（E-03） | `uv run python scripts/measure_recovery.py --timeout 900` | 重启 3.07s；**恢复 2.48s（目标 <5s）**、回填等待 0.00s；业务 `completed` 三步齐全且 Dapr 终态 **COMPLETED** |

**F-01/F-06 的真实链路定向验证**（同一次 live 会话 `33c0700b-…`）：

- 第一轮要求「两阶段各调一次 calculator（21*2 / 21+21）」→ workflow `completed`（21.0s），
  `GET /workflows/{id}/tool-calls` 返回 **4 条** `calculator` 记录（`21*2` ×2、`21+21` ×2，
  均 `succeeded`、ID 互不相同）——修复前同 (scope, index, tool) 的两阶段调用会被合并成 1 条
  并返回首阶段缓存，这里是 F-01 修复后的真实链路证据；报告 1962 字且含 `42`。
- 第二轮在同一会话追问「不要调用工具，只根据上文回答 21*2 的结果」→ `completed`（27.1s），
  回答明确引用「阶段一 `21*2` 与阶段二 `21+21` 的结果均为 42」——**F-06 多轮上下文继承在
  真实模型下成立**（会话记忆写入 + 阶段提示词注入生效）。

**同轮暴露并已处置的 F-07（空输出静默降级）**：精确统计（按「键对应阶段自身的
`content`」）全量 21 个工作流 63 份载荷中 **14 份（22%）为空**，多数只空 1 个阶段。
真因是**模型撞上工具轮次上限（4）后仍在请求工具**（日志里 collect/analyze 常有 7–9 次工具
调用、`pending_tool_calls=2..4`），阶段因此没有文字产出，而 workflow 仍 `completed`。
已排除 F-01/F-06 改动（live 用新会话、无历史；无容器回归网全绿）。
**处置（方案 2）**：补一次「请用文字给出本阶段结论，不要再调用工具」的重试，仍为空则落
WARNING 并继续；并新增 `stage.tool_iteration_limit` 告警让该成因可观测
（实现与判空坑见 ADR-009 修订）。判空只看 `content`——首版把「还有待处理 tool_calls」
当作有产出，实测仍 8/12 为空，修正后：

```bash
# 处置后同一批 live 复测
MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest tests/e2e/test_live_e2e.py -q -s
# → 7 passed，343.69s（处置前 168.87s：每个空阶段多一次模型调用）
# 新增 4 个工作流 12 份阶段载荷：空 content 0 份；
# 日志 7 × stage.tool_iteration_limit + 5 × stage.empty_content action=retry，0 × continue_with_empty
uv run pytest -q          # 627 passed / 7 skipped（新增 U-13 延伸用例 4 条）
```

### 4.7 工具失败重试与"3 次过后必须输出"（2026-09-20）

按人类要求实现：**工具调用失败重试 3 次，3 次过后不再重试、按实际结果输出**
（实现与口径见 ADR-009 修订 2）。

| 项 | 处理 |
| --- | --- |
| 重试粒度 | 单次**逻辑调用**：首次 + 最多 3 次重试 = 最多 4 次尝试（`TOOL_CALL_RETRY_LIMIT = 3`） |
| 调用 ID | 全程沿用同一 `call_id`；审计层把 `failed` 行重置为 `running` 再执行，**一次逻辑调用只占一行** |
| 可观测 | `event=tool.call … attempt=N max_attempts=4`、`event=tool.retry … next_attempt=N+1`（WARNING） |
| 3 次过后 | 失败观察交给模型；模型不给文字时由 F-07 兜底补一次文字提示——输出基于真实结果（含失败原因），流水线不中断 |

```bash
# 单元：修复前只尝试 1 次（RED），修复后
uv run pytest tests/unit/test_pipeline_tools.py -q      # 24 passed（含新增 2 条）
uv run pytest -q                                        # 629 passed / 7 skipped
# 真实模型：本机 web_search 不可达，天然覆盖失败重试路径
MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest tests/e2e/test_live_e2e.py -q -s
# → 7 passed，586.22s（重试前 343.69s）；81 次 tool.retry；出现 attempt=4 max_attempts=4 的终态失败
# tool_calls 表按 (tool_name,status) 分组 rows == ids（27 failed / 61 succeeded），一逻辑调用一行未被破坏
```

同轮暴露的两个新问题（未处置，见 ADR-009 修订 2）：

1. **确定性错误被无谓重试**——`calculator` 的非法参数、`sql_query` 的无效 SQL 重试 3 次
   不会成功，只增加延迟（本轮 live 多花约 4 分钟）；建议按错误类型分类重试（仅瞬时错误）。
2. **F-08 容器内沙箱不可用**——backend 容器未挂载 `/var/run/docker.sock`，
   `code_execution` 在 compose 下必然报 `沙箱不可用: Docker 守护进程不可用`；
   宿主直跑可用，容器部署下该工具形同不可用，修法需部署决策。

### 4.8 按错误类型分类重试（2026-09-20，ADR-009 修订 3）

「失败就重试 3 次」会把模型自己造成的确定性失败也重试一遍，因此补上分类：
异常携带 `retryable`（默认 `True`），确定性失败标 `False`，编排层只在可重试时重试。

| 类别 | 例子 | 行为 |
| --- | --- | --- |
| 瞬时（`retryable=True`） | `web_search` 超时/不可达、沙箱或数据库暂时不可用、MCP 传输错误、未知异常 | 按上限重试（首次 + 3 次） |
| 确定性（`retryable=False`） | 参数不合法、计算器表达式非法、只读策略拒绝、SQL 语句写错（`ProgrammingError` 等）、沙箱策略拒绝、工具未注册 | **只尝试一次**，落 `tool.retry_skipped reason=non_retryable` |

```bash
uv run pytest tests/unit/test_pipeline_tools.py tests/unit/test_builtin_tools.py -q   # 151 passed
uv run pytest -q                                                                     # 636 passed / 7 skipped
# 真实模型：日志统计 36 × tool.retry（瞬时）+ 13 × tool.retry_skipped（非法 SQL / 策略拒绝）
MACP_E2E_LIVE=1 MACP_E2E_TIMEOUT=900 uv run pytest tests/e2e/test_live_e2e.py -q -s
# → 7 passed / 591.47s（修掉分页假设后的完整复跑）
```

**同轮修掉的用例假设问题**：`test_live_tool_calls_are_readable_from_postgresql` 原断言
`total == len(items)`，隐含「一次运行的调用数不超过默认单页 20 条」；本轮真实运行出现
34–39 次调用后误判失败。改为按分页语义断言（`page_size=100`；`total <= page_size` 时相等，
否则本页装满），其余「每行属于本次 Workflow 且状态为终态」不变。

**残留的时间成本在可重试侧**：本机无公网出口，`web_search` 每次尝试约 10s，重试 3 次
≈ 40s/次，是 live 套件 9–10 分钟的主要来源；压缩它需要把 `TOOL_SEARCH_ENDPOINT` 指向
可达服务或在无网环境不暴露该工具（配置项，未在本次范围）。

3. 「任务记录」页只显示当前会话最近一次执行（`App.tsx::History`，页面已标注
   「完整历史查询尚未接入」）。做完整历史需要新增 `GET /api/v1/workflows` 一类的列表
   接口，属新增能力，本轮未做；因此**不据此宣称** `分工.md` §7 的「Web UI 可展示任务历史」
   已闭环。
   - **后续状态（2026-09-16 起，此处更正）**：**会话级**历史已接入——`GET /api/v1/sessions`
     （`doc/api.md` §5.13）+ 记录页「历史会话」副路由 + 侧栏「历史会话」下拉，可浏览并恢复任一
     历史任务。上面这段说的是**workflow 级跨会话列表**，它到 D9-10 收尾时**仍然没有**，
     所以「当前会话最近一次执行」的运行记录区口径未变。`doc/deployment.md` §E-04 的核对清单
     已按这个区分改写。

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

### 4.5 D9-10 收尾：真实附件样本、视觉评测集、Agent 读会话文件、附件回读列、编排净收益（2026-09-17，成员 D）

§4.4 末尾登记的四条「仍未做」在本轮逐条处理，另加一条顺手修掉的既有低效。
新增决策 [ADR-025](decisions/025-session-file-tools.md)（会话级文件工具）。

**1. 后端（隔离存储，跑法与 §1 一致）**

```bash
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/macp_test" \
REDIS_URL="redis://localhost:6380/15" ./.venv/Scripts/python.exe -m pytest -q
# 724 passed, 7 skipped, 7 warnings in 10.50s（上一轮 682 → +42）
```

新增 42 例：

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `tests/unit/test_attachment_fixtures.py` | 13 | 真实 fpdf2 / python-docx / openpyxl 产出的 6 份夹具 |
| `tests/unit/test_session_files_tools.py` | 19 | 会话文件工具、注册表装饰、接线到阶段执行 |
| `tests/unit/test_attachment_readback_columns.py` | 4 | 回读语句的列契约（不选 `data` / `text_content`） |
| `tests/unit/test_attachments.py` | +6 | PDF `/ToUnicode`：十六进制 CID、字面量 CID、无 CMap、码冲突、bfrange、二进制流 |

`7 skipped` 与既有 6 条 warning 不变（需真实模型 / Starlette 弃用告警）。本轮多出的
第 7 条 warning 是**本机环境噪声**：`.pytest_cache` 目录不可写触发的
`PytestCacheWarning: Permission denied`，与代码无关，加 `-p no:cacheprovider` 即回到 6 条。

**2. 真实二进制附件夹具（上一轮「PDF 仍只有构造样本」）**

`tests/unit/test_attachments.py` 里的样本是**手工拼的字节串**——它能钉住「某段正则能处理
某种形状」，钉不住「真实工具产出的文件长什么样」。本轮补 `scripts/make_attachment_fixtures.py`
生成、并提交 6 份**真实库产出**的夹具到 `tests/fixtures/attachments/`：

| 夹具 | 生成方式 | 期望 |
| --- | --- | --- |
| `quarterly-report.pdf` | fpdf2，核心字体 + FlateDecode + 3 页 | 读出全文（含第三页附录） |
| `embedded-subset-font.pdf` | fpdf2 + 内嵌 DejaVuSans TTF 子集，CID 写字面量串 | 靠 `/ToUnicode` 读出全文 |
| `two-font-heading.pdf` | fpdf2 + Bold/Regular 两套内嵌字体 | **明确失败**，不产出乱码 |
| `scanned-invoice.pdf` | PIL 画整页位图后贴进 PDF | 明确失败（没有文本层） |
| `meeting-notes.docx` | python-docx（含表格） | 段落与表格单元格都出来 |
| `budget-plan.xlsx` | openpyxl（含**无缓存结果的公式**） | 表头声明「公式未求值」 |

夹具生成**不进测试依赖**（测试环境没有 fpdf2 / python-docx / openpyxl，也不该为了读夹具去装）：

```bash
uv run --no-project \
  --with fpdf2 --with pillow --with python-docx --with openpyxl --with matplotlib \
  python scripts/make_attachment_fixtures.py
```

用 matplotlib 只是为了取一份**开源许可**的 TTF（DejaVuSans）——内嵌商业字体（SimHei 等）
会把字体文件带进提交物，许可上不合适。`test_fixture_directory_is_fully_covered` 钉住
「夹具目录里不能有没人测的文件」。

**这一轮真正补上的能力是 PDF 的 `/ToUnicode` 解码**：真实 PDF 的正文常写成**字形码**
（CID）而不是字符，单靠 `latin-1` 解出来是控制字符，解析器会整份放弃。`extract_pdf` 现在
会合并文档自带的 `/ToUnicode` CMap（`beginbfchar` / `beginbfrange`），把字面量串
`(\000\001…)` 与十六进制串 `<0001…>` 两种写法都解回文字。

**冲突码安全拒绝**是刻意保留的边界：子集字体的码各自从 0 起编，两套字体合并后同一个码
会指向不同的字；按「能解出来就用」会产出**通顺但完全错误**的句子——比读不出来更糟，
因为模型会当真。所以冲突时返回 `failed`，错误信息点明「字符码冲突、按此解码会得到乱码」，
由 `two-font-heading.pdf` 钉住。

**3. 视觉能力评测集（上一轮「视觉能力没有评测集」）**

`scripts/vision_eval.py`：10 个**答案唯一、机器可判**的用例，覆盖计数（含颜色/形状双重筛选）、
颜色识别、位置（最左/最高）、七段数码管 OCR、网格行列定位、以及「图中没有三角形」这类
**否定回答**。图片用 Pillow 按固定坐标画（数字自绘七段管，不依赖字体），走**真实链路**：
`prepare_upload` → `AttachmentPayload` → `build_human_content` → 真实模型，不绕过附件层。

```bash
MACP_VISION_EVAL=1 uv run --with pillow python scripts/vision_eval.py \
  --out doc/evals/vision.md --keep-images .workbuddy/tmp/vision-images --gap 6
# 10/10 通过
```

报告：[doc/evals/vision.md](evals/vision.md)。**它评的是「看不看得对」，不评回答质量**；
期望串是「归一化后命中」，适合当趋势指标与回归闸门，不是精确准确率。
**评测图片不入库**：脚本按固定坐标逐像素生成，换台机器画出的是同一张图（`--keep-images`
只是给人工看一眼），所以提交一目录二进制没有意义，报告里写清用例形态就够了。

**4. Agent 按需读回会话附件（上一轮「Agent 仍无法自己读文件」）**

按 ADR-025 新增两个**会话级只读**工具 `list_session_files` / `read_session_file`，
数据来自 `attachments` 表：不放宽沙箱策略（`open` / `import` 依旧禁止）、不给沙箱挂任何卷、
不进 `GET /api/v1/tools` 静态目录。19 例覆盖：清单与读取、截断（`session_file_max_chars`）、
图片不可读的提示、解析失败时带出原因、**子串歧义拒绝**（`invoice.pdf` 与 `invoice-2026.pdf`
并存时不猜）、注册表按会话追加、`session_scoped_registry` 在无会话/无条目时空转。
`tests/e2e/test_pipeline_e2e.py` 的真实工具断言改为断言它们**在执行时确实被绑定给模型**
（`bound == {四个内置} ∪ {两个会话级}`）——只在单元测试里存在不算数。

**5. 附件列表回读不再读 `data` 列（上一轮「既有低效，未修」）**

ADR-024 时期 `list_attachments_for_messages` 用 `select(AttachmentRecord)` 取实体，
`data` / `text_content` 被读出来又丢掉。现在改为显式列元信息
（`_attachment_meta_columns()`），`has_original` 由**库侧** `data IS NOT NULL` 算出。
返回结构与调用方口径不变。回归靠编译语句断言选中列里没有 `data` / `text_content`
（`tests/unit/test_attachment_readback_columns.py`）——**不用 SQL 子串断言**：
`has_original` 的表达式本身含 `attachments.data`，子串断言会误报。

**6. 静态 vs 动态编排的净收益（上一轮「动态编排的净收益仍无度量」）**

`scripts/orchestration_ab.py`：4 个任务 × 2 种模式，进程内直跑两条链路，对比
墙钟 / 模型纯耗时 / 节流等待 / 步骤数 / 模型调用数 / 输入输出 token / 交付物字符 /
「必备内容是否命中」。报告：[doc/evals/orchestration-ab.md](evals/orchestration-ab.md)。

```bash
MACP_AB_LIVE=1 uv run python scripts/orchestration_ab.py \
  --out doc/evals/orchestration-ab.md --gap 6 --min-interval 6 --retry 2
```

**这一轮量出一个环境事实，必须先记下来**：本机网关（`localhost:3000`，one-api 系）在
**紧接着**上一次请求结束就发下一次时，必然在 ~1.4s 内返回 500 `do_request_failed`。
判定实验（同进程、同一模型）：

| 调用 | 与上一次的间隔 | 结果 |
| --- | --- | --- |
| A | 首次 | 成功（2.84s） |
| B | 立刻（同客户端） | **失败**（1.33s） |
| C | 距 B 6s（同客户端） | 成功（2.48s） |
| D | 立刻（**新建客户端**） | **失败**（1.52s） |
| E | 距 D 6s | 成功（2.95s） |

D 用的是新客户端却同样失败 → **不是连接复用**，是网关侧节流。第一次跑批因此
**8 个组合全灭**，且失败形态极具误导性：静态链路第 1 步（collect）成功、第 2 步
（analyze）必失败；动态链路规划成功、第一步必失败——看起来像"某一步有 bug"，
实际是"第 2 次调用撞上节流"。所以脚本里加了 `GatewayThrottle`：相邻调用强制间隔
（`--min-interval`，默认 6s）+ 只对**报错**重试，并把**等待时间与模型纯耗时分开记**。
不加它，量到的是网关限流而不是编排差异。视觉评测（第 3 条）也是靠同样的间隔与退避
才跑出 10/10。加上之后第二次跑批 **8/8 组合全部成功、0 次重试需要触发**。

**跑出来的数（单轮采样、4 个任务，`--min-interval 6`）**：

| 任务 | static token | dynamic token | 差值 | 步骤（static → dynamic） | 交付物字符（static → dynamic） |
| --- | --- | --- | --- | --- | --- |
| `single_question` | 6080 | 1687 | **-72.3%** | 3 → **1** | 1596 → 81 |
| `collect_then_summarize` | 5523 | 2696 | **-51.2%** | 3 → **1** | 1805 → 403 |
| `two_independent_sources` | 16413 | 43208 | **+163.3%** | 3 → 4 | 2610 → **736** |
| `analysis_only` | 25432 | 35723 | +40.5% | 3 → 3 | 3697 → 6431 |
| **合计** | 13362/次 | 20828/次 | **+55.9%** | 3.0 → 2.25 | — |

「必备内容命中」两边都是 **4/4**。所以结论是：**在这个小样本上，动态编排的净收益是负的**
（+55.9% token、+0.2 次调用，质量判据持平）。分开看两条更值得记的事实：

1. **动态的「省」来自它不做三步**：前两个任务上规划节点把计划收敛成 **1 步**，等于承认
   「这任务不值得走收集→分析→报告」。这确实便宜，但**不是动态编排的功劳**——
   给静态链路加一条「简单任务跳过 collect」的规则成本更低（连规划调用都省了）。
2. **动态的「贵」发生在它真正拆开的时候，而且没换来产出**：`two_independent_sources`
   是唯一为「两路独立收集」准备的任务，动态确实拆成 4 步，但代价是 **2.6 倍 token**
   （43208 vs 16413），交付物却**从 2610 字缩到 736 字**。更贵、更多步、产出更短——
   这是本轮最直接的反面证据。

因此**不建议在本轮之后打开 `AGENT_ORCHESTRATION_MODE=dynamic`**（默认仍是 `static`）；
档 3（并行 / 聚合 / 反思）的前置条件**仍未满足**——现在有数了，而数说「先别加」。
下一步该做的是把评测集做大（更多任务 + 参考输出 + 多轮采样），而不是先加并行。

报告模板里还有一张**按任务看差值**的自动表，就是为了防止「合计平均值」把这种结构差异
抹平（合计那一栏读起来像"动态贵 56%"，实际是两个方向的巨大差异相加）。原始行落在
`doc/evals/orchestration-ab.rows.json`，改报告措辞用 `--from-rows` **离线重渲染**，
不必再花一次 20 分钟的模型开销。

**7. 仍未做 / 仍未验（不隐瞒）**

1. **`orchestration_mode=dynamic` 的部署环境端到端回归**：`tests/e2e/test_live_e2e.py` 走
   HTTP 打的是部署好的栈，至今只覆盖静态链路；动态链路的真实模型证据是脚本级的
   （第 6 条）而不是走 compose 的。
2. **规划质量仍只能人工读**：`plan_source` 区分 `llm` / `fallback`，但没有「降级率」指标，
   也没有「动态的计划比固定三步更贴任务」的机器判据。
3. **扫描件 OCR**：`scanned-invoice.pdf` 这类没有文本层的 PDF 只做到「明确失败 + 保留原件」，
   不做 OCR。这是范围选择，不是缺陷。
   → **已由 §4.7 / [ADR-027](decisions/027-scanned-pdf-page-images.md) 推进**：仍然不做 OCR，
   但页面会渲成图片走视觉通路，模型不再看到零内容。
4. **多字体 PDF**：两种以上内嵌字体且码冲突时拒绝解析（第 2 条）。单字体覆盖了主流导出，
   但「同一份文档里正文与页眉用不同字体」的场景目前读不了。
   → **已由 §4.7 / [ADR-027](decisions/027-scanned-pdf-page-images.md) 兜住**：文本通路仍然拒绝
   （正确性优先），但这类 PDF 会走页面图像，实际可读。
5. **视觉评测只测 10 例、单轮采样**：够当闸门，不够当准确率；`--repeat` 与更大用例集是
   后续的事。

### 4.6 MCP 注册表接入编排层（2026-09-19，成员 D）

新增决策 [ADR-026](decisions/026-registry-servers-in-orchestration.md)。§4.5 结束时留下的
唯一硬缺口是：**配置页登记并「发现」的 MCP Server 对 Agent 完全无效**
（`mcp_server_registry` 的引用者只有 API 层 CRUD）。本轮把它接上了。

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `app/mcp/registry.py` | 新增 `RegistryServersToolRegistry`（合成层）、`registry_server_entries()`、`refresh_registry_server_entries()`、`_entry_tools()`；`build_tool_registry()` 在基础后端上叠合成层 |
| `app/core/mcp_registry.py` | 新增 `_sync_orchestration_tools()`，在增删改 / 发现 / 列配置之后刷新快照 |
| `app/mcp/__init__.py` | 转出新增的公开名 |
| `tests/unit/conftest.py` | 新增 `memory_mcp_registry`（autouse），理由见下 |

**2. 验证**

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/unit/test_registry_servers.py
# 13 passed in 0.63s

./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/unit/test_mcp_tools.py tests/unit/test_pipeline_tools.py \
  tests/unit/test_session_files_tools.py tests/unit/test_dynamic_pipeline.py
# 95 passed in 1.67s
```

13 例覆盖：与内置工具合成、目录只取**已发现的缓存**（用 `transport="ws"` 证明没去连接）、
`disabled` 摘除且给出原因、重名时内置优先、调用路由到真 Server（走真实 MCP 协议往返）、
未知名字回退基础注册表、传输不支持不抛、读表失败**只尝试一次**、刷新后目录跟随、
`close` 释放 Server 与基础后端。

**3. 本轮实测踩到的坑（已写进 ADR 的「决策 3」）**

第一版让 `list_tools()` **每次现读** `mcp_server_registry`。这在存储正常时看不出问题，
存储不可达时是灾难性的：`docker ps` 显示 Docker Desktop 已停，此时到 `5433` 的连接
失效形态是 **`TimeoutError`（包被丢弃）而不是 `ConnectionRefused`**，于是**每次**工具枚举
都要卡满一个 TCP 超时——4 个文件共 95 例从「几十秒」变成 **15 分钟都跑不完**。

「读不到配置」的正确含义是「没有额外工具」，不是「整条流水线停摆」。所以改成
**进程内快照 + 配置面主动刷新**：只加载一次、失败不重试，热路径纯内存。

**4. 单测必须隔离这条读取**

`tests/unit/conftest.py` 新增 autouse 的 `memory_mcp_registry`，把快照固定为空。
它与既有的 `memory_redis`、`MemoryMetricSink` 是同一条约定——**单元测试不连真实
PostgreSQL**（§1）。把它当作"测试环境特殊处理"就错了：它同时也是**生产语义**的一部分，
即工具枚举路径上不能有 IO。

**5. 本轮验证边界（如实记录）**

- **全量回归未跑**：本机 Docker Desktop 已停（PostgreSQL 5433 / Redis 6380 / 网关 3000
  全部不可达），而全量用例依赖真实 PG。上面两组数字是**不依赖存储**的那部分，
  不是本轮的完整证据。启动 Docker 后需要补跑：
  `DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/macp_test" REDIS_URL="redis://localhost:6380/15" ./.venv/Scripts/python.exe -m pytest -q`
- **`tests/unit/test_mcp_registry.py`（Server CRUD）未验证**：它是本轮改到
  `app/core/mcp_registry.py` 的直接对象，且需要真实 PG。改动本身只新增了一个
  「记日志 + 通知编排层」的调用，不改返回值，但**这是未验证的部分**。
- **动态模式的 compose 端到端回归仍未做**（承接 §4.5 的缺口）。

**6. 顺手修掉的文档缺陷**

`doc/testing.md` 第 201 行原写「见 §3.4 的漂移清单」，但**全文没有这一节**——
§3.4 是「UI 评审预览」。悬空引用已删除，该行同时更新为准确表述：
`disabled` 现在真的会摘掉工具（ADR-026），`allowAutoExecution` 仍然不消费（HITL 未做）。

### 4.7 扫描版 PDF 走页面图像，不引 OCR 引擎（2026-09-20，成员 D）

新增决策 [ADR-027](decisions/027-scanned-pdf-page-images.md)。§4.5 留下的边界是
「扫描件只**明确失败** + 留原件」——原件留住了，但模型看到的是**零内容**。本轮把这个洞补上：
抽不出正文的 PDF 把页面渲成 PNG，当普通图片附件走**已经付过成本的**视觉通路
（`doc/evals/vision.md` 的 10 例实测 10/10）。仍然**不做 OCR**：识别错一个字产出的是一句
通顺但错误的话，而页面图里模型自己会做视觉判断。

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `app/attachments/render.py` | **新增**：`render_pdf_pages()` / `encode_png()` / `RenderedPages` + 五个上限常量 |
| `app/attachments/extract.py` | `extract_pdf()` 抽不到正文时先**探测渲染第 1 页**；新增 `_pdf_render_note()`；`_pdf_failure_reason()` 带上「转图也失败」的原因 |
| `app/attachments/prepare.py` | 新增 `_expand_scanned_pdf()`，`load_payloads()` 把这类 PDF 展开成逐页图片载荷（**执行阶段**才渲） |
| `app/attachments/prompt.py` | 附件数按 id 去重计；「没有可用正文」与「超出长度上限」拆成两句不同的说明；清单带上 `ready` 项的降级说明 |
| `pyproject.toml` / `uv.lock` | `pypdfium2==5.13.0`（BSD-3 / Apache-2.0，wheel 自带 pdfium） |
| `frontend/src/workspace/attachments.ts` | `describeAttachment` 显示 `ready` 项自带的降级说明（原来被静默丢掉） |

**2. 验证**

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/unit/test_pdf_render.py
# 13 passed in 1.02s

./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/unit/test_mcp_tools.py tests/unit/test_pipeline_tools.py \
  tests/unit/test_session_files_tools.py tests/unit/test_dynamic_pipeline.py \
  tests/unit/test_attachments.py tests/unit/test_attachment_fixtures.py \
  tests/unit/test_pdf_render.py
# 173 passed in 3.13s

./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/integration/test_attachments_api.py
# 10 passed in 2.09s

cd frontend && npm run build                      # tsc --noEmit + vite build 通过
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx ... && node "$TEMP/workspace-smoke.cjs"
# 80/80 checks passed（+2 条本轮新增的文案断言）
```

`test_pdf_render.py` 的 13 例刻意**自带一个最小 PNG 解析器**（逐块校验 CRC、核对解压行长、
检查每行 filter 字节）：编码器是自己手写的，用另一个库来"相信"它等于没测。
另外两处实测数字值得记一笔：一页 A4 渲染 **0.02–0.3 秒**、PNG **17–52 KB**；
两份原本读不出正文的夹具（`scanned-invoice.pdf`、`two-font-heading.pdf`）渲出来的图
**肉眼完全可读**（发票金额与标题文字都清楚），这正是"渲染不是 OCR 的降级品"的证据。

**3. 边界（如实记录）**

- **超过 5 页只带前 5 页**：硬上限，靠载荷名字里的「第k页/共N页」与 `error` 里的说明如实报告，
  也不做长图拼接——视觉模型会把长边缩到 1.5k 像素，拼起来每页只剩约 300px 高，文字全糊。
- **不做空白页检测**：可靠的空白判断要逐像素统计（没有 numpy 时是慢路径），误判代价是
  **丢掉真实内容**；宁可让模型自己说"这一页是空白的"。
- **`read_session_file` 工具读不到扫描件正文**：工具结果只能是文本，无法回传图片；
  它会看到 `status=ready` + `text=null` + 那句降级说明。
- **镜像必须重建**：`uv sync --frozen` 从 `uv.lock` 装 pypdfium2，compose 里的 backend
  不重建就还是老镜像；组件缺失时降级为 `failed` 并**点明 `pypdfium2`**，不是含混的"读不出来"。

**4. 本轮验证边界（承接 §4.6）**

本机 Docker Desktop **仍未启动**（PostgreSQL 5433 / Redis 6380 / 网关 3000 不可达），
所以上面四组数字依旧是**不依赖存储**的那部分。全量回归（基线 724 passed / 7 skipped）
与依赖真实 PG 的用例需要在 Docker 起来后补跑，命令见 §4.6。

**5. 顺带修正的旧断言**

两份夹具的期望从「明确失败」改成「`ready` + 页面图像」，这是 ADR-027 改的就是这条边界；
但**核心不变量一条没松**：任何情况下都不许产出"像正文的垃圾"。
`_assert_no_readable_text()` 把它固化成一个共用断言——`text` 必须是 `None` 或有真内容，
且 `error` 必须交代清楚（能渲染就写"改走页面图像"，不能就报 `failed` + 原因）。

### 4.8 执行台弹窗改为「执行轨迹」，时间补日期，确认行右对齐（2026-09-21，成员 D）

§4.7 之后用户提的三处界面问题，一处是功能缺口、两处是版式：

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `app/api/stage_trace.py` | **新增**：只读读取 Dapr State Store 的阶段状态，还原 `content` / `previous` / `tool_calls` |
| `app/api/main.py` | 新增 `GET /api/v1/workflows/{id}/stages`（§5.17）与三个响应模型 |
| `frontend/src/workspace/AgentStageModal.tsx` | 重写为**执行轨迹**弹窗（`AgentTraceView` 纯视图）；删掉「角色与模型绑定」整块与随之为死的字段 / 导出 |
| `frontend/src/config/shared.tsx` | 新增 `formatStamp(value, now = new Date())`：今天 / 昨天 / `M月D日 HH:mm` / 跨年补年份 |
| `frontend/src/App.tsx` | 删掉只给 `HH:mm` 的本地时间实现，改走 `formatStamp` |
| `frontend/src/workspace/workspace.css`、`config/config.css` | `.cfg-modal-foot` / `.cfg-actions` 加 `justify-content: flex-end` |

弹窗回答的是「**它收到了什么、调了什么、产出了什么**」：分配到的任务（上游正文）、按序的工具调用
（含失败原因与截断标记）、阶段产出。**不放模型与参数**——角色绑定与调参属于「Agent 团队」页。
模型内部的隐藏推理没有落盘，弹窗如实说明，不伪造「思维链」。

**2. 验证**

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/unit/test_stage_trace.py \
  tests/integration/test_stage_trace_api.py
# 13 passed（9 + 4）

./.venv/Scripts/python.exe -m pytest -q          # 全量（隔离存储 macp_test + redis/15）
# 768 passed / 7 skipped in 14.41s

cd frontend && npm run build                      # tsc --noEmit + vite build 通过（CSS 80.68 kB）
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx ... && node "$TEMP/workspace-smoke.cjs"
# 90/90 checks passed（+10）
```

**线上端到端**（`docker compose up -d --build backend frontend` 重建后，17m36s，两镜像 Built、
容器 healthy）——要证的是 §5.17 在**真部署**（PostgreSQL + Dapr sidecar + 真实模型网关）上
读得出轨迹，不是拿单测替身糊的：

- 任务「用计算器算出 128/2680 的结果（保留四位小数）」→ workflow
  `961af452-c680-49d9-8cf5-38c5d9fad431`，终态 `completed`，
  `completed_steps=["collect","analyze","report"]`。
- `GET /api/v1/workflows/{id}/stages` → `mode=static`、`availability=available`，三阶段齐全：
  `collect` 是根阶段（`input=null`）且有 **1 次 `calculator` 调用**
  （`{"expression":"128/2680"}` → `{"value":0.04776119402985075}`，`succeeded`）；
  `analyze` 的 `input` 正是 `collect` 的产出切片（**上游接线生效**）；三者 `truncated: false`。
- 镜像内产物核对：`dist/assets/*.css` 含 `ws-trace-` 与 5 处 `justify-content: flex-end`
  ——新样式确实烤进了 nginx 镜像，不是只躺在宿主机源码里。

**3. 边界（如实记录）**

- **动态链路没有逐步骤轨迹**（ADR-019 不落盘）：接口回 `availability=not_integrated` + 原因，
  界面原样显示这句，不改写成「暂无数据」。
- **「没有轨迹」有四句不同的话**：尚未开始 / 正在执行（轨迹在阶段完成后才落盘）/ 已完成但状态
  已被清理 / 载荷无法解析。混成一句会让用户去查错的地方。
- 预览页路由表有**两处**（`rendercheck/preview_seed.py::ROUTES` 与 `build-preview.py`），
  漏一处就提示「预览未收录」——本轮又踩了一次，已补。

### 4.9 侧栏改为协作画布，会话工作流可编号回看（2026-09-21，成员 D）

用户的反馈是「右侧栏的 Agent 卡片应该是记 Agent 的参数、Token 这些，和 Agent 执行台不一样」。
根子上是**三块视图的分工没落到数据上**：侧栏此前展示的是执行进度，和执行台弹窗说的是同一件事。
本轮把侧栏改成「这个 Agent 是用什么跑的」，并给它一个能回看历史对话的全屏画布。
决策见 [ADR-028](decisions/028-sidebar-collaboration-canvas.md)。

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `app/core/checkpoint.py` | 新增 `list_workflows_for_session()`，**按 `created_at` 升序**返回（编号由位置决定，顺序是契约） |
| `app/api/store.py` | `SqlApiStore` / `InMemoryApiStore` 实现 `list_workflows()`；内存实现同样排序 |
| `app/api/main.py` | 新增 `GET /api/v1/sessions/{id}/workflows`（§5.18）与 `WorkflowListResponse` |
| `frontend/src/workspace/collaboration.ts` | **新增**：纯模型（波次、参数、用量归集、工具链路、节点/连线/图），不依赖 React |
| `frontend/src/workspace/CollaborationGraph.tsx` | 重写为 **Agent 卡片 + 连线**；移除「点击开弹窗」 |
| `frontend/src/workspace/CollabCanvas.tsx` | **新增**：全屏画布，顶部 `对话 1 / 2 / 3` 编号切换，Esc 关闭 |
| `frontend/src/workspace/TaskUsage.tsx` | 抽出 `useWorkflowMetrics()`（卡片与用量面板共用一次请求）与 `usageFor()`；新增 `scopeOf()` 的 `role` 兜底 |
| `frontend/src/workspace/workspace.css` | `.collab-card*` / `.collab-hover`（CSS 驱动悬停）/ `.collab-overlay`（`fixed`，z-index 40） |
| `frontend/src/api/client.ts`、`frontend/src/App.tsx` | 新接口封装；`Inspector` 接管画布状态与「别的对话单独取一份轨迹与用量」 |
| `rendercheck/{preview_seed.py,build-preview.py,workspace-smoke.tsx}` | 两处路由登记；种子按**真实标签口径**（`role`/`stage`，无 `agent_id`）给；冒烟 +31 条 |

**2. 修掉的真问题：Token 没有归到 Agent 头上**

采样标签里**没有 `agent_id`**——编排层两条链路都只传 `role`
（`app/orchestration/pipeline_graph.py`、`dynamic_graph.py` 调 `observed_stage(...)` 时只给 `role`），
落地标签是 `workflow_id` / `stage` / `role`（Token 另有 `model`）。`scopeOf()` 原来只认 `agent_id`，
于三个 Agent 的 Token 全部退到 `model` 一层、合并成一个 `gpt-5.5` 分组，界面上看就是
「Token 没按 Agent 记」。归集顺序改为 `agent_id` → `role` → `model` → 任务级。
`doc/api.md` §5.5 原文写的「可选 agent_id/model」也是**文档漂移**，已一并改正。

**3. 验证**

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/integration/test_session_workflows_api.py tests/integration/test_stage_trace_api.py \
  tests/unit/test_stage_trace.py
# 18 passed（5 + 4 + 9）

cd frontend && npm run build                      # tsc --noEmit + vite build 通过（CSS 86.76 kB / JS 417.37 kB）
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx ... && node "$TEMP/workspace-smoke.cjs"
# 121/121 checks passed（+31）
node_modules/.bin/esbuild rendercheck/config-smoke.tsx ... && node "$TEMP/config-smoke.cjs"
# 69/69 checks passed
```

`rendercheck/*.tsx` 单独过一遍 tsc：只剩既有的 `@types/node` 缺失噪音（`tsconfig.json` 的
`include` 只有 `src`，覆盖不到本目录）。预览页重建：**42 条路由**，种子里能看到两个对话编号
（`wf-preview-earlier` 是补出来的更早一次已完结对话），切到「对话 1」时它的
`GET /workflows/{id}` / `/stages` / `/tool-calls` 三条也都有应答——不然预览里的历史对话会是空白。

**线上验证**（`docker compose up -d --build backend frontend` 重建后，两镜像 Built、
容器 healthy；入口是 **5173** 的 nginx 反代 `/api`）：

- `GET /api/v1/sessions/{id}/workflows` 对一条有 **3 条**工作流的会话回
  `total=3`，顺序与创建时间一致（16:17:45 → 16:23:36 → 16:23:39），即「对话 1 / 2 / 3」
  在真实数据上成立，不只是单测里造出来的；三条的 `session_id` 全部等于被查询的会话。
  未知会话回 `404 SESSION_NOT_FOUND`（不是空列表——「草稿态」与「不存在」是两件事）。
- 前端容器里核到了新产物：`index-tFSw4r53.css` 含 `.collab-card` / `.collab-overlay` /
  `.collab-hover` / `.collab-link-tools`，`index-DIwFFPx_.js` 含「展开全屏画布」「工具链路」
  「collab-canvas」——**部署里的界面确实换了，不是只在源码里**。
- **采样标签的实测口径**（直接查库，不是推断）：
  `select metric_name, labels from metrics where labels->>'workflow_id' = '961af452-…'` 回的标签是
  `{"role": "collector", "model": "gpt-5.5", "stage": "collect", "workflow_id": "…"}`；
  另查 `where labels ? 'agent_id'` → **全库 0 条**。这就是 §5.5 那处漂移的实证：
  `agent_id` 从来不存在，Token 只能靠 `role` 归到 Agent 头上。

**4. 边界（如实记录）**

- **卡片不再是执行轨迹的入口**：点了不跳 §5.17 弹窗。执行台卡片已经承担那个入口，两块视图都能
  点进同一份轨迹就又会混成一个（ADR-018）。
- **悬停详情用 CSS `:hover` / `:focus-within`，不用 JS 状态**：DOM 常驻 → 键盘可达，
  离屏冒烟也能断言内容（JS 状态驱动的浮层在静态渲染里永远是空的）。
- **工具链路的计数一律写出来，包括 `×1`**：只在多于一次时写计数会得到
  `web_search ×3、sql_query` 这种半截话，读者无从判断后者调了几次。
- **并行是「上游那一波」决定的**：连线 `kind` 看上游波次的节点数，不看两端是否同波——
  依赖永远指向更低的波次，同波判断不可达。
- **动态链路只有进度没有逐步骤轨迹**：画布能显示波次与状态，轨迹仍旧缺（见 §4.8 的边界）。
- `app/core/checkpoint.py` 的 `list_workflows_for_session()` 属**只读新增**（B 的文件），
  已登记 `分工.md` §4。
- **本节的画布是「卡片 + 连线」，下一轮被重写为节点图**（用户判定「当前根本就不是画布」），
  见 §4.10；本节其余（接口、编号回看、Token 归因）仍是当前状态。

### 4.10 协作画布重写为节点图：卡片排布不是画布（2026-09-21，成员 D）

用户的反馈很直接：「画布效果非常不好，当前根本就不是画布」，并附参考项目
（`Jasper-zh/Multi-Agent-Playground`）的截图，指出可视化源码可参考。根子上是 §4.9 把画布做成了
**竖向堆叠的卡片 + 横向连线**——那是一张表，不是一张图：没有节点、没有弧线、没有「谁指向谁」。
本轮照参考项目的画法重写：**圆节点 + 弧线连线**。决策补进 [ADR-028](decisions/028-sidebar-collaboration-canvas.md)
（同日修订 + 决策 7 的「画布四条规则」）。

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `frontend/src/workspace/GraphCanvas.tsx` | **新增**画布本体：`layoutCollaboration()` 纯函数算坐标，`CollaborationCanvas` 渲染 SVG 弧线 + 绝对定位圆节点，`useElementSize` 量像素尺寸 |
| `frontend/src/workspace/CollaborationGraph.tsx` | 由「卡片 + 连线」**收成薄封装**：只把 `graph` 交给紧凑档画布，空态文案保留 |
| `frontend/src/workspace/CollabCanvas.tsx` | 复用同一画布宽档；`cv-frame` 量出的可用高度喂给 `minHeight` |
| `frontend/src/workspace/workspace.css` | `.collab-card*` / `.collab-link*` / `.collab-legend` / `.collab-foot` **删除**，换成 `.cv-*`（节点、弧线、并行圈定框、工具胶囊、悬停详情） |
| `frontend/src/App.tsx` | 删掉模块标题下的功能说明段（用户要求「模块标题区域不需要那么多文本介绍功能」） |
| `frontend/rendercheck/workspace-smoke.tsx` | 卡片断言换成**几何断言**（节点数 / 连线数 / 路径形状 / 锚点不在中点 / 高度），净 +20 条 |

**2. 画布的硬规则（写进 ADR-028 决策 7）**

1. **`viewBox` 必须等于元素的像素尺寸**（配 `preserveAspectRatio="none"`）：否则 SVG 用户坐标
   和节点的 `left/top` 对不上，弧线整体飘走。
2. **量不到宽高就用默认值**：`useElementSize` 在首帧 / 离屏量不到时退 720（宽档）/ 248（窄档），
   否则首帧 `NaN` 会把整张图画没（冒烟正是断言这两个数）。
3. **`minHeight` 由外层给**：侧栏不给（用自然高度），全屏给 `cv-frame` 的可用高度——
   行距按可用高度铺开，屏幕高时不满压、屏幕矮时才滚动。
4. **标签要盖住弧线，工具胶囊不能放几何中点**：`.cv-name` / `.cv-meta` 带 `--cfg-surface` 底色
   （否则弧线从字上穿过去）；胶囊若放在 `t=0.5` 会被上游标签压住，要采样到
   「上游标签下沿 ↔ 下游节点上沿」的空带里。

**3. 验证**

```bash
cd frontend && npm run build      # tsc --noEmit + vite build 通过（CSS 85.95 kB / JS 420.11 kB）
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx --bundle --platform=node \
  --format=cjs --jsx=automatic --loader:.css=empty --outfile=rendercheck/.smoke.cjs \
  && node rendercheck/.smoke.cjs   # 141/141 checks passed（121 → 141）
```

**4. 边界（如实记录）**

- **侧栏紧凑档只写「生效参数 + Token 消耗」**：圆下只滑出 `模型 · Token`；
  任务 / 阶段产出 / 工具调用留给全屏档与执行台弹窗——三块视图的分工（ADR-018）没变，只是画法换了。
- **悬停详情仍走 CSS `:hover` / `:focus-within`**，不用 JS 状态：离屏冒烟还能断言到内容。
- **节点可拖**（仅全屏档），但拖动只改渲染偏移，不改会话数据、不落盘。
- 参考项目是 Vue（`GraphViewer.vue`），本仓是 React，**只借画法不借代码**：弧线控制点从
  「按 dx 水平折」改成「按垂直极点切线弯曲」；节点用绝对定位 div 而非 SVG `<circle>`
  （图标与文字更好排，且能挂 CSS 驱动的悬停面板）。
- **箭头不能吃 `strokeWidth` 单位**：默认 `markerUnits="strokeWidth"` 会让箭头随线宽放大，
  30px 节点上会盖住圆——改为 `markerUnits="userSpaceOnUse"` 并给死尺寸（7.5 窄档 / 9.5 宽档）。
- **本节没有做「到底画出来了吗」的线上取证**：当时只核了服务端产物哈希与 DOM 计数。
  计数说明不了可见性（`.inspector` 默认收起，画布在 DOM 里但尺寸为 0）。更正与取证方法见 §4.11。

### 4.11 节点图交互动刀：浮层收进全屏、贴侧面、产出优先，拖动整个删掉（2026-09-21，成员 D）

节点图画出来之后，用户又评审了一轮交互，四条意见：

> 首先应该在全屏画布下才会悬停显示具体内容；当前似乎是在正下方会遮盖住图……应该在偏右或者偏左；
> 浮窗太小了看不全内容，按照内容优先级，参数配置 token 消耗这些应该在最下面，首先应该显示
> 分配到的任务和产出；然后这个画布似乎是可被拖动移动的？按理来说这显示的是交互过程可视化，
> 排布固定即可，移动节点没什么实质性作用。

四条**全部采纳**，决策补成 [ADR-028](decisions/028-sidebar-collaboration-canvas.md) 决策 8。

**1. 改动**

| 文件 | 变化 |
| --- | --- |
| `frontend/src/workspace/GraphCanvas.tsx` | 删掉 `draggable` 与整套拖动（`offsets` / `drag` ref / 两个 window 监听 / `onPointerDown` / 拖动后重算边）；`NodeDetail` 去掉 `compact`，小节重排；浮层槽位改 `cv-pop-slot is-{left,right}`，位置由 `popSide()` / `popTop()` 算 |
| `frontend/src/workspace/CollabCanvas.tsx` | 不再传 `draggable` |
| `frontend/src/workspace/workspace.css` | `.cv-pop-slot` 改为侧向锚定（`left:100%` / `right:100%` + 16px `padding` 作悬停过渡带）、`translateY(-50%)`；`.cv-pop` 去掉 `position`/`width`/`translateX(-50%)`，面高 280 → 520；`.cv-pop-node` 264 → **380px**；`.cv-pop-edge` 自带定位；删 `.cv-canvas.dense .cv-pop` 与 `cursor: grab`；悬停抬层 12 → 30 |
| `frontend/rendercheck/workspace-smoke.tsx` | 侧栏断言改为「不挂浮层」；新增内容排序、侧向定位、不可拖断言；**并修掉一个恒真断言**（见下） |

**2. 修掉的真问题：断言读错了文件（恒真）**

新加的「画布节点不再可拖」写成 `!styles.includes("cursor: grab")`，其中 `styles` 是
`src/styles.css` —— 而画布样式在 `src/workspace/workspace.css`，那条规则从来不在 `styles` 里，
**断言恒真**。是「悬停面板的侧向定位规则已落盘」这条正断言先失败，才把它暴露出来。
冒烟里现已单开一个 `canvasStyles` 显式读 `workspace/workspace.css`。

**教训**：**否定式断言（`!x.includes(...)`）在「读的不是那个文件」时永远是绿的**，
比没有断言更糟——它给你的是虚假的安全感。写否定断言时先确认目标字符串**曾经**在那个文件里。

**3. 验证**

```bash
cd frontend && npm run build    # tsc --noEmit + vite build 通过（CSS 85.97 kB / JS 418.99 kB，比上轮少 1.1 kB）
node_modules/.bin/esbuild rendercheck/workspace-smoke.tsx --bundle --platform=node \
  --format=cjs --jsx=automatic --loader:.css=empty --outfile=rendercheck/.smoke.cjs \
  && node rendercheck/.smoke.cjs   # 146/146 checks passed（141 → 146）
```

**线上实测**（重建 frontend 镜像后，用 CDP 驱动真浏览器 —— 见下节「取证手段」）：

| 检查 | 实测 |
| --- | --- |
| 侧栏（紧凑档）浮层元素 | **0**（`cv-pop` / `cv-pop-slot` 都不渲染） |
| 侧栏画布 298×368、5 个圆节点 | 是 |
| 全屏覆盖层 / 宽档画布 | 1664×849 / 1624×638，5 个圆节点 |
| 三个 Agent 浮层面宽 | **380**，`is-right`，与圆间隔 14px（节点右沿 854 → 浮层左沿 868） |
| 浮层竖直位置（视口 849 高） | 192–665 / 218–710 / 270–790，**全部在视口内** |
| 前两个浮层是否需要滚动 | **不需要**（473 / 492 vs 面高上限 520） |
| 报告生成（产出最长） | 面高 520 触顶、内容 562 → 需滚 44px |
| 小节顺序 | 分配到的任务 → 阶段产出 → 本阶段工具调用 → 生效参数 → Token 消耗 |
| 节点 `cursor` / 交付端子 | `auto`（无 `grab`） / 不给浮层 |

**4. 取证手段（本轮最有价值的沉淀）**

`curl` 资产哈希只能证明**部署换了**，证明不了**界面画出来了**；`--dump-dom` 只能看首屏，
而画布要先点会话、再展开侧栏。用 Chrome DevTools Protocol 驱动真浏览器才拿得到几何：

```text
chrome --headless=new --remote-debugging-port=9333 --user-data-dir=<temp> --window-size=1680,1000 about:blank
→ GET http://127.0.0.1:9333/json/list 取 page 的 webSocketDebuggerUrl
→ Python: websockets.sync.client.connect(...)  （很多项目 venv 里已有 websockets，不必另装）
→ Page.enable / Runtime.enable / DOM.enable / CSS.enable
→ Page.navigate → Runtime.evaluate 点击 → DOM.querySelectorAll 拿 nodeId
→ CSS.forcePseudoState {forcedPseudoClasses:["hover"]}  ← 悬停必须用它，见下
→ Page.captureScreenshot
```

两个非显然的点：

- **`Input.dispatchMouseEvent` 在 headless 下改不出 CSS `:hover`**（面板始终 `display:none`）。
  要 `Display`/量几何就用 `CSS.forcePseudoState`——这正是它存在的用途。
- **`.inspector` 默认 `display:none`**（`position:absolute` + `.open` 才 `display:block`）。
  不先点开侧栏，画布虽然在 DOM 里、`querySelector` 也找得到，但**所有 `getBoundingClientRect()`
  都是 0**，截图里什么都没有。另外工具条上**有两个 `.toolbar-toggle`**（「隐藏 Agent 执行台」与
  「显示协作详情」），必须按 `aria-label` 定位——按类名取到的永远是第一个，点了没反应。
- 顺带更正：§4.10 那次「线上实测」只核了 DOM 计数，**没有核可见性**，当时给用户的截图里其实
  没有画布。凡是「画出来了」的结论，都必须带 `getBoundingClientRect()` 或截图。

**5. 边界（如实记录）**

- **浮层会盖住画布右侧空白区**：面宽 380px，只覆盖图上本来没东西的地方；并行波次里靠右的节点
  自动翻到左侧。
- **面高上限 520px 与 JS 里的 `POP_H` 是同一个数**：两处必须一起改，否则竖直夹取算错。
  （§4.12 已把胶囊浮窗那份改成 `--cv-pop-max` 变量，只剩 node 浮窗这一处镜像。）
- **最长的那份产出仍要滚 44px**：再放大就会在矮屏上顶出可见区，滚动是更稳的选择。
- 侧栏紧凑档因此**完全没有任何悬停信息**：这是刻意的——侧栏只回答「用什么跑的」。

### 4.12 浮层规则补全到工具链胶囊，另收两条容器层的账（2026-09-21，成员 D）

**改动**：`frontend/src/workspace/{GraphCanvas.tsx,workspace.css}`、`frontend/src/styles.css`、
`frontend/rendercheck/workspace-smoke.tsx`；`workspace-smoke` **146 → 151**。

**1. 工具链胶囊的浮层仍挂在正上方居中（二轮漏改）**

二轮只把**节点**浮层挪到了侧面，胶囊浮层还是 `bottom: calc(100% + 8px)`。CDP 实测：

```text
胶囊 l751 t427 r835 b445（cx 793）
浮窗 l643 t175 r943 b420（cx 793）   ← 与胶囊同 cx，中心相对胶囊 dy −139
压住的节点 = ["任务", "信息收集 Agent"]
```

胶囊钉在画布中线上，所以「居中 + 向上弹」**必然**盖住上游那一串节点。改为
`.cv-pop-edge.is-left` / `.is-right` 贴侧面（胶囊在右半边 `mid.x > w/2` 就往左弹）。
竖直方向不再"量面高"，改成**算可容高度**：面竖直居中于胶囊，「面高 ≤ 胶囊到上下沿距离的
两倍」就等价于不溢出，于是不必去量浮窗自己的高度。改后实测：`is-right`、在胶囊右侧、
**压住的节点 = []**、340×244、无越界。

**2. 关闭按钮的图标与文字差 2px**

```text
按钮 display:block，svg vertical-align:baseline
图标中线 28 / 文字中线 30 → 偏差 −2
```

`.cfg-quiet` 是块级按钮，lucide 的 `svg` 默认按基线坐，于是图标比文字高 2px。同一个坑
**侧栏早就修过**（`.sidebar-user-menu-head .cfg-quiet` 用 `inline-flex`），全屏画布头这份漏了。
补 `.collab-canvas-head .cfg-quiet { display:inline-flex; align-items:center; gap:5px }`，
改后图标中线 = 文字中线 = 按钮中线 = 标题图标中线 = 28，偏差 **0**。

**3. 侧栏被长会话标题顶宽（老账，非画布引入）**

隔离验证（只改一个变量）：

```text
只把「当前任务标题」灌长：侧栏 214 → 1093px       ← 复现
只把历史列表条目灌长：    侧栏 214 → 214px        ← 不背锅（菜单是 position:absolute）
侧栏 computed min-width: auto
```

`.sidebar` 是 `.app-shell` 这个 flex 行容器的 item，`min-width: auto` 会让**内容的最小宽度**
顶在 `flex-basis: 216px` 之上；当前任务标题是 `white-space: nowrap`，于是被撑到 1093px。
**子项的 `min-width: 0` 救不了这一层**——`.sidebar-user-meta` 本来就写着 `min-width:0`，
那只影响 flex 分配，不改变容器的 min-content 计算。治本点是给 `.sidebar` 自己写
`min-width: 0`。改后：侧栏稳定 **214px**，标题 `clientWidth 111 / scrollWidth 990` → 省略号生效。

**4. 自查：内联 `max-height` 静默顶掉了设计上限**

给胶囊浮窗传可容高度时直接写内联 `maxHeight`，实测 `getComputedStyle` 报 **532.777px** ——
把 `.cv-pop` 里的 `520px` 无声顶掉了。两个约束是两件事（一个是设计上限，一个是"别溢出"），
不能让晚出现的那个吃掉前一个。改为：上限声明成 `--cv-pop-max: 520px`（`.cv-pop` 上），
胶囊浮窗写 `max-height: min(var(--cv-pop-max), var(--cv-pop-fit, var(--cv-pop-max)))`，
内联只给 `--cv-pop-fit`。改后 `getComputedStyle` 报 **520px**。

**教训**

- **「同一套规矩」要回头数元素个数。** 二轮写下了"浮层贴侧面"这条规则，却只应用在节点上。
  只覆盖一半的规则比没有规则更容易漏——写完之后应当枚举这类元素到底有几个。
- **`min-width: auto` 的症状会报在反方向。** 它让"布局由内容决定"，但用户看到的是"侧栏太长"，
  第一直觉会去查侧栏里的文字省略，而省略早就写好了。凡容器尺寸该由设计定、不该由数据定，
  就把 `min-width` 写在**容器**上。
- **内联样式会静默吃掉样式表的同名约束。** 凡是"算出来的值要和设计值取小"，就在 CSS 里用
  `min()`／变量把两者显式并列，而不是让内联值覆盖掉设计值。冒烟为此单开一条断言。
- **隔离变量再下结论。** 侧栏这一条第一次是把两个变量（当前标题 + 列表条目）一起灌长的，
  看起来"列表文本导致侧栏变宽"；拆开单独灌才定位到真正的驱动者。测试几何问题时，
  一次只动一个变量。

**边界（如实记录）**

- 胶囊浮窗面宽 340px，比节点浮窗（380px）窄：它列的是工具入参出参，宽度够用且更省横向空间。
- 竖直可容高度有下限 `180px`（`Math.max(180, …)`）：胶囊极靠上下沿时宁可靠滚动，也不压成一条。
- 面高上限 520px 仍有一处 JS 镜像 `POP_H`（node 浮窗的竖直夹取用），两处需一起改；
  胶囊浮窗已改走 `--cv-pop-max` 变量，不再有第二份数字。

### 4.13 角色图标按 role 解析，另修好预览页在 file:// 下整页空数据（2026-09-21，成员 D）

**需求**（用户原话）

> agent 配置的 icon 似乎是默认显示名称的第一个文本？这样很丑，和协作画布图节点的机器人
> icon 保持一致，也添加一些可能会用到的 icon。

**1. 首字方块换成角色图标**

配置页角色卡的头像位原先渲染 `agent.name.slice(0, 1)`（「信」「数」「报」）。新增
`frontend/src/components/AgentGlyph.tsx` 作为**唯一**的判断处：先按 `role` 全表匹配语义，
再按显示名，最后回退机器人；配置页（卡片 + 弹窗头）与协作画布 `NodeGlyph` 都调它。
决策与口径见 ADR-029。

画布侧只换「角色」那一档，状态档不变——顺序是 `failed` → `paused` → `running` → 角色图标。
实测（CDP，线上 5173 与预览页各跑一遍）：

| 位置 | 信息收集 | 数据分析 | 报告生成 | 自定义「任务规划」 |
| --- | --- | --- | --- | --- |
| 配置页角色卡 | `lucide-file-search` | `lucide-chart-line` | `lucide-file-text` | `lucide-list-checks` |
| 侧栏 / 全屏画布节点 | 同上 | 同上 | 同上（seed 里该步 `running` → 让位给转圈） | — |

每个图标在 34px 方块里 17×17、居中偏差 **dx = dy = 0**，头像位 `textContent` 为空
（即那一格不再有文字）。`running` 的节点实测是 `lucide-loader-circle` 且带 `spin`，
证明「状态优先于角色」在真浏览器里成立。

**2. 冒烟断言**

- `config-smoke` **69 → 77**：卡片头像位是 `<svg>` 且不含汉字、analyst → 折线图、六条
  `role → 图标键` 用例（含 `summarizer` / `translator` / `reviewer`）、`role` 优先于显示名、
  两轮回退（按显示名 → 机器人）、每个图标键都能渲染出图形、**键与图形一一对应**
  （两键共用一张图是静默错配，只能这样发现）。
- `workspace-smoke` **151 → 154**：侧栏节点三个角色图标齐、执行中的节点画转圈且带 `spin`、
  同一张图里已跑完与未开始的节点各带自己的角色图标。

**3. 顺带发现并修好：预览页在 `file://` 下整页空数据**

用 CDP 打开 `rendercheck/ui-preview.html` 复验时，页面自己报了
`连接异常 · 预览未收录：GET /C:/api/v1/agents` —— **种子里一条都没命中**。根因在
`rendercheck/preview.tsx` 的 mock：

```ts
const url = new URL("/api/v1/agents", location.href);  // file:///C:/Users/.../ui-preview.html
url.pathname                                            // → "/C:/api/v1/agents"
```

Windows 下 `file://` 的**盘符会混进 pathname**，于是 `GET /api/v1/...` 永远查不到；
而「双击打开」正是这个预览页的既定用法（见 `build-preview.py` 文件头，也是评审时唯一会
用到的方式）。修法是按 `^\/[A-Za-z]:(?=\/)/` 剥掉盘符前缀——HTTP 下是空操作。

**这条为什么一直没被发现**：该预览页的离线自检（jsdom，见 §3.3）用的是 HTTP 形状的
`location`，`pathname` 本来就是 `/api/...`；而「双击打开」这条使用路径从来没有被自动检查
覆盖过。**声明的用法和自动检查的用法不一致**，缺口就一直留着。

**4. 边界（如实记录）**

- 关键词表不完整：新角色不进表就回退机器人（不会画错，只是不够贴切）。
- 15 张图标进包，JS 419.13 → **424.97 kB**（CSS 86.25 kB）；`lucide-react` 按图标 tree-shake。
- 没做后端 `icon` 字段：那要动 `agent_registry` 的库表与 §5.7 的响应契约，否决理由见 ADR-029。

### 4.14 多智能体拓扑支持面盘点，画布并行能力可验证化（2026-09-21，成员 D）

**1. 改了什么**

| 文件 | 改动 |
| --- | --- |
| `frontend/rendercheck/workspace-smoke.tsx` | 新增「扇出 → 并行 → 汇聚」三波形状断言，**154 → 159** |
| `frontend/rendercheck/preview_seed.py` | 新增 `DYNAMIC_PLAN` / `DYNAMIC_TRACE_REASON` / `new_dynamic_workflow()`；`stage_traces()` 增动态分支 |
| `frontend/rendercheck/build-preview.py` | 注入动态工作流三条路由（42 → **45** 条） |

**2. 为什么补这组断言**

原有用例只覆盖「同波两个节点 + 一起汇入」一种形状，而动态链路真正要表达的是
「**一个上游分叉成两路并行、再合并收尾**」。缺这条时，「画布支持动态并行」只能靠读代码相信。
数据形状取后端 `dynamic_checkpoint_summary` 的四个字段（`id` / `role` / `depends_on` / `status`），
所以同一条用例同时锁住前后端契约。

断言清单（5 条）：波次分组 `1,2,1` / 只有分叉波带 `parallel` / 边口径
`serial,serial,parallel,parallel` / 三波各占一行且中间行两个 / 只有分叉波画「并行协作区」。

**3. 预览页为什么能证明「不是只能画串行」**

`stage_traces()` 原来只按静态三步返回、`mode` 恒为 `static`——**预览页结构上看不到动态画布**。
新增的动态对话（`checkpoint.mode = "dynamic"` + 带 `depends_on` 的 `plan`）走的是与后端
`app/api/stage_trace.py::read_stage_traces` 相同的 `not_integrated` 分支，
所以预览与真实环境口径一致：**形状齐（来自 `plan`）、详情空（未集成）**。

**4. 实渲截图（SSR 量不到宽度，必须客户端渲染）**

把画布渲成图时踩到一次：`renderToStaticMarkup` 下 `useElementSize` 量到宽度 0，
布局退化成紧凑档、画出来挤在左侧且并行框溢出边界。**改用客户端渲染**（esbuild
`--platform=browser` + `createRoot` + `chrome --headless --screenshot`）后宽度正常。
教训：**画布的几何断言要走真实测量路径**，SSR 只能验结构与状态，验不了排布。

**5. 边界（如实记录）**

- 执行层仍未并行：`dynamic_graph.py` 的 `ready[0]` 与 `dynamic.py` 的顺序循环都在 A 线；
  计划声明同波时，图在说并行、实跑没并行（ADR-030「代价」一节）。
- 角色池只有三个（`RoleId`），池外角色会让整份计划作废并回退固定三步。
- 前端**没有**编排模式开关；默认 `static`（`app/config.py:65`），要看动态画布需服务端配置或请求体字段。
- 未跑全量 pytest：Docker Desktop 未启动（同前几轮）。

### 4.15 对话流 Markdown 正文、渐进揭示与内联执行轨迹（2026-09-21，成员 D）

**1. 用户反馈与病根**

反馈原话：「当前的文本输出缺少流式渲染，很多符号并没有相应渲染，效果不是很好，同时缺少在相应位置
展开/收起思维连，还有具体工具输出的地方，按照主流 agent 的实现方法完善」。两处病根：

- `App.tsx::MessageBubble` 正文是 `<p>{message.content}</p>`。模型按 Markdown 组织输出，
  记号（`**` `##` `|`）原样吐出——**结构全丢**，不是观感问题。
- 执行轨迹只在执行台弹窗（§5.17）与记录页，对话流只剩一行 `.run-event` 进度文本。
  要在对话里理解「这一步为什么得出这个结论」，得跑到另一块面板对时间线。

先追问确认了三个分歧点（都撞上已有的、已冻结的约束），三项均取推荐项：
Markdown 用成熟库而不是自研；流式只做**前端呈现层**而不是后端起 SSE；
「思维链」展示**真实的 ReAct 行动链**而不是去落盘模型 reasoning
（后者要改 ADR-018、§5.17、本文件并动 A/B 线）。

**2. 改了什么**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/Markdown.tsx` | 新增。GFM 渲染；`pre`/`code`/`table`/`input`/`a` 五个自定义渲染器；不挂 `rehype-raw` |
| `frontend/src/components/useStreamText.ts` | 新增。渐进揭示：按总时长（320–1800ms）反推步长；文本变长从断点续；非浏览器与 reduced-motion 降级为全文 |
| `frontend/src/components/Disclosure.tsx` | 新增。受控折叠块（原生 `<details>` 的状态父组件改不了） |
| `frontend/src/workspace/RunActivity.tsx` | 新增。对话流内联执行轨迹；正在跑的步骤默认摊开；跑完收成一行 |
| `frontend/src/workspace/TraceParts.tsx` | 新增。`ToolCallBlock` 抽出来给弹窗与对话流共用（原先只有弹窗有） |
| `frontend/src/App.tsx` | `MessageBubble` 走 Markdown；`RunActivity` 按 `reportIndex` 插在提问与答复之间；`revealed` 集合决定谁播动画 |
| `frontend/src/workspace/collaboration.ts` | 导出 `stageMetaFor`（阶段 id 优先、角色兜底） |
| `frontend/src/workspace/AgentStageModal.tsx` | 工具调用改调共享实现，删掉本地那份 |
| `frontend/src/styles.css` | Markdown 版式 / 折叠块 / 执行活动卡片；**移除** `.run-event` |
| `frontend/vite.config.ts` | markdown 栈拆独立 chunk |
| `frontend/package.json` | `+ react-markdown@^10.1.0`、`+ remark-gfm@^4.0.1` |
| `doc/decisions/031-conversation-stream-rendering.md` | 新增 ADR：把 ADR-018 的「三块视图」扩成四块，改按**时间面**划边界 |
| `doc/api.md` §7 | 四块视图对照表 + 对话流细则；顺带修掉一处悬空引用（原文写「见 §7.1」，无此小节） |

**3. 验证**

```bash
npm --prefix frontend run build
# 通过：tsc --noEmit && vite build，无 500 kB 警告
# 产物 index-Bv3Mc4mw.css 91.70 kB / markdown-pr2ruGPi.js 166.03 kB / index-CbrzEpDA.js 422.69 kB
```

- `rendercheck/workspace-smoke.tsx` → **201/201**（159 → 201，+42；含 Markdown 六类结构、
  折叠块可达性、执行活动卡片四种状态、以及「对话流与弹窗同一套小标题」的防漂移断言）
- `rendercheck/config-smoke.tsx` → **77/77**（无回归）
- `rendercheck/preview.tsx` 等三个文件单独过 tsc：只剩既有的 `node:fs` / `process`
  找不到声明的噪音（项目不依赖 `@types/node`），本轮新增代码零报错。
- 产物样式完整性：按**出现次数**（不是行数，压缩后只有一行）核对 `md-body` 37、
  `run-activity` 21、`md-table-wrap` 6、`disclosure-head` 3、`md-caret` 2、`run-step-tools` 1 ——
  全部存活，没有被压缩器丢掉。

**4. 实渲取证（CDP + 真实浏览器，客户端渲染）**

`renderToStaticMarkup` 量不到宽度、也不跑 effect，验证不了版式与动画，所以走 CDP 驱动
headless Chrome 打开离线预览页 `file://.../ui-preview.html`，点到「执行中」的会话后取证：

| 断言 | 实测 |
| --- | --- |
| 正文里没有未渲染的记号 | `.conversation-transcript` 的 `innerText` 匹配 `/\*\*|\| ---/` → **false** |
| 提交一条含七类结构的消息后 | 用户气泡内出现 `H2` / `STRONG` / `CODE.md-code` / `TABLE` / `UL` / `LI`×2 / `BLOCKQUOTE` / `CODE.md-code` |
| 气泡底色迁移 | `.md-body` 的 `background` = `rgb(237,243,245)`（= `#edf3f5`），内层 `p` 为 `transparent`（否则 Markdown 下会「一段一个气泡」） |
| 内联卡片几何 | `.run-activity` = **760×506**（拿到了真实宽度，不是 0），无横向溢出 |
| 默认展开面 | 3 个折叠块、**只 1 个展开**（正在跑的那一步） |
| 摘要不丢信息 | 收起的步骤仍带「信息收集 / 2 次工具调用 / 已完成」 |
| 状态着色 | 已完成 → `tone-green` `rgb(47,143,107)`；执行中 → `tone-accent` `rgb(63,127,147)` |
| 工具入参出参 | 展开后 2 个 `.ws-trace-step`、2 个 `details[open]` |
| 渐进揭示 | 覆盖媒体特性后 0.55s 时 **62/70 字**仍在增长，`::after` 光标 7×14px、`animation-name: md-caret`；随后自动收尾到全文 70 字 |
| 页面报错 | **0** |

**5. 踩到的坑**

- **headless Chrome 默认上报 `prefers-reduced-motion: reduce`**，直接把渐进揭示的降级分支
  打开：`animation-name` 量到 `none`、`.is-streaming` 一直不出现。**看起来像动画根本没实现。**
  要验动画必须 `Emulation.setEmulatedMedia` 显式覆盖成 `no-preference`（脚本已按这个来）。
  反过来，这条也顺带证明了降级分支真的生效——在 reduced-motion 下正文是立即全文。
- **CDP 脚本每次调用都重新导航**，跨调用攒状态是白费：点开的下拉、展开的步骤全丢。
  必须把「点击 → 等待 → 取证 → 截图」放进**同一个连接**里顺序执行。另外每个
  `Runtime.evaluate` 共享全局作用域，同名 `const` 第二次声明会 `SyntaxError`
  （本轮踩到一次，`Identifier 'b' has already been declared`）——用 IIFE 包起来。
- **npm 装 `react-markdown` 遇到 `ECONNRESET` 重试**：多个包的 `cache revalidated` 耗时
  30–40s，首次安装（无 pipe、`--loglevel=http`）约 3 分钟。不是挂死，是慢；
  命令里不要 `| tail`，否则中途看不到任何输出，无法区分「慢」和「挂」。
- **引入 markdown 栈把主包推过 500 kB**：402 kB → 589 kB，Vite 开始告警。
  在 `vite.config.ts` 里把这一栈拆成独立 chunk（422.69 + 166.03 kB）恢复无告警。

**6. 边界（如实记录）**

- **渐进揭示是呈现效果，不是流式传输。** 后端没有事件流，助手正文是工作流终态一次性落库的
  （`app/workflows/pipeline.py::finalize_activity`）。要真正的流式语义，需 **B** 在
  `app/workflows` 于阶段推进/工具调用处发布事件、**D** 新增只读事件流端点；
  且 LLM 逐 token 输出受 Dapr 活动边界限制，现实上限是「按阶段/工具粒度推送」。
- **展示的是真实的 ReAct 行动链，不是模型思维链。** 模型内部推理没有落盘，服务端也不提供；
  界面上不编造。要展示真思维链需 **C** 落库 reasoning + **A** 透传，并同步改 ADR-018、
  §5.17 与本文件中「不假装有思维链」那条断言。
- **只跑了前端门禁**：未跑全量 pytest（Docker Desktop 未启动，同前几轮）。
- `rendercheck/ui-preview.html` 的 jsdom 自检脚本（`verify_preview.mjs`）不在仓库里
  （项目刻意不引前端测试依赖），本轮改用 CDP 实跑替代，覆盖更强。

### 4.16 工作区沙箱、破坏性动作审批与出网策略（2026-09-23，成员 D）

**1. 背景与前置决策**

需求原话是四条：用户选一个本地工作文件夹；Agent 全部业务限定在该文件夹内；目录内可
增删改查；禁止访问目录之外的本地文件——另加一条「网络要单独做一层白/黑名单，拦
`127.0.0.1`/`10.x`/`172.16-31`/`192.168.x`，只允许公网域名」。设计先落成两份 ADR
（[ADR-033](decisions/033-workspace-sandbox.md) / [ADR-034](decisions/034-egress-policy.md)），
使用者拍板了四个口径：宿主固定根挂 `/workspace`；默认档位 `read_only`；出网默认
`public_only`；**模型流量按平台内部依赖处理**（豁免私网判定，Ollama 与内网网关继续可用）；
**沙箱要能直接读写 `/workspace`**。

**2. 分期与实现**

| 阶段 | 提交 | 内容 | 关键取舍 |
| --- | --- | --- | --- |
| 设计 | `adf58ac` | ADR-033/034 + `doc/api.md` §6 规划契约 + `doc/data-model.md` §3.3 + `doc/deployment.md` | 工作区授权单位是「根 + 根内子目录」（浏览器给不了后端宿主路径，bind mount 又在容器创建时固定） |
| 阶段 1 | `562360d` | `app/workspace/`（守卫/服务）、`workspaces` 表、五个工作区接口、`list_work_files`/`read_work_file` | **只读起步**：写档位先不提供，避免"存下来却不生效的假开关" |
| 部署接线 | `427fa81` | compose 挂 `${WORKSPACE_HOST_ROOT}:/workspace`、`WORKSPACE_ROOT` 与沙箱宿主根分开 | 同名变量最容易把宿主路径与容器路径写串 |
| 阶段 2 | `85f06c5` | 写档位 + `write_work_file`/`make_work_dir`/`move_work_entry` + 配额 + 提档 PATCH | 只放开**非破坏性**写：删除与覆盖要审批，而审批在阶段 3 |
| 阶段 3 | `b23dd5b` | `approvals` 表与接口、`delete_work_entry`（软删除）、覆盖/移动覆盖走审批 | 不做工作流级挂起，改「批准一次、放行一次」——比长期放行更强 |
| 沙箱挂载 | `e37b757` | `app/sandbox/workspace_bind.py`、容器只挂会话工作区、`open` 放行 | 沙箱是**兄弟容器**，bind 来源必须是宿主路径；翻不出来就显式失败 |
| 出网策略 | `1f2087e` | `app/security/`（判定 + 钉扎取数）、工具与 MCP 接线、`GET /config/egress` | 策略检查放在**打开会话时**，保住 ADR-026「合并工具目录零 IO」 |

**3. 验证（本轮）**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit tests/integration/test_workspace_api.py tests/integration/test_egress_api.py -q
# 902 passed, 12 failed（12 条仍是本地缺 pypdfium2 的附件/PDF 渲染用例，与本轮无关）
```

新/改套件共 **225 例**：`test_workspace_paths` 34、`test_workspace_service` 45、
`test_work_file_tools` 20、`test_workspace_approvals` 11、`test_workspace_api` 29、
`test_sandbox_workspace_bind` 9、`test_egress_policy` 35、`test_egress_fetch` 6、
`test_egress_wiring` 3、`test_egress_api` 2，另有 `test_sandbox_docker_runtime`（+9）与
`test_search_gateway`（+2）的增量。

真机（重建后的部署栈，不是单测）：

- **阶段 1/2**：登记工作区 201 → 容器内写 `reports/2026/summary.md`（自动建两层目录）
  → 目录树看到 15 B → `read_work_file` / `make_work_dir` / `move_work_entry` 均生效
  → 覆盖与越界分别得到「需要审批」「路径不能包含 `..`」且都**不可重试**
  → 解绑 204 后**宿主文件仍在**（只解除登记，不删文件）；
- **阶段 3**：新文件免审批 → 覆盖得到 pending → 列表 `pending=2` → 批准 → 重试覆盖读出 v2
  → 批准删除 → 重试后进 `.trash/20260922T171508-todo.md` → 二次决策 409 `APPROVAL_NOT_PENDING`
  → 两条审批最终都是 `consumed`；
- **沙箱挂载**：`resolve_host_path` 从真实 mountinfo 里算出
  `/run/desktop/mnt/host/c/…`，先用 `docker run -v` 实测该路径可当 bind 来源；写档下
  沙箱列出/读出/写入工作区文件（宿主侧能看到 `from_sandbox.txt`），切只读档后挂载变 `ro`、
  **读仍成功而写入 `OSError`**；
- **出网策略**：`https://10.0.0.5/`、`169.254.169.254` 被 `private_ip` 拦下；
  **本网络把 `api.github.com` 解析到 `127.0.0.1`，也被拦下**（真实的 DNS 污染案例）；
  私网 MCP 条目在建会话时被拒；模型豁免按 `purpose` 区分同一地址；`web_search` 经内部
  search-gateway 仍正常返回结果；`GET /config/egress` 正确投影配置。

**4. 边界（如实记录）**

- **`rw` 挂载下审批可被绕过**：沙箱代码能直接写/删工作区文件，不经过审批。要严格执行
  审批就把 `WORKSPACE_SANDBOX_MOUNT` 设为 `ro`（沙箱只读，写全走被审批的工具）；
- **沙箱进程身份**：代码默认 `nobody`，但 Docker Desktop 把 bind 统一显示为 uid 0，
  `nobody` 写不进去（真机实测 `PermissionError`）。compose 因此默认 `SANDBOX_UID/GID=0`，
  Linux 宿主建议改成宿主 uid；
- **出网策略的四项已补齐，但最后一步未实测**：新增 `egress-proxy`（明文 HTTP 绝对 URI +
  HTTPS `CONNECT`，两种形态共用同一套「解析 → 私网判定 → 钉扎」），MCP/模型 SDK 靠
  `HTTP_PROXY`/`HTTPS_PROXY` 整段落到代理上（不再需要 SDK 内部钉扎），search-gateway 脚本
  显式构造 `ProxyHandler` 接入，沙箱联网时只接内部网络并注入代理变量。
  **最后一步（internal-only 网络）已实测**：backend 与 search-gateway 只接
  `internal: true` 的网络，物理上无法直连出网；宿主侧入口改为前端反代
  （`/api/` 与新增的 `/health`，`start.ps1` 随之改）。实测结论：
  **internal 网络里端口发布无效、外部 DNS 也不通**（正好印证代理模式的设计），
  backend 里 `socket.gethostbyname('www.baidu.com')` → `gaierror`，而同一进程走代理的
  `urlopen('https://www.baidu.com')` → **200**；`http://10.0.0.5/` 与
  `http://169.254.169.254/` 由**代理侧**回 403；联网沙箱里同样"代理 200 / 私网 403"。
  过程中踩到两个真问题并修掉：**gRPC 只认小写 `no_proxy`**（只给大写会让 Dapr 的
  durabletask worker 把 gRPC 发给代理拿 403 后无限重试）、以及**应用侧把"有代理"错当成
  "所有目标都走代理"**（内部服务应直连，`NO_PROXY` 语义要在 `EgressPolicy` 里实现）；
  另外 `host.docker.internal` 在 internal 网络里不可达，内网模型端点必须经代理放行，
  已写进部署文档；
- **前端已接（同轮）**：配置页新增「工作区」分区（登记、档位提降、目录树与配额、越界
  符号链接标注），「执行边界」扩成沙箱 + 出网策略（只读），对话流内加审批卡片
  （pending / approved / consumed / denied / expired 五态），并在 `App.tsx` 按会话轮询审批。
  门禁：`npm run build`（tsc + vite）通过；`config-smoke` **86/86**（新增工作区与出网 9 条）、
  `workspace-smoke` **210/210**（新增审批卡片与导航角标 9 条）；部署形态下前端容器已服务新 bundle，
  `/api/v1/workspaces` 与 `/api/v1/config/egress` 经前端反代均 200。浏览器里的观感仍按
  `doc/deployment.md`「演示与验收」的清单人工过一遍（前端没有测试框架，这是既有限制）；
  **另用无头 Edge（CDP）实跑了一遍**（`--headless=new --remote-debugging-port`，跑完即杀）：
  配置页确实渲染出 5 个分区（含「工作区」）、工作区面板的文案与登记表单在位、「执行边界」里
  出网策略只读段在位；造一条真实审批后打开对应会话，导航角标显示 **1**，卡片文案为
  「需要你确认 / 删除 notes/todo.md / 待你决定 / 允许一次｜拒绝」，点「允许一次」后卡片变为
  「已允许——Agent 重试同一调用时才会执行」、角标消失，且服务端该条审批确实变成 `approved`
  （`pending=0`）——这条把「界面 → 接口 → 数据库」整条链路串起来验了一遍。

**第三轮（同日）：把「像附件一样选文件夹」补上**。需求原话是「不能像附件上传一样，点击按钮
就弹出磁盘文件吗，然后直接可以选择工作文件夹」。拆成两条各自成立的路：

- **应用内「选择文件夹并导入」= 导入副本**（`<input type="file" webkitdirectory>` →
  `POST /workspaces/{id}/files`）。验证：`import` 的服务层/接口/端到端共 **12 例**（含
  「HTTP → 服务层 → 真实磁盘」那一例）；`config-smoke` 升到 **88/88**（新增
  「剥掉顶层目录名 / 超限单列 / 不出现 `showDirectoryPicker`」三条）。
  **浏览器实跑**（本地静态站 + mock API + 无头 Edge/CDP，不起容器）：在页面上选中工作区、
  往文件输入框塞两个真实文件、派发 `change`，界面回显「已导入 2 个文件」、用量从 `0 B / 0 项`
  变 `37 B / 2 项`、目录树出现 `a.md`（25 B）与 `root.txt`（12 B），**服务端 mock 打印
  `IMPORTED=a.md,root.txt`**、磁盘上确有两个文件——按钮 → handler → HTTP → 落盘 → 目录树刷新
  整条验通。自动化上有一条限制如实记录：**CDP 的 `DOM.setFileInputFiles` 填不进
  `webkitdirectory` 模式的输入框**（浏览器要求由目录选择器填充，原生 input 则能填），
  所以这一跑是在页面里临时去掉该属性走的**同一个 `onChange` handler**；目录选择对话框本身是
  浏览器原生行为，脚本化不了，仍需人工点一次确认观感。
- **宿主侧 `scripts/pick_work_dir.ps1` = 真直连**：在宿主弹原生文件夹对话框、写
  `deploy/.env` 的 `WORKSPACE_HOST_ROOT`，再重建 backend 让 bind mount 生效；脚本**拒绝**
  选中仓库目录（阶段 2 起沙箱对工作区可写）。验证：`-?` 走参数绑定与帮助、退出码 0。
  踩到一个真实的坑并修掉：**`.ps1` 必须存成 UTF-8 with BOM**——Windows PowerShell 5.1 对无 BOM
  的脚本按 ANSI 读，脚本里的中文**字符串**错位后会吃掉引号、直接变成语法错误
  （`start.ps1` 不受影响是因为它的中文只在注释里）。这条已写进脚本头部注释。

**部署形态与真机点击已补跑（同日，修复见 `1a412d5`）**：Docker 引擎恢复后重建 backend、
在宿主上按脚本流程实点到真实文件夹，结果**换根根本没生效**——`deploy/.env` 被写成**一行**
（生成的注释与 `WORKSPACE_HOST_ROOT` 同行），整行以 `#` 开头，compose 于是回落到默认的
`../workspaces`（仓库内目录），而脚本照样打印「工作区根已写入」。根因是 PowerShell 的
标量/数组退化：`Get-Content | Where-Object` 在只剩一行时返回**字符串**而不是数组，
`$lines += "..."` 退化成字符串拼接——所以它只在**第二次运行**发作，而换目录正是这个脚本最
常见的用法。修复：`@(...)` 强制数组、按前缀剔掉上次生成的注释行（重复运行收敛成固定两行）、
写完**回读确认变量独占一行**否则显式报错。修后重跑（第二轮点击）：`.env` 两行正确、容器
`/workspace` 的挂载源变为 `D:` 盘的 `/测试`、宿主侧标记文件出现在平台目录树里、
`write_work_file` 写出的文件落在宿主磁盘；沙箱反查出的 `/run/desktop/mnt/host/d/测试`
在兄弟容器里挂出来是**同一个目录**。顺带记录一条运维坑：`deploy/docker/nginx.conf` 的
`proxy_pass http://backend:8000` 没有 `resolver`，nginx 启动时解析一次上游，换根只重建
backend 时若新容器拿到不同 IP，前端会 502 到重启 frontend 为止（本轮没踩到只是 IP 被复用）。

**第四轮（同日，`329e0f2`）：工作区入口从配置页搬到工作台**。工作区是**按会话**生效的
（会话级注册表只挑绑定本会话的那一条），入口却埋在「工具与配置」里，离它真正起作用的地方
隔了两层。于是搬进工作台：顶栏一个「工作区」开关 + 右侧抽屉，登记、选路径、提降档、目录树、
用量与导入副本都在里面；配置页回到 4 个分区（Provider / 默认路由 / MCP 工具 / 执行边界）。
面板文件同时从 `config/` 移入 `workspace/`（工作台视图的归属目录，ADR-018）。

三处取舍写进了 `doc/api.md` §7 / §7.1：**草稿态只暂存不登记**（工作台起步 `session = null`，
此时 `POST /workspaces` 只会得到一条 `session_id=null` 的记录，会话级注册表不选它、
Agent 拿不到任何文件工具，正是文档反复在修的「存下来却不生效」）；**补登记放在发消息之前**
（编排一开跑就用当时的绑定解析工具集，晚一步可能让这次执行漏掉文件工具），绑定失败不吞消息、
走既有的部分失败通道明说；**登记带 `session_id`**——原面板建的是未绑定工作区，那才是它一直
没真正生效的地方。草稿态也不拉 `GET /workspaces`：那返回的是所有登记，摆出来像「已经选好了」。

门禁：`npm run build`（tsc + vite）通过，产物里确认 `.workspace-drawer` 三条规则；
`config-smoke` **80/80**（配置页只剩 4 个分区、不再挂这块面板）；`workspace-smoke`
**223/223**，新增 7 条（入口在工作台且面板已搬入 `workspace/`、登记带 `session_id`、
草稿态只暂存不 POST、草稿 → 会话建好时补登记且失败不吞消息、草稿态文案与暂存回显、
草稿态不渲染「已登记」列表、草稿态措辞不说成「还没有登记工作区」），
原属 `config-smoke` 的工作区用例随之搬到 `workspace-smoke`。

**当时的遗留已补跑**：本轮前端容器重建时 Docker 引擎再次对所有 API 路由返回 500
（`version` / `images` / `containers` 均挂起），栈里一度仍是旧构建；引擎恢复后
`docker compose up -d --build backend frontend` 已重建，容器里 serve 的 bundle 与本地构建一致
（`index-T6pfPrDq.js`）。「新抽屉在浏览器里的观感」仍按 `doc/deployment.md`
「演示与验收」清单人工点一遍——这部分前端没有测试框架，是既有限制。

**第五轮（同日）：补上「选择文件夹位置」**。使用者指出改过的抽屉里**选不了文件夹位置**，
只能盲打相对路径。查下来这不是漏做，是**文档承诺了、接口层支撑不了**：ADR-033 §1 写的是
「用户能『选』的是这个根**下面的子目录**，前端通过服务端的**目录浏览接口**来选」，
影响清单里也列了 `frontend/` 负责「工作区选择器」，但接口只有
`GET /workspaces/{id}/tree`——它要求**先有一条登记**，而选位置必须发生在登记**之前**，
用它选位置是循环依赖。

补法（文档先行）：`doc/api.md` §5.19 新增**只读**接口
`GET /api/v1/workspace-root/tree?path=&depth=`，基准换成**工作区根**，因此不需要任何登记；
出参与 `{id}/tree` 同形但**没有** `workspace_id`（根不是一条登记）。§7.1 增加
「选择文件夹位置 = 根内目录选择器」一条，并写明它**不解决**什么——浏览器拿不到宿主路径、
bind mount 又在容器创建时固定，换根仍是部署动作（`scripts/pick_work_dir.ps1`）。
服务层 `root_tree()` 复用同一套路径守卫与目录收集（越界符号链接只标记不跟随、跳过回收站、
目录优先）；前端在登记表单旁给「浏览根目录」：面包屑 + 目录列表 + 上一级 + 「选定此文件夹」
回填相对路径，**只列目录**（这一层产物是路径，铺文件只会让人误点），越界链接列出来但点不动。
接口形状上刻意避开 `GET /workspaces/root/tree`——那条与 `{id}/tree` 形状相同，谁能命中只
取决于**路由注册顺序**，以后调一下顺序就会静默换成另一个语义。

验证：`tests/unit/test_workspace_service.py` 新增 7 例（根不依赖任何工作区且不写库、
走子目录 `depth=2`、`../` `/etc` `C:/windows` `..\..\x` 四个越界变体全部拒绝、
越界符号链接只标记不跟随、功能关闭时 503 语义），`tests/integration/test_workspace_api.py`
新增 4 例（根接口出参无 `workspace_id` 且不碰工作区表、越界 422 `WORKSPACE_PATH_REJECTED`、
根不可用 503 `WORKSPACE_ROOT_UNAVAILABLE`、两条近形路由互不吃掉）。全量
`pytest tests/unit tests/integration/test_workspace_api.py tests/integration/test_api.py`
→ **957 passed / 12 failed**（12 条仍是本地缺 `pypdfium2` 的 PDF 用例，与本轮无关）。
前端 `npm run build` 通过；`workspace-smoke` **226/226**（新增 3 条：挂载即读根、
选位置走的是根接口而非 `{id}/tree`、文案把「挂进来的根」与本机磁盘分开且越界项不可进）、
`config-smoke` **80/80**。真实栈（重建 backend + frontend）：
`GET /api/v1/workspace-root/tree` 经前端反代返回宿主根的真实内容且响应无 `workspace_id`，
`?path=../etc` 回 **422**，容器 serve 的 bundle 与本地构建一致。

边界（如实记录）：这一轮让「选位置」有了入口，但**只覆盖根内的子目录**；根本身（宿主上那个
目录）仍然只能由部署者用宿主侧脚本或改 `WORKSPACE_HOST_ROOT` 决定——这是 ADR-033 §1 的既定
口径，不是本轮未完成项。另外使用者当前的根 `D:\测试` 里没有子目录，界面上会直接提示
「这一层没有子目录，可以直接选定当前位置」。

**第六轮（同日）：宿主形态落地——「当场选本机文件夹」端到端可用**。使用者定了范围与形态：
「用户可以在本地机器任意选择一个文件夹，然后在那个文件夹下的所有文件都可以读写，但也只能
在这个文件夹下面」＋「把宿主直跑做成一键默认」。设计与取舍见
[ADR-035](decisions/035-local-service-dynamic-workspace-root.md)（含为什么**不加列**、
为什么浏览范围要按形态判定、为什么仍然拒绝选平台自身源码目录）。

服务端：`WORKSPACE_SOURCE`（默认 `host`，非法值显式报错）＋ `resolve_host_dir()`（绝对路径、
必须存在且是目录、拒绝平台源码目录及其祖先/后代）＋ `workspace_base()`（目录树 / 文件工具 /
导入 / 沙箱挂载统一走它）＋ `host_tree()`（只读、只列目录、条目给绝对路径、附盘符与家目录入口）
＋ `GET /api/v1/host/tree`；创建接口的 `mode` 默认改为 `None`，默认档按形态取（宿主可写、
容器只读）。`deploy/compose.yaml` 显式 `WORKSPACE_SOURCE: container`——代码默认已是 host，
容器不声明就会被当成宿主形态，`/workspace` 挂载与相对 `path` 会立刻失效。

前端：选择器先问宿主目录（默认形态），容器形态按错误码 `WORKSPACE_HOST_BROWSE_DISABLED`
退回根内浏览——用错误码判而不是两个接口都试一遍，是为了默认路径只发一次请求。宿主浏览器
显示**绝对路径**、盘符与家目录入口、上一级，只列文件夹，越界链接列出来但点不动。

一键直跑 `scripts/start_local.ps1`：Redis/PostgreSQL 容器 → 腾出 8000/3500/5173（stop 容器里的
backend / dapr-sidecar / frontend）→ 本地 sidecar 与后端（绑 127.0.0.1）→ Vite dev。
`-Stop` 按记录的 PID 收工。过程中撞到并修掉两个真问题：

1. **`uv run` 会重新解析依赖**，本机镜像对 `asyncpg` 返 403，后端以
   "requirements are unsatisfiable" 收场、根本没启动。改成直接用项目 `.venv`
   （容器里本来也是 `uv run --no-sync`）。
2. **`-Stop` 只杀包装进程**：记录的 PID 是 powershell / cmd，真正占端口的是子进程
   `daprd.exe` 与 vite 的 `node.exe`，只杀父会留下孤儿、下次启动撞端口。改成
   `taskkill /PID <记录的 PID> /T /F`（仍只按 PID 杀自己记录的那几个，绝不按名字杀）。

验证：全量 `pytest tests/unit tests/integration/test_workspace_api.py` → **955 passed /
12 failed**（仍是缺 `pypdfium2` 的 PDF 用例）；新增 9 例服务层 + 3 例接口（绑选定文件夹且默认
可写、区内可写而 `../` 被拒、相对路径被拒、不存在/非目录被拒、拒绝平台源码的三个方向、
留空落家目录、只列目录且条目为绝对路径、从家目录起并能上一级、容器形态 503 语义、宿主浏览
出参、创建接口收绝对路径）。前端 `npm run build` 通过；`workspace-smoke` **228/228**
（新增：先问宿主、按错误码退回、宿主浏览器显示绝对路径与盘符且只列文件夹、读写边界文案）、
`config-smoke` **80/80**。真机（本地服务形态，非容器）：`GET /api/v1/host/tree` 返回
`C:\Users\zq` 的真实目录与 `C:`/`D:` 盘符入口；`POST /workspaces` 以绝对路径 `D:\测试` 登记成功
（`mode=workspace_write`），目录树随即列出该文件夹的真实内容。

边界（如实记录）：**选根是自由的，根内路径约束一字未改**——「只能在所选文件夹下面」由
`resolve_in_workspace()` 保证。容器形态保留原样（固定根 + 相对 `path` + 默认只读），两种形态的
`path` 不通用，切形态会让旧登记解析失败并**显式报错**。宿主形态只绑 `127.0.0.1`，是单人单机
前提；多用户或公网部署必须回到容器形态。

**第七轮（同日）：动态编排漏接会话记忆（使用者实测反馈）**。反馈原话是「这个项目的会话记忆
有问题，同一个会话不记得我之前说过什么」。查证结论：**写入正常、回注缺失**——该会话在 Redis
里每一轮（含用户最初的需求与后续「重试」）都在；而助手在同一会话内答「当前会话中没有任何你
此前要求我编写代码的记录」，说明模型端没看到历史。取证路径：Redis `lrange` 出该会话 6 条记忆；
该会话 3 次 workflow 全部 `has_plan = t`（走动态编排）；前端默认 `mode = "dynamic"`；
而 ADR-019 的读点只接在固定三步（`pipeline_graph._conversation_block` /
`workflows.pipeline._session_history`），动态图的两个入口都只给当前任务。

修复：`app/orchestration/context.py` 收拢 `CONVERSATION_CONTEXT_LIMIT` 与
`conversation_block()`（两条编排共用一份，避免再次分叉）；`_session_history` 提为公开的
`session_history` 供动态链路复用；`dynamic_graph` 的 `generate_plan` / `step_input` /
`run_plan_step` 增加 `history` 入口并把历史放在提示词最前面；两个动态活动按
`session_id` + `agent_run_id` 读记忆并透传（`agent_dynamic_workflow` 本来就把这两个字段
放进了 task）。**规划节点也接了历史**：用户只回「重试」时，没有历史连重试什么都判断不了。

验证：`test_dynamic_pipeline.py` +4（规划提示词带历史且历史在任务之前、**无历史时提示词逐字
不变**、步骤输入带历史前缀、`run_plan_step` 透传），`test_workflow_dynamic.py` +2（规划活动
与步骤活动确实读到会话记忆，用内存替身断言内容）；全量 `pytest tests/unit
tests/integration/test_workspace_api.py` → **961 passed / 12 failed**（仍是缺 `pypdfium2`
的 PDF 用例）。改进程影响：静态链路的既有断言（无历史时提示词逐字不变）全部保持。

**第八轮（同日）：长期记忆的异步读取（ADR-036）**。使用者要求「通过异步的方式读取用户信息
与偏好，不要影响主进程」。查证前提：长期记忆（`agent:{id}:memory`，无 TTL）自 `69960e1` 就
实现完毕，但**一直没有调用方**——运行时 `keys 'agent:*'` 为空，所以本轮先接**读取**。

形态按「增强而非必需」设计：`app/orchestration/long_term.py` 的 `prefetch()` 在**后台线程**
里读进进程内缓存（TTL 300s，同 id 只允许一个在途请求），`preference_block()` **只读缓存**、
不等待不抛错，未就绪即空串；调用点在消息受理（写会话记忆的同一处）预取全部角色，阶段执行时
缓存通常已热。注入位置在会话历史**之前**（约束 → 上下文 → 本轮任务），渲染函数在
`app/orchestration/context.py`，静态与动态两条编排共用。

**归属与写入按使用场景定了**（使用者的判断依据：「没有登录，默认只有一个用户」）：

1. **归属＝使用者一份**。不新增 `user:{id}:profile`（只有一个用户时多一层前缀换不来区分度，
   却要改 ADR-005 与数据模型、把 `user_id` 透传进 Workflow），也不按角色各存一份（偏好是
   使用者的，不是角色的——按角色存会复制 N 份并让各角色的偏好微妙分叉）。采用：**键格式不变
   （`agent:{id}:memory`），保留 id 固定为 `user`**，所有角色与规划节点读同一份。将来接多用户
   时把保留 id 换成 `user:{user_id}` 即可，读取端不用动。
2. **写入＝显式指令**，不做自动抽取（"让模型自己决定记什么"是这类系统最常见的失控点）。
   `记住：<内容>` / `记住 <名称>：<内容>`（同名覆盖）∪ `忘记：<名称>` / `忘记全部：`；
   在**消息受理**时按确定性字符串规则解析，**没有模型参与**；写入/删除后**就地刷新进程内
   缓存**，让紧接着那次执行立刻看到。
3. **审计＝只读接口** `GET /api/v1/memory/long-term`（`doc/api.md` §5.22）：长期记忆无 TTL，
   使用者必须能看见平台记住了什么；删除走上面的指令，本接口不开写口。

顺带补上记忆层缺的一块：`LongTermMemory` 协议与 Redis 实现原先**只有 save/get/list、没有删除**
（`app/memory/store.py`、`redis_store.py` 增加 `delete_entry`）——无 TTL 的存储没有删除入口
等于使用者收不回记忆，这是 ADR-036 §5 的必要条件。

验证：`tests/unit/test_long_term_memory.py` 新增 **8 例**——预取在存储阻塞时也**立即返回**
（<0.5s，用闸门 Event 构造慢存储）、缓存冷时不读存储也不报错、读失败降级为空、
缓存还热不重复打 Redis、渲染跳过空条目、`_role_input` 与 `step_input` 的段落顺序
（长期记忆 → 会话历史 → 本轮任务）、`prefetch(wait=True)` 的同步预热口；
`tests/unit|integration` 的 autouse fixture 增加长期记忆替身（用例不依赖真实 Redis）。
全量 `pytest tests/unit tests/integration/test_api.py tests/integration/test_workspace_api.py`
→ **986 passed / 12 failed**（仍是缺 `pypdfium2` 的 PDF 用例）。

写入/删除/审计的验证（同一轮补）：`test_long_term_memory.py` 另加 **5 例**（指令解析认
`记住`/`记住 <名称>`/`忘记`/`忘记全部`、**句中提到「记住」不算指令**、`remember` 落库并
**就地刷新缓存**、`apply_directives` 先存后删、`forget_all` 清空）；
`test_memory_redis_store.py` 加 **3 例**（`delete_entry` 只删指定 key、删不存在返回 False、
Redis 失败时降级为 False 而不抛）；`test_api.py` 加 **2 例**端到端（发一条含
`记住 回答长度：尽量简短` 的消息 → 记忆里出现该条 → `GET /api/v1/memory/long-term` 列出它；
`忘记：回答长度` 之后列表为空）。全量 `pytest tests/unit tests/integration/test_api.py
tests/integration/test_workspace_api.py` → **996 passed / 12 failed**（仍是缺 `pypdfium2`
的 PDF 用例）。界面上的「记忆」面板仍未做——本轮只到接口。

**第九轮（同日）：界面「记忆」面板（前后端）**。目标是把上一轮只到接口的能力变成**看得见、
能收回**的界面，并验收。

后端补一条：`DELETE /api/v1/memory/long-term/{key}`（`doc/api.md` §5.22、ADR-036 §5）——
命中返回 204，命中不到返回 404 `MEMORY_ENTRY_NOT_FOUND`。**写入仍然不在这里**：一条记忆是
"使用者说出来的话"，只能通过在对话里说 `记住：…` 产生；而"在面板上看着某条、却要切到对话里
打一句 `忘记：` 才能收回"是把看见与处置分到两个地方，属于没做完的界面，所以列表与删除成对
出现在面板上。`long_term.forget()` 相应改为返回布尔值（删不存在的条目不再假装成功）。

前端：`config/MemoryPanel.tsx`（`MemoryBoundary` 纯 props 展示体 + `MemoryPanel` 容器）、
配置页新增第五个分区「记忆」（Brain 图标，排在「执行边界」之前——记忆是全局数据，不属于某个
会话或工作区，所以归配置页而不是工作台）；四种状态齐全（读取中 / 空态 / 失败可重试 / 有数据），
删除走行内确认（`InlineConfirm`），「已经不在了」按**错误码**当作"已为你刷新"而不是故障。
文案里写清记忆怎么产生（对话里 `记住：…`）以及删除只影响长期记忆、不动会话与消息。

验证：

- `test_api.py` +2（删除返回 204 且只删指定条目、删不存在返回 404 `MEMORY_ENTRY_NOT_FOUND`），
  全量 `pytest tests/unit tests/integration/test_api.py tests/integration/test_workspace_api.py`
  → **998 passed / 12 failed**（仍是缺 `pypdfium2` 的 PDF 用例）；
- `npm run build`（tsc + vite）通过；`config-smoke` **85/85**（分区数 4→5、面板逐条列出
  key/正文/更新时间与「忘记」入口、**空态没有任何写入控件**、失败态可重试、读取中不显示成
  "没有记忆"、按错误码处理"已经不在了"）、`workspace-smoke` **228/228**；
- **真实浏览器验收**（无头 Edge + CDP，独立 user-data-dir）：打开 `localhost:5173` →
  「工具与配置」→「记忆」→ 面板列出服务端真实存在的条目（key=称呼、正文=叫我张三）→
  点「忘记」→ 行内「确认」→ 面板回到空态 → 服务端 `GET /memory/long-term` 确认 `total=0`。
  三步全 PASS，链路是**界面 → HTTP → Redis**（不是静态渲染）。
  一处探针经验记下：面板**标题先渲染、数据后到**，第一版断言踩在"读取中…"那一刻、把面板
  误判成空列表——验证脚本要等**数据**到位再断言，别等外壳。

**第十轮（同日）：工作区去重收窄到会话内**。使用者反馈「一个文件夹可以被不同的项目重复登记，
不会互斥」，按需求理解是：**同一个目录应当允许被不同项目（会话）各自登记**。先做实验确认现状：
两个会话分别登记 `D:\测试`，**两边都返回 409**——当时是全局唯一（`ux_workspaces_path`），
第二个项目会被直接挡住。

改动：

- **模型**：`UniqueConstraint("path", name="ux_workspaces_path")` 换成
  `Index("ux_workspaces_session_path", "session_id", "path", unique=True)`——用唯一索引而不是
  `UniqueConstraint`，是为了让迁移能用 `CREATE UNIQUE INDEX IF NOT EXISTS` 幂等重建（同名）；
- **迁移**（`_REGISTRY_INDEX_MIGRATIONS`，与补列清单并列、同一条执行路径）：
  `ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS ux_workspaces_path` +
  `CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_session_path ON workspaces (session_id, path)`。
  老库上 `ux_workspaces_path` 是 `create_all` 建出来的**约束**（不是裸索引），所以先 drop
  constraint（会顺带删掉背后的索引），再建新索引；
- **服务层**：`checkpoint.find_workspace_by_path(path, session_id=…)` 增加会话维度，
  `create_workspace` 的查重改成本会话内；错误文案改成「本会话已登记该路径」。

为什么**不是**把查重整个去掉：真正要挡的是「同一个项目把同一个目录登记两遍」这种误操作；
「两个项目用同一个目录」是常态，不该被挡。所以去重范围收窄到会话内，跨会话放开。
每个会话各自持有自己的档位与配额，互不影响；`.trash`（软删除目录）落在同一个物理目录里——
这是共用的代价，写进 `doc/data-model.md` §3.3 了。

验证：

- 单元：`test_the_same_folder_can_be_registered_by_different_sessions`（两个会话各自登记同一路径
  都成功，且一个提档不影响另一个）、`test_duplicate_path_within_one_session_is_rejected`；
- 迁移护栏：`test_migration_statements_are_idempotent` 改为**按类别**断言幂等（补列 /
  建索引 / drop 旧约束三种写法各自成立），新增
  `test_migration_switches_workspace_uniqueness_to_per_session`——这两条抓到的正是"新语句不符合
  原有假设"这个真问题，不是单纯改数字；
- 全量 `pytest`（含 e2e）→ **1143 passed / 12 failed / 7 skipped**（12 条仍是缺 `pypdfium2`
  的 PDF 用例）；
- **真库迁移**：重启后端后查 `pg_constraint` / `pg_indexes`，`ux_workspaces_path` 已消失、
  `ux_workspaces_session_path (session_id, path)` 唯一索引在位；
- **真机语义**：A 登记 `D:\测试` → **201**，B 登记同一个文件夹 → **201**（改前是 409），
  A 再登记同一路径 → **409**。验证用的测试会话已删除（只留使用者原有的登记）。

**第十一轮（同日）：修「无法准确绑定工作区」——一个会话只绑定一个工作区**。使用者反馈
「无法准确绑定工作区」。先复现：同一会话先后登记 `D:\测试` 与 `C:\Users\zq\MacpWorkspace`，
库里留下**两条**，而执行侧 `workspace_source(session_id)` 取的是 `rows[0]`——**最早那条
`D:\测试`**，与界面上最后登记/选中的那个不一致。也就是说「界面选了哪个」与「Agent 实际用哪个」
根本不是同一件事：界面的选中只改本地状态，从不参与执行侧的判定。

根因不在界面，在模型：**一个会话可以挂多条登记**，而执行侧只能用一个，于是"用哪个"退化成
"哪条先建的"。这也与使用者的原始要求不符——他要的是**一个**文件夹（「选一个文件夹……
也只能在这个文件夹下面」）。所以把模型改成：**一个会话只绑定一个工作区，再登记＝改绑**。

改动：

- **模型**：`ux_workspaces_session_path (session_id, path)` → `ux_workspaces_session (session_id)`
  唯一索引（`session_id` 为 NULL 的"未绑定登记"不受限——PostgreSQL 唯一索引视 NULL 互不相同）；
- **迁移**：清掉前两版唯一结构（`DROP CONSTRAINT IF EXISTS ux_workspaces_path`、
  `DROP INDEX IF EXISTS ux_workspaces_session_path`），**先按会话收敛历史数据**（同一会话多条时
  保留 `created_at` 最新那条，即使用者最近一次选择；审批随工作区行级联删除），再
  `CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_session`，最后删掉被唯一索引取代的冗余索引
  `idx_workspaces_session`；
- **服务层**：`create_workspace` 带 `session_id` 时——同路径重复登记＝**幂等**（不重建那条登记，
  免得丢掉它的待批审批；但档位跟随这次选择，不静默忽略使用者的动作），换路径＝**改绑**（旧绑定
  在同一步解除，记 `workspace.rebound` 日志并带 `replaced_path`）；
- **界面**：文案说清「一个会话只绑定一个工作区，再登记就是改绑」，改绑成功后回执点名
  「原 <旧路径> 已解除」——**静默换掉等于让人以为两个目录都还挂着**；按 **id** 判"是不是换了一条"，
  不比较路径字符串（同一个目录有多种写法）。

验证：

- 单元：`test_the_same_folder_can_be_registered_by_different_sessions`（跨会话共用仍放开）、
  `test_registering_the_same_path_again_is_idempotent_but_keeps_the_new_mode`、
  `test_registering_another_folder_rebinds_the_session`——最后一条直接断言
  `workspace_source(session_id).root()` 与界面看到的那条绑定**一致**（这正是本轮修的缺陷本身）；
- 迁移护栏：`test_migration_statements_are_idempotent` 扩到四类写法（补列 / 建唯一索引 /
  `DROP ... IF EXISTS` / 收敛型 `DELETE`，最后一类显式点名而不是放宽成"什么都行"），新增
  `test_migration_switches_workspace_uniqueness_to_one_per_session`，并**钉住顺序**：
  收敛数据必须排在建唯一索引之前（顺序错了老库直接建不起来）；
- 全量 `pytest tests/unit tests/integration/test_api.py tests/integration/test_workspace_api.py`
  → **1001 passed / 12 failed**（仍是缺 `pypdfium2` 的 PDF 用例）；`npm run build` 通过；
  `workspace-smoke` **229/229**（新增一条：文案与改绑回执）、`config-smoke` **85/85**；
- **真库迁移**：重启后端后 `pg_indexes` 只剩 `workspaces_pkey` 与 `ux_workspaces_session`；
  那个双绑定的测试会话被收敛成 1 条（保留最新）；
- **真机语义与准确性**：同一会话登记 `D:\测试` 再改绑到 `C:\Users\zq\MacpWorkspace` → 两次都
  201、该会话绑定数 **1**、同路径再登记**幂等**（同一条 id）；`workspace_source` 实际使用的路径
  与界面看到的**完全一致**（修复前这里是最早那条 `D:\测试`）。测试会话已删除。

**第十二轮（同日）：启动入口收敛——`deploy/start.ps1` 默认就是本地服务形态**。使用者要求
「改一下启动脚本，启动时，会连带启动本地服务」。两条脚本其实是两种**互斥**形态（共用
3500/8000/5173），所以"两个都起"这条路不存在；能做的、也是想要的，是**一个入口**：

- `deploy/start.ps1` 不带参数 → **转调** `scripts/start_local.ps1`（本地服务形态）；
- `deploy/start.ps1 -Container` → 容器形态（原逻辑原样保留）；
- `deploy/start.ps1 -Stop` → 停本地那三个进程（容器那套由 compose 管）。

两处以前会真的咬人的地方顺带修掉：

1. **切形态撞端口**：`-Container` 之前不会收掉本地那套，直接撞 3500
   （`bind: Only one usage of each socket address` 那个报错就是这么来的）。现在切之前先
   `start_local.ps1 -Stop`——两个方向都由脚本自己收，不要求使用者手工腾端口。
2. **重复敲启动脚本会报错**：已经在跑时原本 `exit 1`，现在如实说一句"已在运行（PID …）"
   并成功返回；只有"半死"（上次被强杀）才先收干净再起。

依赖只起**对宿主后端真有用**的：Redis / PostgreSQL 必需，**新增 Jaeger**（宿主后端默认 trace
端点是 `localhost:4318/v1/traces`）；Prometheus 抓的是容器里的 `backend:8000`、`search-gateway`
与 `egress-proxy` 只在 `internal` 网——宿主进程按设计碰不到，起了也没有数据，所以本形态不起，
理由写进 `doc/deployment.md`（沿用本项目"不给假服务"的一贯口径）。

踩到并修掉一个真坑：给 `deploy/start.ps1` 加了**中文字符串**之后脚本直接语法报错——它原先的
中文只在**注释**里，所以没有 BOM 也一直正常；PowerShell 5.1 对无 BOM 脚本按 ANSI 读，字符串
错位会吃掉引号。补上 UTF-8 BOM 即恢复（`pick_work_dir.ps1` 头部记过同一个坑，这次是"加了中文
字符串的新场景"触发）。顺带把 `[int]$_` 少写 `$` 的笔误一起修了。

验证（**四种用法全部真机跑过**，不是只看语法）：

| 用法 | 结果 |
| --- | --- |
| `cd deploy; .\start.ps1` | 宿主后端 `127.0.0.1:8000` 200、前端 5173 200、**Jaeger 16686 200** |
| 再敲一次 | 「本地服务形态已在运行（PID …），未重复启动」，退出码 0 |
| `.\start.ps1 -Stop` | 三个进程（含子进程树）停掉，依赖容器保留 |
| `.\start.ps1 -Container` | 先收掉本地那套 → 容器栈起来，Frontend / Backend / Dapr Sidecar 三处健康检查全过，8000 空闲 |
| 再切回 `.\start.ps1` | 回到本地形态：8000 / 5173 / 16686 全 200，容器里的 backend / frontend / dapr-sidecar 已停 |

两个脚本都过了 PowerShell 解析器（0 语法错误）且都带 UTF-8 BOM。本轮只动脚本与文档，
未触碰应用代码。

**第十三轮（同日）：停止入口也收敛——`deploy/stop.ps1` 默认停本地服务形态**。使用者指出
「停止服务有停止脚本」。上一轮只把**启动**收敛成了 `deploy/start.ps1`，停止还散在
`start.ps1 -Stop` 与 `scripts/start_local.ps1 -Stop` 两处——启动与停止不对称，早晚会有一处漏改。

现在两侧对称：`stop.ps1` 不带参数＝停本地服务形态（三个宿主进程**含子进程树** + 它起的依赖容器），
`-Container`＝原来的 `docker compose down`；`start.ps1 -Stop` 保留但**内部转调** `stop.ps1`，
不另写一份实现。本地形态只 `stop` 依赖容器、不 `down`：容器与数据都留着，下次起得更快；
要连容器一起删就用 `-Container`。

三处踩到并修掉的问题（都是"看起来对、实际不对"那一类）：

1. **输出自相矛盾**：内层脚本会提示「依赖容器仍在运行」，而紧接着 `stop.ps1` 就把它们停了。
   滤掉那句，由 `stop.ps1` 统一说「依赖容器已停（容器与数据保留）」。
2. **过滤没生效**：内层用的是 `Write-Host`（**信息流**），只重定向 `2>&1` 抓不到，必须 `*>&1`。
3. **docker 的 stderr 被渲染成红字错误**：`docker compose stop` 把进度写到 stderr，PS 5.1 下
   看起来像"停止失败"。改用仓库既有的写法——`*> $null` 吞掉全部流 + 按退出码判定成败。

另外我自己的一份验证探针写错了（`try` 里根本没发请求，于是永远打印"仍在"），差点把"停成功了"
读成"没停"；改用**端口状态**复核才看清。这类探针错误已记在这里，提醒后来者：验证脚本本身也要
能被证伪。

验证（真机，四种用法）：

| 用法 | 结果 |
| --- | --- |
| `cd deploy; .\stop.ps1` | 三个宿主进程（含子进程树）+ 依赖容器全停；真实探针 8000 / 5173 / 16686 全部不可达 |
| 再敲一次 | 幂等：提示"没有本脚本记录过的进程"，依赖容器停用为空操作，退出码 0 |
| `.\start.ps1 -Container` → `.\stop.ps1 -Container` | 容器栈起来后 `docker compose down` 把容器与两个网络全部移除，`docker compose ps` 为空 |
| `.\start.ps1` | 回到本地形态：8000 / 5173 / 16686 全 200 |

三个脚本（`deploy/start.ps1`、`deploy/stop.ps1`、`scripts/start_local.ps1`）都过 PowerShell
解析器（0 语法错误）且都带 UTF-8 BOM。本轮只动脚本与文档，未触碰应用代码。

**前端容器与服务**：`npm run build` 通过（bundle 441.52 kB，未过告警阈值）；
`workspace-smoke` **210/210** 未受影响。
- **`doc/15` 只加了两条**：模块 3 的工作区与出网边界、第五节的三层边界表；该文件是事实源，
  其余表述未动。

## 5. 失败处理约定
- 任一用例失败：先复现，再定位，修复后将失败模式固化为新的测试或本文档约束；
- 对 Dapr/编排等共享行为，先写测试或同步补测试，不允许“看起来正确”代替；
- 每次里程碑结束时在报告记录：运行命令、通过数/失败数、失败原因。

# 部署方案

## 本地环境

1. 安装 Python 3.12 与 UV。
2. 执行 `uv sync` 安装依赖。
3. 执行 `dapr init` 初始化 Dapr 本地运行时。
4. Docker Hub 不可达时执行：

```bash
DAPR_DEFAULT_IMAGE_REGISTRY=ghcr dapr init --runtime-version 1.18.2
```

5. 运行测试：`uv run pytest`

## 一键部署

进入 `deploy` 目录执行：

```powershell
cd deploy
.\start.ps1
```

该脚本会构建后端与前端镜像，并启动以下服务：

| 服务 | 说明 |
| --- | --- |
| `frontend` | React + Vite Web UI，nginx 托管静态资源并反向代理 `/api` |
| `backend` | FastAPI 后端 |
| `dapr-sidecar` | Dapr Sidecar |
| `placement` | Dapr Placement |
| `scheduler` | Dapr Scheduler |
| `redis` | 状态存储与消息总线 |
| `postgres` | 会话持久化 |
| `jaeger` | 追踪可视化 |
| `prometheus` | 指标采集 |
| `search-gateway` | Agent 的网页搜索出口：把可达的 Bing RSS 转成 `web_search` 要的 JSON 契约（ADR-032）。**不发布宿主端口**，只服务同网络的 backend |
| `egress-proxy` | 出网策略的硬边界（ADR-034 §4）：HTTP 绝对 URI 转发 + HTTPS `CONNECT` 隧道，两种形态都先经同一套「解析 → 私网判定 → 钉扎」再转发。**不发布宿主端口**；内部服务由客户端直连（`NO_PROXY`），不进它 |

启动后访问：

- Web UI：http://localhost:5173
- 后端 API：http://localhost:5173/api/v1（**经前端反代**）
- 后端健康探针：http://localhost:5173/health

> **backend 不再发布宿主端口（ADR-034 §4）**。它只接 compose 的 `internal` 网络
> （`internal: true`：没有默认路由，容器连外部 DNS 都解析不了），唯一出口是同网络上的
> `egress-proxy`——这是「出网策略在网络层强制」的落点。代价就是宿主不能再直连 8000：
> 前端 nginx 反代 `/api/` 与 `/health`，`start.ps1` 的健康检查也走这条。
> 需要直连时用 `docker compose exec backend ...`，或把 backend 临时接回 `default` 网
> （那就同时放开了直连出网，等于放弃这层强制）。

### 换工作区根（原生文件夹对话框）

工作区根是**宿主目录**，换根要写 `deploy/.env` 再重建 backend（bind mount 在容器创建时固定）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/pick_work_dir.ps1
# 或者在选完之后自己执行：cd deploy; docker compose up -d backend
# 加 -Recreate 可以让脚本顺带重建
```

脚本会在宿主上弹系统文件夹对话框，并**拒绝**选中仓库目录（阶段 2 起沙箱对工作区可写，
而仓库里是平台自己的源码）。界面上的「选择文件夹并导入」是另一条路：它导入**副本**，
适合"把一批文件交给 Agent 处理"，不适合"让 Agent 直接改我本机那个目录"。

> **模型前置条件（ADR-014）**：默认提供方是 OpenAI 兼容 API（`AGENT_LLM_PROVIDER=openai`），
> 所以**部署本身不需要 Ollama**，但要先配好模型名与凭据，否则部署会成功、提交任务后
> Workflow 阶段 fail-fast 失败（缺 model 或凭据时模型构造直接抛错，不会静默回退到别的
> 提供方）。两种配法：
>
> 1. 在 `deploy/` 下建 `.env`（已在 `.gitignore` 中，不会入库）写
>    `AGENT_OPENAI_BASE_URL=` / `AGENT_OPENAI_API_KEY=` / `AGENT_OPENAI_MODEL=`，
>    `compose.yaml` 的 `${AGENT_OPENAI_*:-}` 会把它们注入 backend；
> 2. 先起栈，再到 Web 的「工具与配置」页写运行期覆盖
>    （`PUT /api/v1/config/provider`，见 `doc/api.md` §5.8），下一次任务即生效、无需重启。
>
> 回退本机 Ollama 时设 `AGENT_LLM_PROVIDER=ollama`，宿主机需运行 Ollama 且已拉取
> `AGENT_OLLAMA_MODEL`（默认 `qwen2.5-coder:7b`），容器通过 `AGENT_OLLAMA_BASE_URL`
> （默认 `http://host.docker.internal:11434`）访问它（见 ADR-007）。
>
> **容器内访问宿主机服务**：`AGENT_OPENAI_BASE_URL` 指向宿主上的网关时要写
> `http://host.docker.internal:<端口>/v1`；写 `127.0.0.1` 会指向容器自身。

停止服务：

```powershell
.\stop.ps1
```

## 演示与验收（D9-10，成员 D）

三个演示场景中由 D 负责的可复现部分。完整验收矩阵见 `doc/testing.md` §2.3，
本轮状态见 §4。

### E-05：一键部署全流程

```powershell
cd deploy
.\start.ps1     # 构建镜像 → up -d --wait → 三段健康检查 → 打印访问地址
.\stop.ps1      # 停止并移除容器
```

`start.ps1` 通过的标准是**同时满足**两条：脚本退出码为 `0`，且三段健康检查都打印
`is healthy`——

| 检查 | 地址 |
| --- | --- |
| Frontend | http://localhost:5173/ |
| Backend | http://localhost:5173/health（经前端反代；backend 只接 internal 网络） |
| Dapr Sidecar | http://localhost:3500/v1.0/metadata |

最后会打印 7 行访问地址（Web UI / Backend / Dapr / Jaeger / Prometheus / Redis /
PostgreSQL）。**只看「容器起来了」不算通过**：脚本中途异常时容器可能已经在运行。

`stop.ps1` 通过的标准是退出码 `0`，且
`docker ps -a --filter "name=multi-agent-collaboration-platform"` 为空。

> 两个脚本都必须容忍 `docker compose` 写到 stderr 的构建/停止进度。Windows
> PowerShell 5.1 在 `$ErrorActionPreference = "Stop"` 下会把原生命令的 stderr 当成
> 终止性错误：镜像构建成功、容器也起来了，脚本却以非零码退出——`start.ps1` 不做健康
> 检查也不打印地址，`stop.ps1` 对已经完成的停止报错。两处都已改为在该调用期间临时
> 切到 `Continue` 并用 `$LASTEXITCODE` 判定成败，与文件里 `docker info` /
> `compose version` / `compose config` 的既有写法一致。

### E-04：Web 会话管理

自动化部分（走 nginx 反代的真实 API，**不依赖模型**）：

```bash
MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s -k "frontend or web_ui_session"
```

覆盖：`frontend` 容器可访问且 SPA 入口引用的构建产物可取到（dist 没随镜像更新时
首页仍返回 200、页面却白屏）、nginx 把 `/api` 反代到 backend、工作台首屏与
「工具与配置」页用到的只读接口经反代可用（含 `availability=available` 与
「Provider 配置响应不含密钥」），以及「新建任务 → 暂停 → 暂停期提交被拒 → 恢复 →
回读」六步会话生命周期。

浏览器端仍需人工确认的部分（前端没有浏览器自动化用例，见 `doc/testing.md` §3.3）：

| 步骤 | 应看到 |
| --- | --- |
| 打开 http://localhost:5173 | 顶栏「API 已连接」，左下「运行时」显示在线 |
| 发一条任务 | 用户气泡出现；Agent 执行台展开三个阶段节点并随状态推进 |
| 展开「协作详情」 | 「工具调用链路」「本次任务 Token 与指标采样」两块；无记录时显示「暂无记录」而不是 0 |
| 点「任务记录」 | 「运行记录」显示当前会话最近一次执行；切到「历史会话」副路由可浏览并恢复全部会话（`doc/api.md` §5.13）。**跨会话的 workflow 级列表仍未接入**（没有 `GET /api/v1/workflows`） |
| 点「Agent 团队」 | 三张角色卡片，Provider/模型/温度取自生效配置 |
| 点「工具与配置」 | Provider 表单显示生效值与「凭据已配置 / 缺少凭据」；工具目录 4 项；全局指标采样非空 |
| 在输入框右侧切换「自动编排 / 固定三步」 | 按钮高亮随之改变；「自动编排」下任务执行台的节点数由计划决定，不再固定三个（ADR-019、`doc/orchestration.md` §2.5） |
| 点顶栏「暂停」 | 输入框禁用并提示「会话已暂停」；点「恢复」后输入框重新可用 |
| 点「＋ 新建任务」 | 对话清空，进入新会话 |

> 「发消息后跑通 collect → analyze → report 并生成报告」（E-01/E-02）不在上表：
> 它需要可用的模型凭据，步骤与预期见 `doc/testing.md` §2.3。

## 本地启动后端（PyCharm / 命令行）

容器里的 `backend` 由 `uv run python -m app.workflows.worker` 启动，入口就是
`app/workflows/worker.py` 的 `main()`。它做三件事：

1. `init_checkpoint_schema()` 建表；
2. 后台线程启动 Dapr Workflow runtime（等 sidecar 就绪，失败每 2 秒重试）；
3. `uvicorn` 托管 `app.api.main:app`，默认监听 `0.0.0.0:8000`。

> `app/api/main.py` 只是 ASGI 应用本身，不是进程入口。直接
> `uvicorn app.api.main:app` 能起 HTTP，但不会注册 Workflow 与活动，提交消息会在
> 调度阶段失败。

### PyCharm 运行配置

仓库自带共享运行配置 `.idea/runConfigurations/Backend.xml`（名为 **Backend**）：

| 配置项 | 值 |
| --- | --- |
| 运行方式 | Python → 模块 |
| 模块 | `app.workflows.worker` |
| 工作目录 | `$PROJECT_DIR$`（项目根，必须如此，否则 `app.*` 导入失败） |
| 解释器 | 项目解释器 `uv (Multi-Agent Collaboration Platform)` |
| 环境变量 | `PYTHONUNBUFFERED=1` |

曾用过的 `uvicorn` 运行配置（参数 `main:--host 0.0.0.0 ...`）可以删掉：仓库根目录没有
`main.py`，所以会报 `Could not import module "main"`。

### 本地运行需要的外部依赖

| 依赖 | 本地地址 | 来源 |
| --- | --- | --- |
| PostgreSQL | `localhost:5433` | `docker compose -f deploy/compose.yaml up -d postgres` |
| Redis | `localhost:6380` | `docker compose -f deploy/compose.yaml up -d redis` |
| Ollama | `localhost:11434` | 宿主机安装并 `ollama pull qwen2.5-coder:7b` |
| Dapr sidecar | `localhost:3500` / `localhost:50001` | `.\scripts\run_local_sidecar.ps1` |

默认值已对齐上面的端口（见 `app/config.py`、`app/core/storage.py`），无需改 `.env`。

### 推荐步骤

```powershell
# 1) 停掉容器里的 backend 与 dapr-sidecar：它们占用 8000/3500
cd deploy
docker compose stop backend dapr-sidecar

# 2) 只起基础设施
docker compose up -d redis postgres

# 3) 起本地 Dapr sidecar（独立窗口常驻）
cd ..
.\scripts\run_local_sidecar.ps1

# 4) 在 PyCharm 里运行 "Backend"（或等价地：uv run python -m app.workflows.worker）
```

`deploy/compose.yaml` 里的 `dapr-sidecar` 用 `--app-channel-address backend`，
只为容器内的 backend 服务，宿主机进程连不上它，所以第 3 步必须另起一个 sidecar；
脚本用 `deploy/dapr/components-local/`，其中 Redis 指向 `localhost:6380`
（容器版组件指向 compose 网络别名 `redis:6379`）。

不调试、只想单命令跑通时，可以合并 3、4 两步：

```powershell
dapr run --app-id backend --app-port 8000 --dapr-http-port 3500 --dapr-grpc-port 50001 `
  --resources-path deploy/dapr/components-local -- python -m app.workflows.worker
```

注意此时前端容器（依赖 backend 健康）不会启动；调试后端请直接访问
http://localhost:8000，或另开一个窗口跑前端的 `npm run dev`。

## 前端本地开发

前端联调也可以不经过容器，直接用 Vite 开发服务器启动：

```powershell
cd frontend
npm install
npm run dev
```

开发服务器监听 http://localhost:5173，并把 `/api` 代理到 http://localhost:8000，
因此需要先启动后端（推荐直接执行 `deploy/start.ps1`）。

## Dapr 组件

- State Store：Redis / PostgreSQL
- Pub/Sub：Redis
- Workflow：Dapr Workflow 引擎
- Service Invocation：Agent 服务间调用

组件文件位于 `deploy/dapr/components/`。

## 部署文件

- `deploy/compose.yaml`：Docker Compose 编排
- `deploy/docker/backend.Dockerfile`：后端镜像
- `deploy/docker/frontend.Dockerfile`：前端镜像（Node 构建 + nginx 托管）
- `deploy/docker/nginx.conf`：Web UI 静态托管与 `/api` 反向代理
- `deploy/dapr/components/`：Dapr State Store 与 Pub/Sub
- `deploy/prometheus/prometheus.yml`：Prometheus 抓取配置（`dapr-sidecar:9090` 与
  `backend:8000/metrics` 两个 job）
- `deploy/start.ps1` / `deploy/stop.ps1`：一键启动与停止

## 可观测数据

| 数据 | 地址 | 说明 |
| --- | --- | --- |
| Jaeger UI | http://localhost:16686 | 追踪浏览；后端按 `OBS_TRACING_ENDPOINT` 上报，容器内指向 `http://jaeger:4318/v1/traces` |
| Prometheus UI | http://localhost:9090 | 抓取 `backend:8000/metrics` 与 `dapr-sidecar:9090`；`/api/v1/metrics` 的采样表数据在 PostgreSQL，不在 Prometheus |
| 后端指标文本 | http://localhost:8000/metrics | 进程内 Prometheus 注册表，契约见 `doc/api.md` §5.6；不读数据库 |
| 指标采样表 | PostgreSQL `metrics` | 由 `app/core/checkpoint.py::MetricRecord` 经 `init_checkpoint_schema()` 在 backend 启动时建表（`doc/data-model.md` §3） |

> `metrics` 表缺失时观测采样只记日志并跳过，Prometheus 指标与追踪不受影响；
> 表建好后重启 backend 即可看到 `/api/v1/metrics` 由 `not_integrated` 变为 `available`。

## 环境变量

仓库根目录的 `.env_example` 是这份清单的可复制版本（`AGENT_` / `TOOL_` / `MCP_` /
`SANDBOX_` / `OBS_` 五个业务前缀，外加 `REDIS_URL`、`DATABASE_URL`；其中
`TOOL_` / `MCP_` / `SANDBOX_` / `OBS_` 四段由 2026-09-16 的 `31d9cef` 补齐）。
它是模板，不是运行时配置：应用只加载 `.env`
（`app/config.py` 的 `env_file`），`uv run pytest` 与 `deploy/` 都不读它。

| 变量 | 说明 |
| --- | --- |
| `AGENT_LLM_PROVIDER` | `openai`（默认，OpenAI 兼容 API）或 `ollama`（备用） |
| `AGENT_OPENAI_MODEL` | OpenAI 兼容 API 的模型名（默认提供方下必填） |
| `AGENT_OPENAI_BASE_URL` | 兼容端点地址，留空用官方端点 |
| `AGENT_OPENAI_API_KEY` | API 凭据（未设置时回退标准 `OPENAI_API_KEY`） |
| `AGENT_OLLAMA_BASE_URL` | Ollama 地址（`AGENT_LLM_PROVIDER=ollama` 时使用） |
| `AGENT_OLLAMA_MODEL` | Ollama 模型名 |
| `REDIS_URL` | Redis 连接 |
| `DATABASE_URL` | PostgreSQL 连接（也是只读 SQL 工具的默认数据源） |
| `LOG_LEVEL` | 应用行为日志级别，默认 `INFO`（见 ADR-010） |
| `MCP_TRANSPORT` | 工具注册表传输：`inprocess`（默认）/ `stdio` / `http`（见 ADR-012） |
| `MCP_SERVER_COMMAND` | `stdio` 传输时启动 MCP Server 的命令，默认当前解释器 |
| `MCP_SERVER_URL` | `http` 传输时的 MCP Server 地址 |
| `MCP_INCLUDE_REGISTERED_SERVERS` | 是否把登记并「发现」过的 MCP Server 工具并入 Agent 工具集与 `GET /api/v1/tools`；部署默认 `true`，代码默认 `false`（见 ADR-026） |
| `TOOL_SEARCH_ENDPOINT` | 网页搜索端点。代码默认 DuckDuckGo Instant Answer，**一键部署默认指向同网络的 `search-gateway`**（`http://search-gateway:8800/search`，ADR-032）；要换回官方端点或自建网关用这个变量覆盖 |
| `TOOL_SQL_DSN` | 只读 SQL 工具的 DSN，留空则用 `DATABASE_URL` |
| `SANDBOX_BACKEND` | 代码执行沙箱后端：`docker`（默认）/ `denied` |
| `SANDBOX_TIMEOUT_SECONDS` | 单次沙箱执行超时，默认 15 |
| `OBS_TRACING_ENDPOINT` | OTLP HTTP 追踪导出地址，默认 `http://localhost:4318/v1/traces`（Jaeger） |
| `OBS_TRACING_ENABLED` | 是否启用追踪导出，默认 `true` |
| `OBS_METRICS_SINK` | `metrics` 采样写入：`postgres`（默认）/ `none` |
| `OBS_SERVICE_NAME` | 追踪服务名，默认 `macp-backend` |
| `OBS_TRACING_SAMPLE_RATIO` | 采样比例，默认 `1.0` |
| `OBS_METRICS_ENABLED` | 是否采集指标，默认 `true` |
| `OBS_METRICS_FLUSH_SIZE` | 采样批量落库阈值，默认 `50` |

### 工作区与出网（ADR-033 / ADR-034）

工作区分成两个变量，别混：`WORKSPACE_HOST_ROOT` 是**宿主**上那个目录（compose 用它做
bind mount），`WORKSPACE_ROOT` 是**容器内**的挂载点（应用读的是它）。

| 变量 | 状态 | 说明 |
| --- | --- | --- |
| `WORKSPACE_HOST_ROOT` | 阶段 1 | 宿主工作根（compose 默认 `../workspaces`），挂进容器的 `/workspace`。**不得指向项目仓库、`deploy/` 或平台数据目录**——沙箱对它可写（ADR-033） |
| `WORKSPACE_IMPORT_MAX_FILES` / `WORKSPACE_IMPORT_MAX_FILE_BYTES` | 阶段 4 | 单次导入的文件数（200）与单文件字节上限（20 MB）；前端「选择文件夹并导入」分片上传，服务端兜底 |
| `WORKSPACE_ROOT` | 阶段 1 | 容器内的工作区根，默认 `/workspace`；宿主机直跑后端时改成宿主绝对路径 |
| `WORKSPACE_ENABLED` | 阶段 1 | 默认 `true`；设 `false` 时工作区接口返回 503 `WORKSPACE_DISABLED`，不静默降级 |
| `WORKSPACE_MAX_FILE_BYTES` | 阶段 1 | 单文件读取上限，默认 5 MB |
| `WORKSPACE_READ_MAX_CHARS` | 阶段 1 | 单次读取返回的字符上限，默认 20000 |
| `WORKSPACE_TREE_MAX_ENTRIES` / `WORKSPACE_TREE_MAX_DEPTH` | 阶段 1 | 目录树条目与深度上限（500 / 8） |
| `WORKSPACE_SCAN_LIMIT` | 阶段 1 | 用量扫描的条目上限（10000），超出时 `usage.truncated=true` |
| `WORKSPACE_MAX_TOTAL_BYTES` / `WORKSPACE_MAX_ENTRIES` | 阶段 2 | 目录总字节与条目配额；写入前校验，超限返回 409 `WORKSPACE_QUOTA_EXCEEDED` |
| `WORKSPACE_APPROVAL_TTL_SECONDS` | 阶段 3 | 审批有效期，默认 900 秒；超时未决策置 `expired`（不放行、不删记录） |
| `WORKSPACE_SANDBOX_MOUNT` | 阶段 4 | 沙箱挂载工作区的方式：`rw`（默认）/ `ro`（**审批才拦得住**：沙箱只读，写走文件工具）/ `none`（沙箱看不到用户文件） |
| `SANDBOX_UID` / `SANDBOX_GID` | 阶段 4 | 沙箱进程身份，代码默认 `nobody`；compose 默认 `0`——Docker Desktop 把 bind 显示为 uid 0，非 0 身份写不进工作区。Linux 宿主改成宿主 uid，或用 `WORKSPACE_SANDBOX_MOUNT=ro` |
| `SANDBOX_WORKSPACE_HOST_ROOT` | 阶段 4 | 仅当自动反查宿主路径失败时用：显式指定宿主侧工作区根（沙箱是兄弟容器，bind 来源必须是宿主路径） |
| `EGRESS_MODE` | 已实现 | `public_only`（默认，只放公网）或 `allowlist`（再要求域名命中白名单） |
| `EGRESS_ALLOW_HOSTS` / `EGRESS_DENY_HOSTS` | 已实现 | 域名白/黑名单，**黑名单优先**；只支持精确主机名与 `*.` 前缀通配，按 label 边界匹配 |
| `EGRESS_INTERNAL_HOSTS` | 已实现 | 平台内部依赖的**精确主机名**（Dapr、Redis、PostgreSQL、Jaeger、`search-gateway`），豁免私网判定 |
| `EGRESS_ALLOWED_PORTS` | 已实现 | 默认 `443`；compose 里放宽到 `443,8800`（search-gateway 自己的端口）。**端口与私网豁免是两件事** |
| `EGRESS_MODEL_EXEMPT` | 已实现 | 默认 `true`：模型流量豁免私网判定，Ollama（`host.docker.internal`）与内网自建网关继续可用；仍受 scheme/端口/域名黑名单约束 |
| `EGRESS_MAX_REDIRECTS` / `EGRESS_TIMEOUT_SECONDS` / `EGRESS_MAX_RESPONSE_BYTES` | 已实现 | 重定向跳数（5）、超时（8s）与响应体上限（1 MB） |
| `EGRESS_PROXY_URL` | 已实现 | 强制出网代理（compose 默认 `http://egress-proxy:8888`）。设置后应用侧不再本地解析/判私网（交给代理），但仍执行 scheme/端口/域名规则 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 已实现 | 给 MCP 与模型这类**自建连接的 SDK** 用（httpx `trust_env`）。`NO_PROXY` 必须排除内部服务与内网模型端点（compose 默认已含 Dapr/Redis/PG/Jaeger/search-gateway/egress-proxy 与 `host.docker.internal`） |
| `SANDBOX_EGRESS_PROXY_URL` | 已实现 | 沙箱联网时注入给沙箱的代理地址；配合 `SANDBOX_NETWORK_ENABLED=true` |
| `SANDBOX_EGRESS_NETWORK` | 已实现 | 沙箱联网时接入的**内部**网络名（compose 里 `internal: true`，即 `<项目名>_internal`）。为空时沙箱会用默认网络直连，日志会告警 |
| `EGRESS_NO_PROXY` | 已实现 | 应用侧策略自己的直连名单（内部服务与它同源）。**只表示不走代理，不表示放行** |
| `EGRESS_PROXY_INTERNAL_HOSTS` | 已实现 | 代理侧的私网放行名单，默认空。用于内网模型端点（Ollama / 自建网关）：它们在 internal 网络里不可直达，只能经代理 |

> **用 Ollama / 内网自建模型网关时要注意**（ADR-034 §3「甲」的口径在新拓扑下的落地）：
> `host.docker.internal` 在 internal 网络里**不可达**（实测 `Network is unreachable`），
> 所以内网模型端点必须经代理放行：把它的主机名写进 `EGRESS_PROXY_INTERNAL_HOSTS`、
> 从 `NO_PROXY` 里去掉它、并把它的端口加进 `EGRESS_ALLOWED_PORTS`（Ollama 是 11434）。
> 公网模型服务（默认路线）不受影响，直接经代理出去。

两条容易误读的点：`EGRESS_INTERNAL_HOSTS` 放行的是**列出的服务**，不是"整个内网"；
模型豁免放行的是**模型这一类调用**，不是"任何指向内网地址的请求"（ADR-034 §3）。

模型接入默认走 OpenAI 兼容 API（ADR-014）。`AGENT_OPENAI_*` 是**环境回退值**：
运行期可用 `PUT /api/v1/config/provider`（`doc/api.md` §5.8）写入覆盖，覆盖值落
PostgreSQL `provider_configs`（事实源）并镜像到 Redis `provider:config`。
Redis 不可用只影响镜像：写仍然成功、读回源 PostgreSQL，两处都不可用时回退上面的
环境变量，因此删掉 Redis 不会丢配置。

`deploy/compose.yaml` 的 backend 显式设置 `OBS_TRACING_ENDPOINT=http://jaeger:4318/v1/traces`
与 `OBS_METRICS_SINK=postgres`，避免容器内默认值 `localhost` 指不到 Jaeger。

> **安全边界（ADR-015）**：配置写接口（`PATCH /api/v1/config/agents/{agent_id}`、
> `PUT /api/v1/config/provider`）不再鉴权，任何能访问 API 的调用方都能改写模型端点与
> 凭据。`ADMIN_TOKEN` 已于 2026-09-15 取消。因此请把 API 限制在本机或可信内网
> （compose 默认只映射到宿主机端口，不要直接暴露到公网）；若将来需要公网或多租户，
> 应引入会话级身份或 API 网关，而不是静态令牌。

> 工具、沙箱与可观测的完整配置项见 `app/tools/config.py`、`app/sandbox/config.py`、
> `app/mcp/config.py`、`app/observability/config.py`（ADR-012）。
> `docker` 后端需要挂载宿主 Docker socket；`SANDBOX_BACKEND=denied` 时
> `code_execution` 工具会以「沙箱不可用」失败，不会退回宿主执行。

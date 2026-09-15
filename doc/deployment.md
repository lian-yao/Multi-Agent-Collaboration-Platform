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

启动后访问：

- Web UI：http://localhost:5173
- 后端 API：http://localhost:8000

> 前置条件：宿主机需要运行 Ollama 并已拉取 `qwen2.5-coder:7b`；容器通过
> `deploy/compose.yaml` 的 `AGENT_OLLAMA_BASE_URL`（默认
> `http://host.docker.internal:11434`）访问它。Ollama 未启动时部署本身仍然成功，
> 但提交任务后 Workflow 阶段会失败（见 ADR-007）。

停止服务：

```powershell
.\stop.ps1
```

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

| 变量 | 说明 |
| --- | --- |
| `AGENT_LLM_PROVIDER` | `ollama` 或 `openai` |
| `AGENT_OLLAMA_BASE_URL` | Ollama 地址 |
| `AGENT_OLLAMA_MODEL` | Ollama 模型名 |
| `AGENT_OPENAI_MODEL` | OpenAI 模型名 |
| `REDIS_URL` | Redis 连接 |
| `DATABASE_URL` | PostgreSQL 连接（也是只读 SQL 工具的默认数据源） |
| `LOG_LEVEL` | 应用行为日志级别，默认 `INFO`（见 ADR-010） |
| `MCP_TRANSPORT` | 工具注册表传输：`inprocess`（默认）/ `stdio` / `http`（见 ADR-012） |
| `MCP_SERVER_COMMAND` | `stdio` 传输时启动 MCP Server 的命令，默认当前解释器 |
| `MCP_SERVER_URL` | `http` 传输时的 MCP Server 地址 |
| `TOOL_SEARCH_ENDPOINT` | 网页搜索端点，默认 DuckDuckGo Instant Answer |
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
| `ADMIN_TOKEN` | 配置写入（`PATCH /api/v1/config/agents/{agent_id}`）所需的 `X-Admin-Token` 值；留空表示禁用该接口（返回 403，fail-closed，见 `doc/api.md` §5.7） |

`deploy/compose.yaml` 的 backend 显式设置 `OBS_TRACING_ENDPOINT=http://jaeger:4318/v1/traces`
与 `OBS_METRICS_SINK=postgres`，避免容器内默认值 `localhost` 指不到 Jaeger。

> `ADMIN_TOKEN` 未写入 `deploy/compose.yaml`：容器默认不开放配置写入，需要在
> `deploy/.env` 或 compose 覆盖里显式提供，避免示例部署带上一个可猜的默认口令。

> 工具、沙箱与可观测的完整配置项见 `app/tools/config.py`、`app/sandbox/config.py`、
> `app/mcp/config.py`、`app/observability/config.py`（ADR-012）。
> `docker` 后端需要挂载宿主 Docker socket；`SANDBOX_BACKEND=denied` 时
> `code_execution` 工具会以「沙箱不可用」失败，不会退回宿主执行。

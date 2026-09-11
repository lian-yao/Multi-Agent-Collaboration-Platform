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
- `deploy/prometheus/prometheus.yml`：Prometheus 抓取配置
- `deploy/start.ps1` / `deploy/stop.ps1`：一键启动与停止

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `AGENT_LLM_PROVIDER` | `ollama` 或 `openai` |
| `AGENT_OLLAMA_BASE_URL` | Ollama 地址 |
| `AGENT_OLLAMA_MODEL` | Ollama 模型名 |
| `AGENT_OPENAI_MODEL` | OpenAI 模型名 |
| `REDIS_URL` | Redis 连接 |
| `DATABASE_URL` | PostgreSQL 连接 |
| `LOG_LEVEL` | 应用行为日志级别，默认 `INFO`（见 ADR-010） |

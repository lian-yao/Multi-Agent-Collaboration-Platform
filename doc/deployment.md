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

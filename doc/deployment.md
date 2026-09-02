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

该脚本会构建后端镜像，并启动以下服务：

| 服务 | 说明 |
| --- | --- |
| `backend` | FastAPI 后端 |
| `dapr-sidecar` | Dapr Sidecar |
| `placement` | Dapr Placement |
| `scheduler` | Dapr Scheduler |
| `redis` | 状态存储与消息总线 |
| `postgres` | 会话持久化 |
| `jaeger` | 追踪可视化 |
| `prometheus` | 指标采集 |

停止服务：

```powershell
.\stop.ps1
```

## Dapr 组件

- State Store：Redis / PostgreSQL
- Pub/Sub：Redis
- Workflow：Dapr Workflow 引擎
- Service Invocation：Agent 服务间调用

组件文件位于 `deploy/dapr/components/`。

## 部署文件

- `deploy/compose.yaml`：Docker Compose 编排
- `deploy/docker/backend.Dockerfile`：后端镜像
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

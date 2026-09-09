# Multi-Agent Collaboration Platform

基于 LangGraph 与 Dapr 的 AI Native 多智能体协作平台，实现 Agent 间任务分配、协作执行与持久化编排，支持多模型接入（OpenAI/Claude/Ollama）、MCP 工具集成和全链路可观测。

## 当前状态

- LangGraph 单 Agent 原型已完成，包含基础图结构与测试。
- 编排框架固定为 LangGraph，不引入 CrewAI。
- Dapr 本地运行时已完成初始化，Redis、Placement、Scheduler、Zipkin 容器可用。
- D3-D4（里程碑 M2）已完成：固定三步 Dapr Workflow + State Management 持久化、
  断点续跑演练脚本、会话/记忆数据结构与 REST API（会话、消息、Workflow 暂停/恢复）。
- API 发起的 Workflow 执行完成后会由 durable 活动回写终态，轮询可观察到 completed。
- Python 环境由 UV 管理，实际环境以 `pyproject.toml + uv.lock` 为准。

## 技术栈

- 编排：LangGraph 1.2.11
- 运行时：Dapr 1.18.3 + Dapr Workflows
- API：FastAPI + Uvicorn
- 存储：Redis + PostgreSQL（SQLAlchemy）
- 工具：MCP
- 可观测：OpenTelemetry + Jaeger + Prometheus
- 前端（规划）：React + Vite + TailwindCSS
- 语言：Python 3.12、TypeScript

## 目录结构

```text
.
├── AGENTS.md                 # Agent 工作入口与硬性约束
├── app/                      # FastAPI 后端与编排层
├── frontend/                 # Web 可视化界面（规划）
├── tests/                    # 单元、集成、端到端测试
├── deploy/                   # Docker 与 Dapr 部署资源
├── scripts/                  # 本地开发脚本
├── examples/                 # 示例场景（规划）
├── doc/                      # 项目文档
│   ├── 15 AI Native多智能体协作平台.md
│   ├── requirements.txt
│   ├── architecture.md
│   ├── conventions.md
│   ├── api.md
│   ├── data-model.md
│   ├── testing.md
│   ├── deployment.md
│   ├── roadmap.md
│   └── decisions/
├── pyproject.toml            # 依赖与工具配置
└── uv.lock                   # 环境锁定文件
```

## 环境准备

```bash
uv sync
uv run pytest
```

Dapr 本地运行时初始化：

```bash
dapr init
```

Docker Hub 不可达时改用 GitHub Container Registry：

```bash
DAPR_DEFAULT_IMAGE_REGISTRY=ghcr dapr init --runtime-version 1.18.2
```

## 一键部署

需要本机已安装 Docker。进入 `deploy` 目录后执行：

```powershell
cd deploy
.\start.ps1
```

启动后访问：

- 后端 API：http://localhost:8000
- Dapr API：http://localhost:3500
- Jaeger：http://localhost:16686
- Prometheus：http://localhost:9090

停止服务：

```powershell
.\stop.ps1
```

## 文档入口

- 需求与完整设计：[doc/15 AI Native多智能体协作平台.md](<doc/15 AI Native多智能体协作平台.md>)
- 架构：[doc/architecture.md](doc/architecture.md)
- 工程规范：[doc/conventions.md](doc/conventions.md)
- API：[doc/api.md](doc/api.md)
- 数据模型：[doc/data-model.md](doc/data-model.md)
- 测试：[doc/testing.md](doc/testing.md)
- 部署：[doc/deployment.md](doc/deployment.md)
- 路线图：[doc/roadmap.md](doc/roadmap.md)
- 决策记录：[doc/decisions](doc/decisions)

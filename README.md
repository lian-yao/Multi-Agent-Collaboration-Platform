# Multi-Agent Collaboration Platform

基于 LangGraph 与 Dapr 的 AI Native 多智能体协作平台，实现 Agent 间任务分配、协作执行与持久化编排，支持多模型接入（OpenAI/Claude/Ollama）、MCP 工具集成和全链路可观测。

## 当前状态

- D1-D2（M1）：LangGraph 单 Agent 原型、基础图结构与测试完成；编排框架固定为 LangGraph，
  不引入 CrewAI。
- D3-D4（M2）：固定三步 Dapr Workflow + State Management 持久化、断点续跑演练脚本、
  会话/记忆数据结构与 REST API（会话、消息、Workflow 暂停/恢复）；API 发起的执行完成后
  由 durable 终态活动回写 `completed`/`failed`。
- D5-D6（M3）：多 Agent 流水线（信息收集 → 数据分析 → 报告生成）与可恢复子任务 Workflow；
  最终报告作为 `messages(role=assistant)` 回写，可经 `GET /messages` 读到。
- D7-D8（M4，已完成）：四个内置 MCP 工具与沙箱隔离、工具调用审计落库、
  OpenTelemetry + Jaeger + Prometheus 可观测；Web 工作台展示 Agent 团队、工具调用链路与
  Token 采样，模型 Provider 配置可在「工具与配置」页读写。
- D9-10（M5，进行中）：Web UI 与一键部署全流程（`deploy/start.ps1` / `stop.ps1`）已实跑验收；
  端到端用例、性能基线与未闭环项见 [doc/testing.md](doc/testing.md) §4。
- 后续追加（2026-09-17）：动态编排图**并列新增、默认关闭**（ADR-019，`dynamic` 由规划节点
  按任务分配角色，`static` 仍走固定三步）；执行边界只读可见（ADR-020）；聊天框支持引入
  图片与文档附件，走 `image_url` / 内联正文送进模型（ADR-021，`doc/api.md` §5.16）；
  首页引导卡改为「任务原型」并显式体现协作形态（ADR-022）。
- Python 环境由 UV 管理，实际环境以 `pyproject.toml + uv.lock` 为准。

## 技术栈

- 编排：LangGraph 1.2.11
- 运行时：Dapr 1.18.3 + Dapr Workflows
- API：FastAPI + Uvicorn
- 存储：Redis + PostgreSQL（SQLAlchemy）
- 工具：MCP
- 可观测：OpenTelemetry + Jaeger + Prometheus
- 前端：React + Vite + TypeScript
- 语言：Python 3.12、TypeScript

## 目录结构

```text
.
├── AGENTS.md                 # Agent 工作入口与硬性约束
├── app/                      # FastAPI 后端与编排层
├── frontend/                 # Web 可视化界面（React + Vite）
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
│   ├── orchestration.md
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

需要本机已安装 Docker。**默认模型提供方是 OpenAI 兼容 API（ADR-014），部署前请先在
`deploy/.env` 配好 `AGENT_OPENAI_MODEL` 与 `AGENT_OPENAI_API_KEY`（`.env` 已在
`.gitignore` 中），否则部署本身会成功、但提交任务后 Workflow 阶段会 fail-fast 失败。**
回退本机 Ollama 时设 `AGENT_LLM_PROVIDER=ollama`。详见 [doc/deployment.md](doc/deployment.md)。

```powershell
cd deploy
.\start.ps1
```

启动后访问：

- Web UI：http://localhost:5173
- 后端 API：http://localhost:8000
- Dapr API：http://localhost:3500
- Jaeger：http://localhost:16686
- Prometheus：http://localhost:9090

`start.ps1` 通过的标准是脚本退出码为 `0`，且 Frontend / Backend / Dapr Sidecar 三段健康
检查都打印 `is healthy`（只看「容器起来了」不算通过）。完整演示步骤与浏览器核对清单见
[doc/deployment.md](doc/deployment.md) 的「演示与验收」。

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

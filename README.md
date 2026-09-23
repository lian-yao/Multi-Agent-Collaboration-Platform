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
- D9-10（M5，已完成）：Web UI 与一键部署全流程（`deploy/start.ps1` / `stop.ps1`）已实跑验收，
  E-01～E-05 与性能基线见 [doc/testing.md](doc/testing.md) §4；浏览器端渲染仍是人工核对项。
- M5 之后（2026-09-16）：多 Provider / 多模型 / MCP Server 注册表与工作台视图三分
  （ADR-017、ADR-018）、Agent 角色目录与自定义角色 CRUD 已合入；Redis 会话/长期记忆实现与
  「外部 MCP Server 并入流水线工具注册表」也已合入，但记忆**尚未接入编排**。
  进度、缺口与实测结果见 [doc/roadmap.md](doc/roadmap.md) 与 [doc/testing.md](doc/testing.md) §4.4。
- 后续追加（2026-09-17）：动态编排图**并列新增、默认关闭**（ADR-019，`dynamic` 由规划节点
  按任务分配角色，`static` 仍走固定三步）；执行边界只读可见（ADR-020）；聊天框支持引入
  图片与文档附件，走 `image_url` / 内联正文送进模型（ADR-021，`doc/api.md` §5.16）；
  首页引导卡改为「任务原型」并显式体现协作形态（ADR-022）；**代码执行沙箱在部署环境真正可用**
  （ADR-023，安全代价与关闭方式见下文「一键部署 → 执行边界（沙箱）」）；附件原件一律留档，
  已发送的图片与文档都能下载原件（ADR-024）；**Agent 可按需读回本次会话的附件**
  （ADR-025：会话级只读工具，不放宽沙箱策略、不给沙箱挂卷）。
- 评测与度量（`doc/evals/`）：视觉能力评测集（10 例，答案机器可判）与
  「静态 vs 动态编排」净收益 A/B，都用真实模型跑，用法见两节各自的报告与
  [doc/testing.md](doc/testing.md) §4.5。
- 工作区沙箱与出网策略（2026-09-23，ADR-033 / ADR-034）：Agent 的文件业务限定在一个
  **工作区**内——登记、路径守卫（越界与符号链接逃逸一律拒绝）、目录树、按档位（默认
  `read_only`，可提档到 `workspace_write`）读写的六个工具，删除与覆盖**必须人工审批**
  （一次一授权、软删除进 `.trash/`）；`code_execution` 的沙箱按档位挂载本次会话的工作区
  （`rw`/`ro`）。出网单独一层策略：默认只放公网，内网/保留网段与云元数据地址一律拦截，
  支持域名白/黑名单，连接**钉在解析出的 IP** 上；模型流量与平台内部依赖按有限枚举豁免。
  前端已接：配置页新增「工作区」分区（登记、档位、目录树与配额），「执行边界」分区扩成
  沙箱 + 出网策略（只读）；对话流里出现**审批卡片**（覆盖/删除由人放行一次）。
  接口与界面约定见 [doc/api.md](doc/api.md) §5.19–§5.21 与 §7.1。
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
├── tests/                    # 单元、集成、端到端测试（fixtures/ 里是真实库生成的附件固件）
├── deploy/                   # Docker 与 Dapr 部署资源
├── scripts/                  # 本地开发脚本、附件固件生成、视觉与编排评测
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
│   ├── evals/                # 评测报告（视觉能力、编排净收益）
│   └── decisions/
├── pyproject.toml            # 依赖与工具配置
└── uv.lock                   # 环境锁定文件
```

## 环境准备

```bash
uv sync
uv run pytest
```

## 默认启动方式（本地服务形态，ADR-035）

```powershell
cd deploy
.\start.ps1
```

后端直跑在**本机**（绑 `127.0.0.1:8000`），前端是 Vite dev（`http://localhost:5173`），
Redis / PostgreSQL / Jaeger 走容器。这样工作区才能让你**当场选一个本机文件夹**——Agent 在那个
文件夹里读写，文件夹之外一律拒绝。

停止用 `cd deploy; .\stop.ps1`（与启动对称：默认停本地服务形态，容器形态加 `-Container`）。

容器形态仍然保留（适合部署与演示）：`cd deploy; .\start.ps1 -Container`，见
[doc/deployment.md](doc/deployment.md)。两种形态**共用 3500/8000/5173**，不能同时跑——
脚本会自己把另一套停掉，不用手工腾端口。

> 全量 `uv run pytest` 目前需要可达的 PostgreSQL（`localhost:5433`）与 Redis（`localhost:6380`），
> 且**集成层有 5 条已知失败**（测试隔离缺口，非功能缺陷）；单元与 E2E 层已自足。
> 用例规模与失败原因见 [doc/testing.md](doc/testing.md) §1 与 §4.4。

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
- 后端 API：http://localhost:5173/api/v1（经前端反代；backend 只接 internal 网络，
  不再发布宿主端口，见 ADR-034 §4）
- Dapr API：http://localhost:3500
- Jaeger：http://localhost:16686
- Prometheus：http://localhost:9090

`start.ps1` 通过的标准是脚本退出码为 `0`，且 Frontend / Backend / Dapr Sidecar 三段健康
检查都打印 `is healthy`（只看「容器起来了」不算通过）。完整演示步骤与浏览器核对清单见
[doc/deployment.md](doc/deployment.md) 的「演示与验收」。

### 执行边界（沙箱）

「代码执行」工具把模型生成的代码放进一次性容器里跑（`network_disabled` + `read_only` +
`user=nobody` + `cap_drop=ALL` + 内存/CPU/进程数上限，超时即 kill）。容器是 backend 通过
**宿主机的** Docker 守护进程创建的**兄弟容器**，因此 backend 挂载了 `/var/run/docker.sock`：

- **安全代价（务必知情）**：这等于把宿主机的 root 等价权限间接交给 backend 容器。要防的是
  模型生成的代码——它拿不到套接字（先过语言层策略，再进无权、只读、无网的一次性容器）；
  但一旦 **API 进程本身**被攻破，宿主机就跟着暴露。**不要把 8000 端口放到不可信网络。**
- 沙箱镜像由 `SANDBOX_IMAGE` 指定，compose 里指向**本项目自建镜像**：离线/内网拉不到
  `python:3.12-slim`，而 compose 一定会把自建镜像构建出来。能连外网时可以删掉那行，
  回落代码默认的官方 slim 镜像（更小、不含本项目代码）。
- 宿主机套接字不在默认路径时用 `SANDBOX_DOCKER_SOCKET` 覆盖（例如 Podman 的
  `/run/podman/podman.sock`）。
- 不想开：删掉 backend 的卷挂载并设 `SANDBOX_BACKEND=denied`。工具会以 `SandboxUnavailable`
  失败，**不会**降级成宿主进程执行。
- 当前是否可用、为什么不可用，在「工具与配置 → 执行边界」只读可见（ADR-020、`doc/api.md`
  §5.15）。完整决策与实机验收见 [ADR-023](doc/decisions/023-sandbox-deploy-docker-socket.md)。

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
- 评测报告：[doc/evals/](doc/evals)（视觉能力、静态 vs 动态编排净收益）
- 决策记录：[doc/decisions](doc/decisions)

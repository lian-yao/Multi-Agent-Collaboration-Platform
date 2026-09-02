# Multi-Agent Collaboration Platform Agent Harness

> Harness Engineering 是一种让 AI 智能体“可靠工作”的工程范式：通过构建约束、上下文管理、工具调用与反馈回路，使模型从不可控的智能变成可预测、可验证、可审计的工程系统。
> 本文件是 Agent 在仓库中的入口（`AGENTS.md`），是“目录页”而不是“百科全书”。详细设计一律以 `doc/` 为准。

## 1. 项目定位

本仓库实现 **AI Native 多智能体协作平台**：基于 LangGraph/CrewAI 与 Dapr Agents 构建 Agent 间任务分配、协作执行与持久化编排，支持多模型接入（OpenAI/Claude/Ollama）、MCP 工具集成和全链路可观测。

完整设计、模块说明与路线图以 [doc/15 AI Native多智能体协作平台.md](<doc/15 AI Native多智能体协作平台.md>) 为唯一事实源。

### 技术栈（以文档和依赖清单为准）

- 编排：LangGraph 1.2.11 / CrewAI 1.15.18 / Dapr Agents 1.0.6
- 运行时：Dapr 1.18.3 + Dapr Workflows
- API：FastAPI + Uvicorn
- 存储：Redis + PostgreSQL（SQLAlchemy）
- 工具：MCP
- 可观测：OpenTelemetry + Jaeger + Prometheus
- 前端（规划）：React + Vite + TailwindCSS
- 语言：Python 3.12、TypeScript

### 已定架构决策（不要自由发挥）

- 当前阶段先以 LangGraph 深度实现；CrewAI 与 Dapr Agents 因 OpenTelemetry 约束冲突暂缓引入，作为后续阶段补充。
- 优先集成持久化执行与状态管理，暂不深入 Service Invocation 与 Actor 模型。
- 模型优先 Ollama 本地小模型，OpenAI 作为备选。
- 代码执行等敏感工具必须沙箱隔离（Docker 或 Wasm）。

## 2. 硬性约束

1. `doc/` 下的文档是工作准则，不能自由发挥；实现必须能追溯回文档条款。
2. 发现文档错误时，先修改文档，再修改代码或继续操作。
3. 环境以 `pyproject.toml + uv.lock` 为准，使用 `uv sync` 安装；`doc/requirements.txt` 是给人看的依赖清单，每次更新依赖需同步维护三处。
4. 使用 UV 管理虚拟环境，不直接依赖系统 Python 环境。
5. 只修改任务范围内的文件，不顺手重构、不清理无关代码。
6. 不删除或回退用户已有的未提交改动。
7. 禁止破坏性 git 操作（`git reset --hard`、`git checkout --` 等），除非用户明确要求。
8. 不新增未在文档中出现的能力或抽象。

## 3. 上下文管理

渐进式披露：

- 本文件：始终可见，只放最关键规则。
- `doc/15 AI Native多智能体协作平台.md`：按需加载，系统架构与功能的唯一事实源。
- `doc/requirements.txt`：按需加载，依赖清单文档；实际环境以 `uv.lock` 为准。
- 代码内注释与模块说明：即时上下文，不重复写进本文件。

跨会话记忆：

- 重要决策必须落盘（文档、代码注释、commit message），不能只留在对话里。
- 上下文过长时，先产出交接产物：已完成、当前状态、下一步、已做决策及原因。
- 发现规则与现实不一致时，先修 `AGENTS.md` 或文档，再继续任务。

## 4. 工具调用与命令约束

- 搜索优先 `rg` / `rg --files`；读取文件时优先并行。
- 安装/同步依赖：`uv sync`
- 运行测试：`uv run pytest`
- 结构化解析优先使用成熟库/API，避免 ad-hoc 字符串处理。
- 敏感命令（删除、移动、数据库写入、部署）先确认目标路径与影响范围。
- 每个任务完成前必须给出可复现的验证结果，不能以“看起来正确”代替。

## 5. 反馈回路

定义完成（Definition of Done）：

1. 改动有文档依据，且与文档一致。
2. 相关测试通过，关键链路（Dapr 持久化、Workflow 恢复、MCP 工具）有覆盖。
3. `git diff` 只包含任务范围内的文件。
4. 若依赖有变化，`pyproject.toml`、`uv.lock`、`doc/requirements.txt` 已同步。
5. 提交按逻辑单元拆分，commit message 说明原因。

失败处理：复现 → 定位 → 修复 → 将失败模式固化为约束或测试，避免同型失败再次发生。

代码与文档漂移：发现漂移先更新文档，再修代码。

本文件只添加能防止真实失败的规则；不写从未触发过的规则。

## 6. 可验证、可审计

- 每个改动应能回溯到设计文档的对应模块（用户交互层、编排层、Dapr 运行时层、工具层、存储层、可观测性层）。
- 核心演示场景即验收基线：单 Agent 问答、多 Agent 协作流水线、故障恢复从断点续跑。
- 对 Dapr/编排等共享行为，先写测试再实现，或至少同步补测试。
- 向用户汇报时说明做了什么、验证了什么、哪些未完成，不隐藏失败。

## 7. 分工与升级路径

- 人类负责方向、验收与最终决策；Agent 在约束内自主执行。
- Agent 遇到文档矛盾、歧义或无法从仓库推断的内容时，停下来说明，而不是猜。
- 涉及文档变更、依赖变更、破坏性操作或超出任务范围的改动时，先报告再执行。

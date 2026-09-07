# ADR-004: 引入 Dapr Agents 1.0.6 并使用 OpenTelemetry 1.39.1 兼容组合

状态：已接受

## 背景

设计文档（`doc/15 AI Native多智能体协作平台.md`）将 Dapr Agents 定位为 Agent 生命周期管理与
持久化执行能力的一部分，与 LangGraph 编排共同构成平台核心。

早期评估时，Dapr Agents 依赖的 OpenTelemetry 语义约定与当时锁定的 OTel 1.44 系列不兼容，
因此一度“暂缓引入”。随后确认 Dapr Agents 1.0.6 使用 OpenTelemetry 1.39.1 兼容组合，
可以同时满足 Dapr Agents 与全链路可观测需求，团队决定正式引入。

## 决策

- 项目依赖引入 `dapr-agents==1.0.6`。
- `opentelemetry-sdk` 与 `opentelemetry-exporter-otlp-proto-http` 固定为 `1.39.1`。
- 同步维护 `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 三处。
- Dapr Agents 承担持久化执行、Agent 生命周期、记忆与工具生态等运行时能力；
  多 Agent 编排仍固定为 LangGraph，不引入 CrewAI。
- Dapr Agents 与 LangGraph 的具体集成方式以 `doc/dapr-integration.md` 为准，先做最小可运行
  POC 再固化实现细节。

## 影响

- OpenTelemetry 生态版本被 Dapr Agents 约束在 1.39.x，后续升级 OTel 必须先验证兼容性。
- 使用 Dapr Agents 后，Workflow 与状态读写能力应复用其提供的运行时，而不是各自实现一套。
- 依赖变更已在 `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 三处同步。
- 设计事实源 `doc/15 AI Native多智能体协作平台.md` 中若仍有“待引入/暂缓”表述，需同步修正。

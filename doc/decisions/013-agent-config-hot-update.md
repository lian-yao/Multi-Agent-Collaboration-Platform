# ADR-013: Agent 配置热更新（覆盖表 + 行内权限边界）

状态：已接受

## 背景

`doc/api.md` §6 把 `PATCH /api/v1/config/agents/{agent_id}` 列为规划接口，并规定
落地前必须补齐 Pydantic Schema、存储写入、权限边界、审计记录和测试；
`doc/testing.md` I-08「配置热更新：PATCH agent 后新执行使用新配置」属 M4 验收用例，
此前一直是「未实现」，是 D7-D8 唯一完全空缺的功能项。

现状约束：

- 模型与温度只来自 `AGENT_*` 环境配置（`app/config.py::AgentSettings`），
  `doc/api.md` §4.9 因此写明「配置修改需要重启对应进程」；
- API 进程与 Workflow Worker 是同一进程（`app/workflows/worker.py` 里 uvicorn 托管
  `app.api.main:app`），阶段活动在进程内执行，具备「改完立刻生效」的前提；
- 项目没有既有的鉴权/管理面约定，需要给出一个可测试、默认安全的最小边界。

## 决策

1. **只存覆盖，不存全量快照**：新增 `agent_configs` 表（`doc/data-model.md` §3），
   主键 `agent_id`，`model` / `temperature` 可空。行内 NULL 表示该字段回退环境配置，
   显式传 `null` 即删除覆盖。这样部署环境仍是配置的事实源，API 不改写环境语义。
2. **读时合并，写时校验**：`app/core/agent_config.py::resolve_agent_settings()` 把
   `AgentSettings` 与覆盖值合并成生效配置，API 的 `GET /agents`、`GET /agents/{id}`
   和阶段活动都走这一条路径，保证「看到的」与「跑起来的」一致。
3. **生效点在阶段活动执行时**：`app/workflows/pipeline.py::_run_stage_activity` 在
   执行阶段前按角色解析生效配置。Dapr 活动只执行一次、结果进历史，重放不会重新解析，
   因此不会引入新的不确定性；同时保证「PATCH 后新执行使用新配置」无需重启进程。
4. **provider 不在接口范围**：`llm_provider` 关联 base_url 与凭据，属部署配置。
   接口只允许改 `model` 与 `temperature`，避免通过 API 改变出网目标。
5. **权限边界 fail-closed**：写接口要求请求头 `X-Admin-Token` 等于环境变量
   `ADMIN_TOKEN`；`ADMIN_TOKEN` 未配置（空）时一律 `403 CONFIG_WRITE_FORBIDDEN`，
   令牌缺失或错误同样 `403`。默认拒绝比默认放行安全：忘记配置只是功能不可用，
   不会把「谁能改模型」暴露给任意调用方。
6. **审计用结构化日志 + 表内元数据**：成功写入产生
   `event=config.agent.updated`（`agent_id`、`actor`、`before`/`after`、`request_id`，
   沿用 ADR-010 的日志约定），表内落 `updated_by` / `updated_at`。
   不新建审计表：配置变更频次低、需要的是可检索的变更痕迹，而不是逐次调用级流水。
7. **读失败回退、写失败暴露**：`agent_configs` 读取异常时回退环境配置并记
   `config.read_fallback` 警告，列表接口与流水线都不因此失败；写入异常返回
   `503 DATA_SOURCE_UNAVAILABLE`，绝不返回「看起来成功」的响应。

## 影响

- `doc/testing.md` I-08 从「未实现」变为「通过」：单元用例覆盖合并与校验
  （`tests/unit/test_agent_config.py`），集成用例覆盖 403/404/422/503/200 与生效值
  （`tests/integration/test_config_api.py`）。
- `doc/api.md` §4.9 原先「配置修改需要重启对应进程」不再成立，已同步改为
  「覆盖 + 环境回退」，并在 §6 把 PATCH 从规划接口移除、§5.7 给出完整契约。
- **新增环境变量 `ADMIN_TOKEN`**：`doc/deployment.md` 已登记；未配置时接口按 403 拒绝，
  这是刻意的默认值，不是缺陷。
- **跨成员改动**：`agent_configs` 建表在新表模型 `app/core/checkpoint.py`
  （成员 B 的目录）、生效点在 `app/workflows/pipeline.py`（成员 B 的目录）、
  接口在 `app/api/main.py`（成员 D），合并逻辑在新模块 `app/core/agent_config.py`。
  本次由成员 D 按用户指示跨范围实现，归属已写进 `doc/data-model.md` 与本节。
- **前端不接入写接口**：令牌不能下发到浏览器，`doc/api.md` §7 已注明前端只展示生效值；
  Web 控制台的「主决策 Agent 选择」仍是预览交互。

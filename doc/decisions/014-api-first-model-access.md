# ADR-014: 模型接入改为 API 优先，Ollama 降为备用

状态：已接受

## 背景

D7-D8（M4）的缺口二是「真实流水线没有产生任何工具调用」：阶段活动默认调用本机
Ollama 的 `qwen2.5-coder:7b`，审计表 `tool_calls` 在真实运行中为 0 条。该缺口导致
M4 只能记为「代码完成、验收未闭环」（见 `doc/roadmap.md`）。

2026-09-15 按 A 的范围做了一次定位实验，结论是**该缺口无法用 Prompt 修复**：

| 尝试 | 结果 |
| --- | --- |
| 默认调用（Ollama 模板自带同款格式指令） | `tool_calls` 为空 |
| system 中文格式说明 | 同上 |
| system 格式说明 + 完整示例 | 同上 |
| 指令放到最后一条 user 消息 | 同上 |
| 要求第一个 token 必须是 `<tool_call>` | 同上 |
| assistant 前缀预填 `<tool_call>` | 同上 |
| 上下文内 few-shot（含一轮成功调用） | 同上 |
| 改用 Ollama 的 OpenAI 兼容端点 `/v1/chat/completions` | 同上 |

模型六次返回的 `content` 完全一致（`{"name": "calculator", "arguments": {...}}`），
既不报错也不产出包装标记。两个对照实验定位了原因：

- 要求模型原样重复 `<banana>HELLO</banana>` 时标签完好 → Ollama 不是无差别过滤标签；
- 流式抓取原始 token，输出自始至终没有 `<tool_call>` 字样 → 模型本身不吐这层包装。

即：Ollama 的对话模板要求把调用包在 `<tool_call></tool_call>` 内，解析器也只在该标记
存在时才填充结构化 `tool_calls`；而 `qwen2.5-coder` 属代码模型，倾向于直接输出 JSON。
工具定义注入本身是成功的（模型准确知道工具名与参数名），问题在模型输出层。

用户据此决定：**后续以 API 接入模型为主，Ollama 作为备用**。本 ADR 固化该决策。

## 决策

1. **默认提供方改为 API**：`AgentSettings.llm_provider` 默认值由 `ollama` 改为
   `openai`（OpenAI 兼容协议）；需要本地模型时显式设置 `AGENT_LLM_PROVIDER=ollama`
   回退，Ollama 接入代码保留，不删除。
2. **凭据由使用者配置，不进入仓库**：新增 `AGENT_OPENAI_API_KEY` 与
   `AGENT_OPENAI_BASE_URL` 作为环境回退。`base_url` 用于兼容第三方/中转端点，
   留空即官方端点；未设置 `AGENT_OPENAI_API_KEY` 时沿用 `OPENAI_API_KEY` 环境变量
   （由 `langchain-openai` 自身读取）。`.env` 已在 `.gitignore` 内，密钥不得写入
   文档、日志或接口响应。
3. **失败语义 fail-fast**：`llm_provider=openai` 时缺 `model` 或缺凭据一律
   `raise ValueError`，错误信息点名对应环境变量；**不静默回退到 Ollama**。
   「看起来成功」比失败更危险，与 I-08 的 fail-closed 取向一致。
4. **不新增依赖与抽象**：沿用既有 `langchain-openai==1.6.0` 与 `build_chat_model()`
   单一工厂，不引入新的提供方枚举或适配层。
5. **每角色模型解析不变**：模型名仍走「`agent_configs` 覆盖 + 环境回退」
   （ADR-013），本次只改提供方与凭据来源。
6. **运行期配置由系统保存，环境变量降为回退**：`provider` / `model` / `base_url` /
   `api_key` / `temperature` 可通过 `PUT /api/v1/config/provider`（`doc/api.md` §5.8）
   在线写入，**PostgreSQL 为事实源**（`provider_configs` 单行表，
   `doc/data-model.md` §3），**Redis 只存一份缓存镜像**（`provider:config`，
   `doc/data-model.md` §4）供跨进程快速读取。合并顺序为
   「存储配置 → 环境配置」，与 ADR-013 的「覆盖 + 环境回退」同构。
7. **PostgreSQL 是唯一事实源，Redis 是可丢失的缓存**：写路径先写 PostgreSQL 再写 Redis，Redis 写失败
   只记警告不影响写入成功；读路径先读 Redis，未命中回源 PostgreSQL 并回填，
   PostgreSQL 不可用时回退环境配置并记警告。删除 Redis 不丢配置，符合
   `doc/data-model.md` §4「可丢失，PostgreSQL 为事实源」的既有约定。
8. **密钥不回传、不落日志**：`GET /api/v1/config/provider` 与 `/providers` 只返回
   `api_key` 是否已配置（布尔/脱敏占位），不返回原值；审计日志只记录字段是否变化。
9. **密钥以明文存储在事实源与缓存中，访问边界由部署保证**（2026-09-15 用户确认）：
   静态加密不在本期范围；防线是数据库访问权限 + `ADMIN_TOKEN` 写接口边界，加上
   接口与日志的脱敏。后续如需静态加密，单独评估并另立 ADR。

## 影响

- **缺口二的验收方式改变**：从「本地 Ollama 产出工具调用」改为「API 模型下真实
  Workflow 产出工具调用并落 `tool_calls` 表」。在拿到可用密钥并跑通真实链路之前，
  M4 仍记为「代码完成、验收未闭环」，本 ADR 不构成验收通过的证据。
- **改动跨成员范围**：`app/config.py` 与 `app/orchestration/llm.py` 的模型接入属
  成员 C 的职责（`分工.md` §2「多模型接入」），本次由成员 A 按用户指示跨范围实现，
  `provider_configs` 建表落在 `app/core/checkpoint.py`、镜像与合并逻辑落在
  `app/core/provider_config.py`（成员 B 的存储范围），HTTP 契约落在
  `app/api/main.py`（成员 D 的接口范围），`deploy/compose.yaml`、
  `doc/deployment.md` 属成员 B 的部署范围。归属记在本节，合并前需与对应成员确认。
- **Redis 首次进入代码路径**：此前 `REDIS_URL` 只存在于配置与 compose，没有应用代码
  读取。本次的镜像读写是 Redis 的第一个代码消费方，连接失败必须降级而不阻断（决策 7）。
- **前端入口与令牌边界调整（2026-09-15）**：工作台「工具与配置」页新增 Provider 配置
  表单（`frontend/src/Inspection.tsx`），`doc/api.md` §7 原先「写接口没有前端入口、
  令牌不能下发到浏览器」的约束据此修订——令牌仍**不从服务端下发**，改由操作者手动输入并
  只保留在页面内存（不落浏览器存储、不入日志与构建产物）。这是有意的放宽：
  单机演示场景下操作者即管理员，若将来多租户使用，需要改为独立的会话/权限体系。
- **`/providers` 契约不变**：仍只返回 `configured` / `missing_model`，不返回密钥，
  也不探测模型可达性（`doc/api.md` §5.1）。因此该接口不反映凭据是否配置，
  凭据缺失在模型构造时以异常暴露。
- **ADR-007 部分被取代**：其「默认使用真实 Ollama 模型」的模型来源部分由本 ADR 取代；
  子 Workflow 封装、Fake 开关、失败与幂等语义继续有效。
- **事实源已同步（2026-09-15，经用户同意）**：`doc/15 AI Native多智能体协作平台.md`
  §风险应对与 `AGENTS.md` 的架构决策都已改为「API 优先、Ollama 备用」，不再存在
  与实际实现相反的表述。

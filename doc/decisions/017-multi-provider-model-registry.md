# ADR-017：多 Provider / 多模型注册表与 MCP Server 注册表

- 状态：已接受
- 日期：2026-09-15
- 相关：ADR-013（Agent 配置热更新）、ADR-014（API 优先的模型接入）、ADR-009（MCP 工具集成）、ADR-015（取消配置写入令牌）
- 参考：`obsidian-yolo` 的 `providers` / `chatModels` / `customParameters` / `mcpServers` 配置模型

## 背景

ADR-014 落地后，模型接入层是**单套全局配置**：

- `provider_configs` 是单行表（`id='default'`），只能保存一个 provider + 一个 model；
- `ALLOWED_PROVIDERS = ("openai", "ollama")`，无法接入 DeepSeek / 通义 / Moonshot / OpenRouter 等
  实际使用的 OpenAI 兼容网关；
- `agent_configs` 只能覆盖 `model` 与 `temperature`，无法表达 `top_p`、`max_tokens`、
  推理模式、自定义请求参数等**模型特化调参**；
- 模型中转端点（如自建 new-api 网关）通常一次暴露几十上百个模型，逐个手工录入不可行；
- MCP 只有全局 `MCP_*` 环境变量，无法登记多个 Server，工具目录也没有按 Server 分组，
  前端把每个工具的完整 JSON Schema 平铺展示，卡片信息冗杂。

参考项目 `obsidian-yolo` 的配置层已经解决了同一组问题，其做法是：
配置由**数组**构成（`providers: LLMProvider[]`、`chatModels: ChatModel[]`），
条目自带 `presetType` / `apiType` 以区分协议族，模型条目承载特化调参，
批量导入直接调用 Provider 的 `GET /models` 后多选落库。

本项目采纳同一套模型，但保持既有 REST 契约向后兼容。

## 决策

### 1. 新增三张注册表，保留两张单行/覆盖表

| 表 | 角色 |
| --- | --- |
| `llm_providers` | Provider 注册表（多行）：协议族、端点、凭据、自定义请求头 |
| `llm_models` | 模型注册表（多行）：属于某个 Provider，承载特化调参 |
| `mcp_server_registry` | MCP Server 注册表（多行）：传输方式、启动参数、工具级选项与发现缓存 |
| `provider_configs` | **保留**，语义收窄为「默认路由」：新增 `default_llm_model_id` 指向默认模型 |
| `agent_configs` | **保留**，扩展 `llm_model_id` / `top_p` / `max_output_tokens` / `reasoning_type` |

保留 `provider_configs` 的原因：§5.8 已经被前端使用、被测试覆盖，且「没有注册表也能跑」
是本地演示的重要路径。它的 `provider` / `model` / `base_url` / `api_key` / `temperature`
五列语义不变，仍作为**环境配置之上的直接覆盖层**。

### 2. 模型解析优先级（低 → 高）

```
AGENT_* 环境配置
  → provider_configs 的默认路由（default_llm_model_id 指向的 llm_models 行，含其 Provider 与特化参数）
  → provider_configs 的 legacy 五列覆盖（provider / model / base_url / api_key / temperature）
  → agent_configs 的角色覆盖（llm_model_id，其次 model / temperature / top_p / max_output_tokens / reasoning_type）
```

高优先级层里**未提供的字段**继续沿用低优先级层的值（逐字段回退），与 ADR-013/014 一致。
`agent_configs.llm_model_id` 一旦设置，即用该模型所属 Provider 的端点与凭据重建连接参数，
再用同行的 `model` / `temperature` / `top_p` / `max_output_tokens` / `reasoning_type` 覆盖。

### 3. `api_key` 只写不回读

沿用 ADR-014：注册表读取接口只返回 `api_key_configured: bool`，日志与响应都不含原值。
`llm_providers.api_key` 明文存储，访问边界由数据库权限与部署网络保证（ADR-015）。

### 4. 批量引入模型走「服务端发现 + 客户端多选」

- 发现：`GET /api/v1/config/providers/{id}/models/discover` 由**服务端**按 `api_type` 拉取
  远端模型清单（浏览器直连会撞 CORS 并泄露凭据）。
- 落库：`POST /api/v1/config/models/batch` 一次写入多条，`(provider_id, model)` 唯一约束 +
  `ON CONFLICT DO NOTHING`，天然幂等；已存在的模型计入 `skipped` 而不是报错。
- 批量导入只写 `model` / `name` / 默认 `enabled=true`，**不**批量写特化参数：
  参数在导入后按模型单独调整（与参考项目「批量添加使用默认参数，可在添加后单独调整」一致）。

### 5. 凭据写入策略：`api_key` 空串 = 不修改

`PATCH` 与 `PUT` 一律用 `UNSET`（字段缺省）/ `None`（显式清除）区分，
`api_key` 传空串视为「不改动」，避免表单重提交把已配置的密钥清空。

### 6. MCP 工具目录按 Server 分组，Schema 收进折叠区

新增 `GET /api/v1/config/mcp/tools`，返回按 Server 分组的紧凑条目
（`server_id` / `name` / `description` / `available` / `enabled`），**不**内联 `input_schema`。
需要 Schema 时走原有 `GET /api/v1/tools`（含 `input_schema`，分页）。
`mcp_server_registry.discovered` 缓存最近一次发现结果，使目录是**配置的函数**而非连接状态的函数：
Server 离线时工具仍出现在目录里，调用失败报连接错误而不是「无此工具」。

### 7. 不引入新运行时依赖

远端模型发现用标准库 `urllib.request`，并把「取 JSON」做成可注入函数以便测试。
原因：`httpx` 目前只在 dev 依赖组；新增运行时依赖需要同步改三处清单（`pyproject.toml`、
`uv.lock`、`doc/requirements.txt`），收益不足。

## 后果

正面：

- 一次配置多个 Provider、一次导入多批模型，中转网关场景可用；
- 每个模型独立调参，Agent 可指向具体模型条目；
- MCP 可登记多个 Server，工具卡片信息量可控。

代价与约束：

- 配置层从 2 张表扩到 5 张表，`resolve_*` 的合并链变长，必须靠单测锁住逐字段回退语义；
- 发现接口会向用户填写的 `base_url` 发起出站请求，属于 SSRF 面；缓解方式是仅允许
  `http(s)`、不带凭据转发、限制响应体积与超时，且**只在用户显式点击时**触发；
- `provider_configs` 与 `llm_models` 存在两套「默认模型」表达，靠 §2 的优先级规则消歧，
  文档必须与之同步。

## 未采纳方案

- **把 `provider_configs` 直接改造成多行表**：会破坏 §5.8 与既有测试，且本地演示要求
  「不配注册表也能跑」。
- **前端直连 Provider 拉模型列表**：CORS 不可控，且会把 `api_key` 暴露给浏览器网络层。
- **复用库里已存在的空表 `providers` / `mcp_servers`**：这两张表是早期实验残留，列定义与
  本设计不符（`providers` 只有单列 `model`、`mcp_servers` 无 headers/tool_options），
  且不在任何 ORM 定义中。改列名需要手写迁移，收益低于新增表。**它们保持空置不删**。

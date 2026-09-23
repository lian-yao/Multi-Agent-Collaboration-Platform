# ADR-037：搜索出口支持火山引擎豆包搜索（按量付费 key）

- 状态：已接受
- 日期：2026-09-23
- 相关：ADR-032（部署侧常驻搜索网关，替代连不通的 DuckDuckGo 端点）、ADR-034（出网策略）、`app/tools/search.py`、`app/security/egress.py`

## 背景

`web_search` 的默认端点 `https://api.duckduckgo.com/` 在开发网络里 TCP 443 直接超时
（ADR-032 已实测记录）。ADR-032 的退路是**部署侧常驻一个搜索网关**（`scripts/search_gateway.py`，
Bing RSS → DuckDuckGo 契约），compose 形态默认指向它。

但**本地服务形态（ADR-035）没有接这条线**：`scripts/start_local.ps1` 只起 daprd / 后端 / 前端，
既不起网关、也不设 `TOOL_SEARCH_ENDPOINT`；`.env` 里也没有这一项。于是本地形态回落代码默认值，
每次 `web_search` 都要 8s × 4 次重试才失败（ADR-032 记的「live 套件 9–10 分钟耗时主要来源」）。

使用者现在改用**火山引擎豆包搜索（Custom，按量付费 API key）**作为搜索出口。实测契约：

| 项 | 实测结果 |
| --- | --- |
| 端点 | `POST https://open.feedcoopapi.com/search_api/web_search` |
| 鉴权 | `Authorization: Bearer <API key>`，`Content-Type: application/json` |
| 请求体 | `{"Query": "...", "SearchType": "web", "Count": 3}`（**PascalCase**；`Count` 生效） |
| 响应 | `Result.WebResults[]`，每条含 `Title` / `Url` / `Snippet` / `Summary` / `Content` / `PublishTime` … |
| 业务错误 | HTTP 200 + `ResponseMetadata.Error.{Code,Message}`（如 10400 缺 query、10402 search type 非法、鉴权失败） |
| 网络可达性 | 本机 200（中文查询同样有结果），公网 443，`public_only` 出网策略默认放行 |

## 决策

1. **新增 provider 开关，而不是换掉解析契约**：`TOOL_SEARCH_PROVIDER` 取 `duckduckgo`
   （默认，保持 ADR-032 的网关/DuckDuckGo 契约不变）或 `volcengine`（豆包搜索 Custom）。
   两条出口的解析各自独立，`WebSearchTool` 的对外结果形状（`{title,url,snippet}`）不变。
2. **凭证只从环境 / `.env` 读**：`TOOL_SEARCH_API_KEY`。空 key 且 provider=volcengine 时**直接报
   配置错**（非 retryable），不发出无鉴权请求；key 不进仓库、不进日志（出网日志只记 host/port/purpose）。
3. **端点按 provider 给默认值**：provider=volcengine 且未显式给 `TOOL_SEARCH_ENDPOINT` 时，
   自动用豆包端点；显式给了就尊重显式值（便于换自建/代理）。
4. **仍走出网策略**：豆包是 POST，`app/security/egress.py` 原本只有 GET，这里补 POST——
   判定/钉扎/端口/域名/私网/重定向上限/响应体上限全部沿用同一套（差别只在方法与请求头）。
   重定向按标准语义处理：`307/308` 保留方法与请求体，`301/302/303` 降级为 GET 并丢掉请求体。
5. **失败语义分清**：网络层故障（超时、不可达、5xx、429）保持 `retryable=True`；凭证无效
   （401/403）、请求体业务错误（`ResponseMetadata.Error`）、provider/key 配置缺失一律
   `retryable=False`——重试只会重复失败，且能省掉 4 次无谓重试。
6. **与 ADR-032 并存**：compose 形态的常驻网关继续可用（provider 仍可留 `duckduckgo` 并把端点指向
   网关）；本地形态用 `volcengine` 就不需要额外进程与 `EGRESS_INTERNAL_HOSTS`/端口白名单，
   正好补上「本地形态没有搜索网关」这个缺口。

## 备选方案

- **给 `scripts/search_gateway.py` 加一个豆包上游**：能保持「工具契约不动」的一致性，但本地形态
  仍然要额外起一个进程、还要把 `127.0.0.1:8800` 加进 `EGRESS_INTERNAL_HOSTS` 与端口白名单
  （实测只设 `TOOL_SEARCH_ENDPOINT` 会被出网策略按端口拒掉）。对使用者的实际形态来说步骤更多、
  失败面更大，故不选为默认路径。
- **只在 `.env` 里把端点指到豆包、不改代码**：不可行——请求方法是 POST、字段名是 PascalCase、
  响应是 `Result.WebResults`，与 DuckDuckGo 契约完全不同。
- **引入第三方 SDK**：会新增依赖（`pyproject.toml` / `uv.lock` / `doc/requirements.txt` 三处同步），
  而这里只需要一个 POST + 字段映射，用标准库足够。

## 代价（如实记录）

- **多了一条与外部付费服务耦合的路径**：配额、计费、可用性都不由本项目控制；豆包侧字段若变动，
  解析会显式失败（`retryable=False`），不会假装「搜到了但没内容」。
- **按量付费 key 落在 `.env`**（该文件已在 `.gitignore` 里，不入库）；但这属于长期凭证，
  轮换与最小配额需要使用者自己管。
- **出网层从「只有 GET」变成「GET + POST」**：攻击面略有扩大，因此仍强制走同一套判定与钉扎，
  并禁止调用方覆盖 `Host` / `User-Agent` / `Content-Length`。

## 影响

- `app/tools/config.py`：新增 `search_provider`、`search_api_key`，端点默认值随 provider 推导；
- `app/tools/search.py`：按 provider 分派，新增豆包响应解析与错误映射；
- `app/security/egress.py`：新增 `http_post`（GET 路径行为不变）；
- `scripts/egress_proxy.py`：补 `do_POST` 明文转发（compose 形态经代理时同样可用）；
- `.env_example` 与 `doc/deployment.md`：补两个环境变量与端点说明；
- 测试：`tests/unit/test_search_tool_volcengine.py`（解析 / 错误映射 / 缺 key）、
  `tests/unit/test_egress_post.py`（POST 透传、重定向降级、禁止覆盖关键请求头）。

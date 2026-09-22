# ADR-032：部署侧常驻搜索网关，替代连不通的 DuckDuckGo 端点

- 状态：已接受
- 日期：2026-09-22
- 相关：ADR-012（内置工具、沙箱与可观测）、`app/tools/search.py`、`scripts/search_gateway.py`

## 背景

`web_search` 的实现（`app/tools/search.py`）默认打 `https://api.duckduckgo.com/`
的 Instant Answer 接口，`TOOL_SEARCH_ENDPOINT` 可以覆盖。实测本项目的开发网络：

| 目标 | 结果 |
| --- | --- |
| 宿主机 → `api.duckduckgo.com:443` | TCP 连接超时（DNS 解析到 103.214.168.106） |
| backend 容器 → 同一地址 | `ToolExecutionError: 搜索服务不可达（timed out）` |
| backend 容器 → `cn.bing.com` | 200，RSS 返回 10 条结果 |

也就是说容器有外网，只有这个域名被阻断。后果不只是「搜不到」：`ToolExecutionError`
被判定为可重试，一次搜索要 8s × 4 次尝试后才失败，`doc/testing.md` §4.4 里
「live 套件 9–10 分钟耗时主要来源」记的就是这件事。

`.env_example` 已经写明退路——「指向一个返回同一 JSON 契约（`Abstract` /
`RelatedTopics`）的自建网关」——但仓库里没有可用的网关，等于这条退路只是纸面承诺。

另外，即使网络可达，DuckDuckGo 的 Instant Answer **不是通用搜索引擎**：它只返回实体
摘要，中文查询绝大多数情况下 `AbstractText` 与 `RelatedTopics` 都为空，工具不报错、
而是返回 `results: []`，Agent 表现为「搜了但没资料」。

## 决策

**用一个常驻的本地网关服务承接 `web_search` 的外呼，并在部署侧把它设为默认出口。**

1. **网关就是 `scripts/search_gateway.py`**：纯标准库，单端点 `GET /search?q=...`，
   把 Bing 的 RSS 输出（`?format=rss`，免密钥、国内网络可直连）映射成工具消费的
   DuckDuckGo 契约。不新增依赖，`pyproject.toml` / `uv.lock` / `doc/requirements.txt`
   无需同步；`app/tools` 一行不改（契约不变，改的只是端点指向）。
2. **一键部署里它是一个 compose 服务**（`search-gateway`），与 backend 共用同一个
   镜像：`scripts/` 被 `deploy/docker/backend.Dockerfile` 一并 COPY 进去，因此不必为
   一个只有标准库依赖的脚本再拉一个基础镜像。
3. **不发布宿主端口**：只有同 compose 网络的 backend 需要它，暴露到宿主机只会多一个
   入口。
4. **backend 默认指向它**：`TOOL_SEARCH_ENDPOINT: ${TOOL_SEARCH_ENDPOINT:-http://search-gateway:8800/search}`。
   代码默认值保持 DuckDuckGo（`app/tools/config.py`），换部署方式时行为不变。
5. **不加 `depends_on`**：搜索是可选能力，网关起不来时 backend 仍应正常启动、任务照跑
   （工具失败按 `retryable` 重试后降级），不能让一个外呼组件的健康状态决定编排服务能不能起。
6. **`/health` 不打上游**：健康检查只证明「网关进程在监听」。上游可用性交给工具自己的
   重试与日志，否则 Bing 抖一下就会把容器标成 unhealthy，进而拖住 `docker compose --wait`。

## 备选方案

- **给容器配 HTTP 代理**：改动最小，但要求使用者已有可用代理，且 `urllib` 走
  `HTTPS_PROXY` 会把模型调用一起带上（国内网关还得逐个加 `NO_PROXY`），把「搜索不通」
  换成更难排的「模型请求也走代理」。未采纳为默认。
- **在 `app/tools/search.py` 里直接换成 Bing/Serper/Tavily 解析**：改动最大，且后者要
  API key；还会让「工具实现」与「外部服务可用性」耦得更紧。网关方案下工具契约不动。
- **把网关做成独立镜像**：更干净，但要新增基础镜像拉取（本环境 Docker Hub 不可达），
  对只有标准库的一百多行脚本不划算。
- **`discover` 之外再加「启用即自动发现」式的自动探测**：与本 ADR 无关，不引入。

## 代价（如实记录）

- **上游是 Bing 的 RSS 输出**，不是受支持的搜索 API：格式变动、限流、结果质量都不受本
  项目控制；网关对非 RSS 响应显式回 502（不伪装成「零条结果」），因此失败可见、但要换
  上游得改 `--upstream`。
- **Bing RSS 的版权声明**：其返回内容注明「除非出于个人的非商业用途在 RSS 聚合器中呈现
  必应结果」。**商用部署请改用有相应授权的搜索 API**（届时只需把 `TOOL_SEARCH_ENDPOINT`
  指向符合契约的服务，或在网关里换 `--upstream`）。
- **多一个容器**：一键部署从 10 个服务变 11 个；网关本身无状态、无卷、无端口。
- **中文标题里的 `" - "` 会被替换成 `" – "`**：这是为了绕开 `_parse_results` 用
  `partition(" - ")` 从 `RelatedTopics[].Text` 还原标题，否则标题会被截断。属于显示层
  的字符替换，会在文档化的位置发生。

## 影响

- `scripts/search_gateway.py`：新增 `GET /health`（不打上游），供 compose healthcheck 用；
- `deploy/compose.yaml`：新增 `search-gateway` 服务，backend 环境新增
  `TOOL_SEARCH_ENDPOINT` 默认值；
- `deploy/docker/backend.Dockerfile`：`COPY scripts ./scripts`；
- `doc/deployment.md`：服务表与环境变量表同步；
- `.env_example`：`TOOL_SEARCH_ENDPOINT` 说明改为「一键部署默认已指向网关」；
- 测试：`tests/unit/test_search_gateway.py`（含 `/health` 不打上游的断言）；
- 验收：在部署形态下用真 `WebSearchTool` 在 backend 容器内直跑，返回真实 Bing 结果。

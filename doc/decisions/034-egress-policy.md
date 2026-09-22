# ADR-034：出网策略（平台内部依赖与 Agent 出网分开管）

- 状态：已接受（**设计**；实现待排期）
- 日期：2026-09-23
- 相关：ADR-012（内置工具与沙箱）、ADR-014（API 优先的模型接入）、ADR-023（部署侧沙箱）、
  ADR-032（部署侧搜索网关）、ADR-033（工作区沙箱）

## 背景

需求是「禁止 Agent 访问内网地址（`127.0.0.1`、`192.168.x.x`、`10.x.x.x`、`172.16-31`），
阻止扫描本机局域网服务，只允许访问公网域名；白/黑名单与文件沙箱分开做一层」。

现状是出网**没有任何统一收口**：`web_search` 用 `urllib` 直连、MCP 的 `http`/`sse`
传输各建各的连接、模型调用走 SDK、搜索网关自己在容器里出网。任何一处漏掉，策略就形同虚设。

同时必须先承认一个事实：**并不是所有内网访问都是攻击**。这个平台的正常工作依赖一批
内网地址：

| 调用 | 目标 |
| --- | --- |
| 编排与状态 | Dapr sidecar `:3500`、Redis、PostgreSQL |
| 追踪 | Jaeger `:4318` |
| 搜索 | 本地 `search-gateway`（ADR-032，`http://search-gateway:8800/search`） |
| 模型回退 | Ollama（`AGENT_OLLAMA_BASE_URL`，compose 默认指向 `host.docker.internal:11434`） |
| 模型主路 | 使用者自建的 OpenAI 兼容网关，常见形态就是内网地址 |

所以第一条决策不是"拦哪些 IP"，而是**把两类流量分开**。

## 决策

### 1. 两类流量，两套规则

- **平台内部依赖**：由部署拓扑决定的固定目标（Dapr、Redis、PG、Jaeger、search-gateway）。
  它们不出网，也不受私网判定约束——写进 `EGRESS_INTERNAL_HOSTS` 的**精确主机名**白名单，
  不做后缀匹配，不随用户配置变化而扩大。
- **Agent 行为出网**：模型看不到、由使用者输入或外部内容决定目标的调用——`web_search`
  的实际目标、登记的 MCP `http`/`sse` Server、以及任何未来的抓取工具。**这一类才是要按
  公网/白名单管的**。
- **模型流量单列**（决策见 §3）：按「平台内部依赖」处理，但**不是无条件放行**。

### 2. 默认 `public_only`：拦私网，放公网

`EGRESS_MODE` 两档：

| 模式 | 语义 |
| --- | --- |
| `public_only`（**默认**） | 只允许解析到公网地址的目标；域名黑名单优先 |
| `allowlist` | 在 `public_only` 之上再要求域名命中 `EGRESS_ALLOW_HOSTS` |

判定顺序（**每一步失败都拒绝，不降级**）：

1. scheme 只允许 `http` / `https`（默认只放 `443`，需要 `80` 显式开）；
2. 拒绝 URL 里带 `user:pass@`；
3. 域名小写 + IDNA/punycode 归一化后，过**黑名单**（优先于白名单）→ 白名单（`allowlist`
   模式下）；
4. `getaddrinfo` 解析出**全部**地址，逐个做私网判定（豁免类见 §3）；
5. **连解析出来的那个 IP**，不重新解析——消除"校验时公网、连接时内网"的 rebinding 时间窗；
6. 重定向**不自动跟随**，每一跳回到第 1 步重跑；跳数上限 5。

私网判定覆盖（IPv4 与 IPv6 都必须查，只查 IPv4 等于没做）：

`0.0.0.0/8`、`10/8`、`100.64/10`、`127/8`、`169.254/16`（含云元数据 `169.254.169.254`）、
`172.16/12`、`192.0.0/24`、`192.168/16`、`198.18/15`、`224/4`、`240/4`；
`::`、`::1`、`fc00::/7`、`fe80::/10`、`ff00::/8`、IPv4-mapped 的 `::ffff:0:0/96`。

### 3. 豁免规则是**有限枚举**，不是"内网放行"

| 豁免对象 | 豁免什么 | 不豁免什么 |
| --- | --- | --- |
| `EGRESS_INTERNAL_HOSTS` 里的主机名 | 私网判定 | scheme、端口、黑名单、IP 钉扎 |
| 模型流量（`EGRESS_MODEL_EXEMPT=true`） | 私网判定（放行 Ollama 与使用者自建的内网网关） | scheme、域名黑名单、端口白名单 |

模型豁免的理由是**既有产品能力**：`AGENT_LLM_PROVIDER=ollama` 与自建 OpenAI 兼容网关
（ADR-014 明确支持）本来就是内网地址，若一并拦掉，平台会直接不出结果——那不是安全，
是把自己关停。

实现上模型流量用**带标签的 egress 客户端**（`purpose="model"`）打标，而不是按目标地址
猜：同一个内网 IP，模型调用可以走，爬取类工具调用不能走。

### 4. 强制点在网络层，应用层只负责说清原因

应用层收口（`app/security/egress.py`）能挡住我们自己的代码，挡不住未来某处的直连。
所以分两步落：

1. **应用层**：所有出网换成统一客户端，命中拒绝时返回结构化原因
   （`private_ip` / `denied_host` / `scheme` / `port` / `redirect`）并记
   `macp_egress_blocked_total{reason=...}`；
2. **网络层**：backend 不给默认路由直出，只能经 egress 代理；代理里跑同一套规则。
   到了这一步，"绕过应用层"不再等于"绕过策略"。

沙箱的网络保持关闭（`SANDBOX_NETWORK_ENABLED=false`）。若将来要给沙箱联网，**只准走同一个
代理**并带 `purpose="sandbox"` 标签，绝不给直连。

### 5. 域名匹配规则

- 黑名单优先于白名单（同一条目同时命中时拒绝）；
- 后缀匹配必须按 label 边界：`example.com` 只命中 `example.com` 与 `*.example.com`，
  **不命中** `evil-example.com`；
- 只支持精确主机名与 `*.` 前缀通配，不支持 `*` 出现在中间、不支持正则；
- 配置里的每一条都要在启动时校验格式，非法配置**拒绝启动**（不能静默忽略一条拼错的规则）。

## 备选方案

- **只做应用层收口**：改动最小，但任何绕过（新工具、第三方库、未来的插件）都是洞。否决为
  终态，仅作为第一步。
- **只做代理层**：最硬，但错误信息只能回到"连接失败"，排障体验差；且开发态（宿主机直跑）
  没有代理。否决为唯一手段，与 §4 的两步并用。
- **默认 `allowlist`**：更严，但会让"换一个搜索源/加一个 MCP Server"这类正常操作变成改配置。
  暂不做默认；需要更严的部署可以自行切到 `allowlist`。
- **模型也不豁免**：会关停 Ollama 回退与内网网关（ADR-014 的既有能力）。使用者已明确选择
  豁免内网判定，此方案仅作记录。

## 代价（如实记录）

- **模型豁免是一个口径风险**：若使用者把模型 `base_url` 指向一个被攻陷的内网服务，策略不会
  拦。接受这个风险的理由是产品能力优先；缓解是模型端点只由使用者/管理员配置，Agent 无法
  改写它。
- **代理是单点**：代理挂掉 = 所有 Agent 出网失败（平台内部依赖不受影响，因为走服务名直连）。
  这是有意的取舍：宁可失败可见，不要静默绕过。
- **`host.docker.internal` 这类地址**：它指向宿主，属于豁免类（模型）；Compose 服务的
  内网 IP（`172.x`）属于 `EGRESS_INTERNAL_HOSTS`。两者都要在文档里写清，否则使用者会把
  "允许内网服务"误读成"允许内网"。
- **无法阻止使用者自己在宿主上开洞**：本 ADR 管的是平台自身与 Agent 的出网，不是宿主的
  网络配置。

## 影响（待实现）

| 位置 | 改动 |
| --- | --- |
| `app/security/egress.py`（新） | 策略判定、IP 钉扎、重定向重校验、指标与日志 |
| `app/tools/search.py` | 换用统一客户端（`purpose="tool"`） |
| `app/mcp/client.py` | `http`/`sse` 传输换用统一客户端（`purpose="mcp"`） |
| `app/orchestration/`（模型调用） | 换用统一客户端（`purpose="model"`） |
| `scripts/search_gateway.py` | 上游出网同样受策略约束（`purpose="gateway"`） |
| `deploy/compose.yaml` | egress 代理服务；backend 与沙箱只经代理出网 |
| `doc/deployment.md` | `EGRESS_*` 环境变量与默认值 |

## 验证（实现时按此测试）

- **SSRF 表**（≥15 例）：各私网段边界值、IPv6 与 IPv4-mapped、`http://2130706433/`（十进制
  IP）、`http://0x7f.1/`、`localhost` 与 `*.local`/mDNS、解析后重绑定（首跳公网、次跳内网）、
  30x 跳到内网、`user:pass@`、非 http(s) scheme、非白名单端口；
- **豁免正确性**：`search-gateway` 与 Dapr/Redis/PG 可直连；Ollama 与内网模型网关在
  `purpose="model"` 下放行，同样的地址在 `purpose="tool"` 下被拒；
- **域名边界**：`evil-example.com` 不命中 `example.com`；punycode 域名按同一套规则判定；
- **配置校验**：非法 CIDR / 非法通配 / 空条目在启动时失败，而不是被忽略；
- **代理层**：应用层被绕过后（例如直接 `requests` 测试脚本）仍然连不出去。

## 实现口径（2026-09-23）

已落地的是**应用层判定 + 钉扎取数**，落在 `app/security/`：

- `egress.py`：按 §2 的顺序判定（scheme → 端口 → 域名黑/白名单 → 解析 → 私网 CIDR →
  逐跳重校验重定向），连接**钉在解析出的 IP** 上（HTTPS 仍按原域名做 SNI/证书校验），
  响应体有上限；被拒时抛 `EgressDenied(reason)` 并同时记 `egress.blocked` 日志与
  Prometheus 的 `macp_egress_blocked_total`；
- 配置在**构造时**校验：非法端口/通配/字符直接抛 `EgressConfigError`，不做"跳过这条"；
- 接线两处：工具侧（`app/tools/search.py` 的默认取数以 `purpose="tool"` 走策略，
  拒绝映射为 `retryable=false`）与远程 MCP（`http`/`sse` 在**打开会话时**判定，
  拒绝复用 `McpTransportUnsupported`）。校验刻意不做在构造会话工厂时：合并工具目录
  那条路径要求零 IO（ADR-026），构造期做 DNS 会把目录变成"网络可用性的函数"；
- `GET /api/v1/config/egress` 提供只读投影（理由：能改策略的接口就是绕过边界的路）。

## 修订（2026-09-23）：四项补齐——代理落地，其余全部逼到代理上

第一版实现之后留了四条口子。第二轮的做法是**先造出那个真正的强制点**，再把其余三项
都变成「把流量逼到它上面」：

1. **网络层强制：新增 `egress-proxy` 服务**（`scripts/egress_proxy.py`，与 backend 共用
   镜像）。它是 HTTP 代理：明文 HTTP 走绝对 URI 转发、HTTPS 走 `CONNECT` 隧道，
   两种形态都先经**同一套策略**（解析 → 私网判定 → 钉在 IP 上连接）再转发，
   策略日志与 `macp_egress_blocked_total` 一并复用。代理自己**不允许再配代理**
   （启动即拒绝，避免绕圈），且 `EGRESS_INTERNAL_HOSTS` 留空——代理只服务公网访问，
   内部服务由客户端直连（`NO_PROXY` 排除），否则代理会变成"任何容器都能探测内网"的跳板。
2. **MCP / 模型 SDK 的钉扎问题，由代理解决**：这两个 SDK 自建 httpx 连接、不接受自定义
   transport，但它们都认 `HTTP_PROXY`/`HTTPS_PROXY`，于是请求整段落到代理手里——
   "解析域名 + 判私网 + 钉住 IP 连接"三件事在代理侧一次完成。客户端侧因此**不再需要
   DNS**：代理模式下客户端只解析代理主机（compose 服务名），`EgressPolicy.evaluate`
   相应跳过本地解析与私网判定（scheme / 端口 / 域名白黑名单仍在本地跑，好处是拒绝快、
   原因准）。
3. **search-gateway 脚本出网**：保持"只用标准库"的设计，靠 `HTTP_PROXY`/`HTTPS_PROXY`
   环境变量接入（脚本里显式构造 `ProxyHandler`，不是依赖 urllib 的隐式行为）。
4. **沙箱联网**：`SANDBOX_NETWORK_ENABLED=true` 时，沙箱**只接内部网络**
   （`SANDBOX_EGRESS_NETWORK`，compose 里 `internal: true`，没有默认路由），并注入
   `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`。于是"允许沙箱联网"等于"允许它经代理访问公网"，
   而不是把它直接接到公网。默认仍然是关的。

**还没做完的最后一步**（需要可用的 Docker 引擎才能验）：

- **把 backend 与 search-gateway 从默认网络挪到 internal-only**，从物理上断掉直连。
  现在的状态是：代理已就绪、四类客户端的代理变量都已接上、应用层判定仍在兜底，
  但 backend 仍在默认网络上，**理论上**仍可绕过代理直连。这一步必须实测（端口发布、
  DNS、前端 `/api` 反代、prometheus 抓取都在同一拓扑里），所以先在文档里写明变更点，
  等引擎恢复后按 §4 的验证清单逐条过；
- 代理侧**无法**校验 CONNECT 隧道的目标证书（这是隧道代理的固有性质）：证书校验由
  客户端完成。所以"钉扎"在这里的含义是"连接钉在解析出的公网 IP 上 + 私网一律拒绝"，
  不是"代理替客户端做 TLS 校验"。

另外补了一条实现细节：**端口白名单与私网豁免是两件事**。search-gateway 是内网服务，
但它在 8800 端口，所以部署侧要同时配 `EGRESS_INTERNAL_HOSTS=…search-gateway…` 与
`EGRESS_ALLOWED_PORTS=443,8800`——豁免的是「目标是不是内网」，不是「这个端口能不能用」。

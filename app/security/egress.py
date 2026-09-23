"""出网策略：判定顺序、私网拦截与钉扎取数（ADR-034）。

判定顺序（每一步失败都拒绝，**不降级**）：

1. scheme 只允许 `http` / `https`，且 URL 里不能带 `user:pass@`；
2. 端口命中 `EGRESS_ALLOWED_PORTS`；
3. 域名归一化（小写、去尾点、IDNA）后过**黑名单**（优先）与白名单（`allowlist` 模式）；
4. `getaddrinfo` 解析出**全部**地址，逐个做私网判定（内部服务与模型流量按配置豁免）；
5. 连接**钉在解析出来的那个 IP** 上（`_PinnedHTTPSConnection`），消除"校验时公网、
   连接时内网"的 rebinding 时间窗；
6. 重定向不自动跟随：每一跳回到第 1 步重跑，跳数上限 `EGRESS_MAX_REDIRECTS`。

被拒绝时抛 `EgressDenied`（带 `reason`，与指标标签同名），并记一条结构化日志。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlsplit

from app.observability.logging import get_logger, log_event
from app.observability.metrics import record_egress_blocked
from app.security.config import EgressSettings, get_egress_settings

logger = get_logger("security.egress")

USER_AGENT = "macp-agent/0.1 (+multi-agent-collaboration-platform)"

BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 含云元数据 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)
"""私网与保留网段。**IPv6 必须一起查**：只查 IPv4 等于没做。"""

_IPV4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")


class EgressConfigError(RuntimeError):
    """出网配置本身非法：**拒绝启动**而不是静默忽略一条拼错的规则（ADR-034 §5）。"""


class EgressDenied(RuntimeError):
    """出网被策略拒绝。`reason` 与指标标签一致，便于聚合。"""

    def __init__(self, reason: str, detail: str, *, host: str = "") -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.host = host


@dataclass(frozen=True)
class EgressTarget:
    """通过判定的一次出网目标；`addresses` 是用来**钉扎**的地址列表。"""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    addresses: tuple[str, ...]
    purpose: str
    private_exempt: bool
    via_proxy: bool = False

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def parse_host_patterns(value: str) -> tuple[str, ...]:
    """解析并校验域名列表；非法条目直接抛错，不做"跳过这一条"的宽容处理。"""

    patterns: list[str] = []
    for raw in (value or "").split(","):
        item = raw.strip().lower().rstrip(".")
        if not item:
            continue
        wildcard = item.startswith("*.")
        body = item[2:] if wildcard else item
        if not body or any(char not in _HOST_CHARS for char in body):
            raise EgressConfigError(f"非法的域名规则：{raw!r}")
        if "*" in body:
            raise EgressConfigError(f"通配符只能出现在最前面的 `*.`：{raw!r}")
        if ":" in item or "/" in item:
            raise EgressConfigError(f"域名规则不能带端口或路径：{raw!r}")
        patterns.append(item)
    return tuple(patterns)


def parse_ports(value: str) -> frozenset[int]:
    ports: set[int] = set()
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        if not item.isdigit() or not (0 < int(item) < 65536):
            raise EgressConfigError(f"非法的端口：{raw!r}")
        ports.add(int(item))
    if not ports:
        raise EgressConfigError("EGRESS_ALLOWED_PORTS 不能为空")
    return frozenset(ports)


def parse_internal_hosts(value: str) -> frozenset[str]:
    hosts: set[str] = set()
    for raw in (value or "").split(","):
        item = raw.strip().lower().rstrip(".")
        if not item:
            continue
        if "*" in item:
            raise EgressConfigError(f"内部服务必须写精确主机名，不支持通配：{raw!r}")
        hosts.add(item)
    return frozenset(hosts)


def is_blocked_address(address: str) -> bool:
    """该地址是否属于被拦截的私网/保留网段（含 IPv4-mapped 的 IPv6）。"""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    candidates = [parsed]
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        candidates.append(parsed.ipv4_mapped)
    for candidate in candidates:
        if any(candidate in network for network in BLOCKED_NETWORKS):
            return True
    return False


class EgressPolicy:
    """一次判定 + 钉扎取数的执行者。配置在构造时就校验完。"""

    def __init__(
        self,
        settings: EgressSettings | None = None,
        *,
        resolver=None,
    ) -> None:
        self.settings = settings or get_egress_settings()
        self._allow = parse_host_patterns(self.settings.allow_hosts)
        self._deny = parse_host_patterns(self.settings.deny_hosts)
        self._internal = parse_internal_hosts(self.settings.internal_hosts)
        self._no_proxy = parse_internal_hosts(self.settings.no_proxy)
        self._ports = parse_ports(self.settings.allowed_ports)
        self._resolver = resolver or _default_resolver
        self.blocked: dict[str, int] = {}

    # -- 判定 --------------------------------------------------------------- #

    def evaluate(self, url: str, *, purpose: str = "tool") -> EgressTarget:
        parsed = urlsplit(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in {"http", "https"}:
            self._deny_and_raise("scheme", f"只允许 http(s)：{url}", host=parsed.hostname or "")
        if parsed.username or parsed.password:
            self._deny_and_raise(
                "userinfo", f"URL 不能带用户名密码：{url}", host=parsed.hostname or ""
            )

        raw_host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not raw_host:
            self._deny_and_raise("host", f"地址里没有主机名：{url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        if port not in self._ports:
            self._deny_and_raise(
                "port",
                f"端口不在允许列表（{sorted(self._ports)}）：{raw_host}:{port}",
                host=raw_host,
            )

        host = _normalize_host(raw_host)
        if self._matches(host, self._deny):
            self._deny_and_raise("denied_host", f"域名在黑名单里：{host}", host=host)
        if self.settings.mode == "allowlist" and not self._matches(host, self._allow):
            self._deny_and_raise(
                "not_allowlisted", f"域名不在白名单里（allowlist 模式）：{host}", host=host
            )

        exempt = host in self._internal or raw_host in self._internal
        # 内部服务与 NO_PROXY 列表里的主机**直连**：它们本来就在同一张内网里，
        # 让它们绕代理既没意义、代理也会（按设计）把它们拦下。
        direct = exempt or host in self._no_proxy or raw_host in self._no_proxy
        via_proxy = bool(self.settings.proxy_url) and not direct
        # 有代理时**不在本地解析**：代理才是硬边界，而且容器可能处在一个外网 DNS
        # 不通的 internal 网络里（客户端只需解析代理本身）。域名与端口规则仍然在本地跑，
        # 好处是拒绝能给出准确原因、不必等一次代理往返。
        addresses = () if via_proxy else self._addresses(host)
        if not exempt and purpose == "model" and self.settings.model_exempt:
            exempt = True
        if not exempt and not via_proxy:
            for address in addresses:
                if is_blocked_address(address):
                    self._deny_and_raise(
                        "private_ip",
                        f"目标是内网/保留地址，已拦截：{host} → {address}",
                        host=host,
                    )

        target = EgressTarget(
            url=url,
            scheme=scheme,
            host=host,
            port=port,
            path=parsed.path or "/",
            addresses=addresses,
            purpose=purpose,
            private_exempt=exempt,
            via_proxy=via_proxy,
        )
        query = f"?{parsed.query}" if parsed.query else ""
        log_event(
            logger,
            "egress.allowed",
            host=host,
            port=port,
            purpose=purpose,
            private_exempt=exempt,
            addresses=len(addresses),
        )
        return EgressTarget(
            url=target.url,
            scheme=target.scheme,
            host=target.host,
            port=target.port,
            path=target.path + query,
            addresses=target.addresses,
            purpose=target.purpose,
            private_exempt=target.private_exempt,
            via_proxy=target.via_proxy,
        )

    def _addresses(self, host: str) -> tuple[str, ...]:
        """IP 字面量直接用；域名走解析（全部地址都要判，不能只看第一个）。"""

        try:
            return (str(ipaddress.ip_address(host)),)
        except ValueError:
            pass
        try:
            resolved = self._resolver(host)
        except Exception as exc:  # DNS 失败：拒绝而不是放行
            self._deny_and_raise("dns", f"域名解析失败：{host}（{exc}）", host=host)
        unique = sorted({address for address in resolved})
        if not unique:
            self._deny_and_raise("dns", f"域名解析不到地址：{host}", host=host)
        return tuple(unique)

    @staticmethod
    def _matches(host: str, patterns: Iterable[str]) -> bool:
        """精确匹配或 `*.` 前缀匹配（**按 label 边界**，`evil-example.com` 不命中）。"""

        for pattern in patterns:
            if pattern.startswith("*."):
                suffix = pattern[1:]  # ".example.com"
                if host.endswith(suffix) and len(host) > len(suffix):
                    return True
            elif host == pattern:
                return True
        return False

    def _deny_and_raise(self, reason: str, detail: str, *, host: str = "") -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1
        log_event(logger, "egress.blocked", level=30, reason=reason, host=host, detail=detail)
        record_egress_blocked(reason)
        raise EgressDenied(reason, detail, host=host)


def _normalize_host(host: str) -> str:
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return host.encode("idna").decode("ascii").lower()
    return host


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


@lru_cache
def get_egress_policy() -> EgressPolicy:
    """进程级策略实例（配置在首次使用时校验，非法配置直接抛 `EgressConfigError`）。"""

    return EgressPolicy()


# --------------------------------------------------------------------------- #
# 钉扎取数
# --------------------------------------------------------------------------- #


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连到**指定 IP**，但 `Host` 头与请求路径仍是原域名的。"""

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS 版：先连 IP，再用**原域名**做 SNI/证书校验。

    这正是"钉扎"的意义：DNS 在校验之后被改写也影响不了这次连接，而证书校验仍然
    按域名来——不会因为钉扎而放宽 TLS。
    """

    def __init__(
        self,
        host: str,
        address: str,
        port: int,
        *,
        context: ssl.SSLContext,
        timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool


def http_get(
    url: str,
    *,
    purpose: str = "tool",
    policy: EgressPolicy | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> HttpResponse:
    """按策略取一次 GET；重定向逐跳重校验。"""

    return _send(
        url,
        method="GET",
        body=None,
        extra_headers=None,
        purpose=purpose,
        policy=policy,
        timeout=timeout,
        max_bytes=max_bytes,
    )


def http_post(
    url: str,
    *,
    payload: dict[str, Any] | bytes,
    headers: dict[str, str] | None = None,
    purpose: str = "tool",
    policy: EgressPolicy | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> HttpResponse:
    """按策略发一次 POST（默认 JSON），**走与 GET 完全相同的判定与钉扎**（ADR-037 §4）。

    为什么出网层要开 POST：豆包搜索（`open.feedcoopapi.com/search_api/web_search`）是 POST +
    自定义请求头。判定顺序、私网拦截、连接钉扎、重定向上限、响应体上限一个都不能少，
    所以不另开一条"裸"通道，而是复用同一条路径。

    `headers` 只用于鉴权这类附加头；`Host` / `User-Agent` / `Content-Length` 不允许覆盖——
    前两个决定钉扎与反爬语义，后一个由 http.client 按实际字节数写。
    """

    if isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
        merged: dict[str, str] = {}
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json"}
    merged.update(headers or {})

    return _send(
        url,
        method="POST",
        body=body,
        extra_headers=merged,
        purpose=purpose,
        policy=policy,
        timeout=timeout,
        max_bytes=max_bytes,
    )


_FORBIDDEN_EXTRA_HEADERS = frozenset({"host", "user-agent", "content-length"})


def _build_headers(target: EgressTarget, extra: dict[str, str] | None) -> dict[str, str]:
    """默认头 + 调用方附加头；附加头不得覆盖决定钉扎与长度语义的那几个。"""

    headers = {
        "Host": target.host_header,
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    for key, value in (extra or {}).items():
        if key.lower() in _FORBIDDEN_EXTRA_HEADERS:
            raise EgressConfigError(f"禁止覆盖请求头：{key}")
        headers[key] = value
    return headers


def _send(
    url: str,
    *,
    method: str,
    body: bytes | None,
    extra_headers: dict[str, str] | None,
    purpose: str,
    policy: EgressPolicy | None,
    timeout: float | None,
    max_bytes: int | None,
) -> HttpResponse:
    """GET / POST 共用的发送循环：每一跳都重新判定，重定向上限照旧。"""

    resolved_policy = policy or get_egress_policy()
    settings = resolved_policy.settings
    limit = max_bytes or settings.max_response_bytes
    deadline = timeout or settings.timeout_seconds

    current = url
    current_method, current_body, current_headers = method, body, extra_headers
    for _hop in range(settings.max_redirects + 1):
        target = resolved_policy.evaluate(current, purpose=purpose)
        if target.via_proxy:
            status, headers, payload, truncated = _request_via_proxy(
                target,
                proxy=settings.proxy_url,
                timeout=deadline,
                max_bytes=limit,
                method=current_method,
                body=current_body,
                extra_headers=current_headers,
            )
        else:
            status, headers, payload, truncated = _request_once(
                target,
                timeout=deadline,
                max_bytes=limit,
                method=current_method,
                body=current_body,
                extra_headers=current_headers,
            )
        location = headers.get("location")
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            # 标准语义：307/308 保留方法与请求体；301/302/303 把非 GET 降级成 GET 并丢掉请求体。
            if status in {301, 302, 303} and current_method != "GET":
                current_method, current_body, current_headers = "GET", None, None
            continue
        return HttpResponse(
            url=current, status=status, headers=headers, body=payload, truncated=truncated
        )
    raise EgressDenied(
        "redirect_limit",
        f"重定向超过 {settings.max_redirects} 跳，已停止：{url}",
    )


def _request_once(
    target: EgressTarget,
    *,
    timeout: float,
    max_bytes: int,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes, bool]:
    address = target.addresses[0]
    if target.scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
            target.host,
            address,
            target.port,
            context=ssl.create_default_context(),
            timeout=timeout,
        )
    else:
        connection = _PinnedHTTPConnection(target.host, address, target.port, timeout)
    headers = _build_headers(target, extra_headers)
    try:
        connection.request(method, target.path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(max_bytes + 1)
        headers = {key.lower(): value for key, value in response.getheaders()}
        truncated = len(raw) > max_bytes
        return response.status, headers, raw[:max_bytes], truncated
    finally:
        connection.close()


def _request_via_proxy(
    target: EgressTarget,
    *,
    proxy: str,
    timeout: float,
    max_bytes: int,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes, bool]:
    """经代理取一次：HTTP 用绝对 URI，HTTPS 用 CONNECT 隧道。

    这里**不做 DNS**：客户端只解析代理主机（compose 服务名），目标域名与地址判定都在
    代理侧完成——这也是代理能解决「MCP / 模型 SDK 内部无法钉扎」的原因：那些 SDK
    只认识 `HTTP_PROXY`/`HTTPS_PROXY`，而它们发出的请求会整段落到代理手里。
    """

    parsed = urlsplit(proxy)
    proxy_host = parsed.hostname or ""
    proxy_port = parsed.port or 8080
    if not proxy_host:
        raise EgressDenied("proxy", f"EGRESS_PROXY_URL 不是合法地址：{proxy}")

    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
    try:
        if target.scheme == "https":
            connection.set_tunnel(target.host, target.port)
            connection.connect()
            tunnel = connection.sock
            if tunnel is None:  # pragma: no cover - connect 之后必有 socket
                raise EgressDenied("proxy", f"代理隧道建立失败：{proxy}")
            wrapped = ssl.create_default_context().wrap_socket(
                tunnel, server_hostname=target.host
            )
            connection.sock = wrapped
            request_target = target.path
        else:
            request_target = target.url
        connection.request(
            method,
            request_target,
            body=body,
            headers=_build_headers(target, extra_headers),
        )
        response = connection.getresponse()
        raw = response.read(max_bytes + 1)
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, raw[:max_bytes], len(raw) > max_bytes
    except OSError as exc:
        raise EgressDenied("proxy", f"代理不可达或不接受该目标：{proxy}（{exc}）") from exc
    finally:
        connection.close()


def fetch_json(
    url: str,
    params: dict[str, str] | None = None,
    timeout: float | None = None,
    *,
    purpose: str = "tool",
    policy: EgressPolicy | None = None,
) -> Any:
    """带查询串的 GET 并解析 JSON（工具侧的默认取数入口）。"""

    full = f"{url}?{urlencode(params)}" if params else url
    response = http_get(full, purpose=purpose, policy=policy, timeout=timeout)
    if response.status >= 400:
        raise EgressDenied("http_status", f"上游返回 HTTP {response.status}：{url}")
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EgressDenied("invalid_json", f"上游返回的不是合法 JSON：{url}（{exc}）") from exc


__all__ = [
    "BLOCKED_NETWORKS",
    "EgressConfigError",
    "EgressDenied",
    "EgressPolicy",
    "EgressTarget",
    "HttpResponse",
    "fetch_json",
    "get_egress_policy",
    "http_get",
    "http_post",
    "is_blocked_address",
    "parse_host_patterns",
    "parse_internal_hosts",
    "parse_ports",
]

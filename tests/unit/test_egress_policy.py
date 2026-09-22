"""出网策略判定（ADR-034 §2/§3/§5）。

按**攻击面**列表：内网与保留网段的边界值、IPv6 与 IPv4-mapped、IP 变体写法、
scheme/端口/userinfo、白黑名单语义、内部服务与模型流量的豁免边界。

全程不碰真实网络：解析器是注入的替身。
"""

from __future__ import annotations

import pytest

from app.security.egress import (
    EgressConfigError,
    EgressDenied,
    EgressPolicy,
    is_blocked_address,
    parse_host_patterns,
    parse_internal_hosts,
    parse_ports,
)
from app.security.config import EgressSettings

PUBLIC = "93.184.216.34"


def _policy(**overrides) -> EgressPolicy:
    settings = {
        "allowed_ports": "443,80,8800",
        "internal_hosts": "search-gateway,dapr-sidecar,redis",
        "resolver": None,
    }
    resolver = overrides.pop("resolver", None)
    settings.update(overrides)
    resolved = EgressSettings(_env_file=None, **settings)
    return EgressPolicy(resolved, resolver=resolver or (lambda host: [PUBLIC]))


def _resolver(mapping):
    return lambda host: mapping.get(host, [PUBLIC])


REJECTED = [
    # 回环、私网、链路本地、CGNAT、保留段
    ("https://127.0.0.1/", "private_ip"),
    ("http://127.1.2.3:80/", "private_ip"),
    ("http://10.0.0.7:80/", "private_ip"),
    ("http://172.16.0.1:80/", "private_ip"),
    ("http://172.31.255.254:80/", "private_ip"),
    ("http://192.168.1.1:80/", "private_ip"),
    ("http://169.254.169.254:80/", "private_ip"),  # 云元数据
    ("http://100.64.0.1:80/", "private_ip"),
    ("http://0.0.0.0:80/", "private_ip"),
    ("http://198.18.0.1:80/", "private_ip"),
    ("http://224.0.0.1:80/", "private_ip"),
    # IPv6 与 IPv4-mapped：只查 IPv4 等于没做
    ("http://[::1]:80/", "private_ip"),
    ("http://[fe80::1]:80/", "private_ip"),
    ("http://[fc00::1]:80/", "private_ip"),
    ("http://[::ffff:127.0.0.1]:80/", "private_ip"),
    ("http://[::ffff:192.168.0.1]:80/", "private_ip"),
    # 协议、凭据、端口
    ("ftp://example.com/", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("http://user:pass@example.com:80/", "userinfo"),
    ("https://example.com:9999/", "port"),
]


@pytest.mark.parametrize("url, reason", REJECTED)
def test_private_and_illegal_targets_are_rejected(url: str, reason: str):
    policy = _policy()

    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate(url)

    assert excinfo.value.reason == reason
    assert policy.blocked[reason] == 1, "拒绝要计数，供 /config/egress 与 Prometheus 使用"


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "127.0.0.1.nip.io"])
def test_weird_ip_spellings_are_blocked_after_resolution(host: str):
    """十进制/十六进制/回环域名的写法都只是"解析结果不同"，判定在解析之后。"""

    policy = _policy(resolver=_resolver({host: ["127.0.0.1"]}))

    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate(f"http://{host}:80/")

    assert excinfo.value.reason == "private_ip"


def test_public_targets_pass():
    policy = _policy(resolver=_resolver({"example.com": [PUBLIC], "sub.example.com": [PUBLIC]}))

    assert policy.evaluate("https://example.com/a/b").host == "example.com"
    assert policy.evaluate("https://sub.example.com/").port == 443


def test_dns_failure_is_a_rejection_not_a_pass():
    def broken(_host):
        raise OSError("no such host")

    policy = _policy(resolver=broken)

    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("https://nowhere.example/")

    assert excinfo.value.reason == "dns"


def test_internal_services_are_exempt_from_the_private_check():
    policy = _policy(
        resolver=_resolver({"search-gateway": ["172.18.0.5"], "redis": ["172.18.0.6"]})
    )

    target = policy.evaluate("http://search-gateway:8800/search")
    assert target.private_exempt is True
    # 豁免的只是私网判定：端口仍要在允许列表里
    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("http://redis:6390/")
    assert excinfo.value.reason == "port"


def test_internal_hosts_do_not_open_the_whole_private_range():
    """放行的是**列出的服务**，不是"整个内网"。"""

    policy = _policy(resolver=_resolver({"other-service": ["172.18.0.9"]}))

    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("http://other-service:8800/")

    assert excinfo.value.reason == "private_ip"


def test_model_traffic_exemption_is_scoped_to_the_purpose():
    """「甲」：同样的内网地址，模型调用放行、爬取类工具调用拒绝。"""

    policy = _policy(resolver=_resolver({"gateway.internal": ["192.168.1.9"]}))

    assert policy.evaluate("http://gateway.internal:8800/v1", purpose="model").host == (
        "gateway.internal"
    )
    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("http://gateway.internal:8800/v1", purpose="tool")
    assert excinfo.value.reason == "private_ip"


def test_model_exemption_can_be_turned_off():
    policy = _policy(
        model_exempt=False, resolver=_resolver({"gateway.internal": ["192.168.1.9"]})
    )

    with pytest.raises(EgressDenied):
        policy.evaluate("http://gateway.internal:8800/v1", purpose="model")


# --------------------------------------------------------------------------- #
# 域名规则
# --------------------------------------------------------------------------- #


def test_denylist_wins_over_allowlist():
    policy = _policy(mode="allowlist", allow_hosts="example.com", deny_hosts="example.com")

    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("https://example.com/")

    assert excinfo.value.reason == "denied_host"


def test_allowlist_mode_blocks_everything_not_listed():
    policy = _policy(mode="allowlist", allow_hosts="*.example.com")

    assert policy.evaluate("https://a.example.com/").host == "a.example.com"
    with pytest.raises(EgressDenied) as excinfo:
        policy.evaluate("https://example.com/")
    assert excinfo.value.reason == "not_allowlisted"


def test_wildcard_matching_respects_label_boundaries():
    policy = _policy(deny_hosts="*.example.com")

    with pytest.raises(EgressDenied):
        policy.evaluate("https://a.example.com/")
    # `evil-example.com` 不是 `example.com` 的子域，不能被后缀匹配误伤
    assert (
        policy.evaluate("https://evil-example.com/", ).host == "evil-example.com"
    )


def test_patterns_reject_ports_wildcards_in_the_middle_and_bad_chars():
    with pytest.raises(EgressConfigError):
        parse_host_patterns("example.com:8080")
    with pytest.raises(EgressConfigError):
        parse_host_patterns("a.*.com")
    with pytest.raises(EgressConfigError):
        parse_host_patterns("exa mple.com")
    with pytest.raises(EgressConfigError):
        parse_ports("443,abc")
    with pytest.raises(EgressConfigError):
        parse_ports("")
    with pytest.raises(EgressConfigError):
        parse_internal_hosts("*.internal")


def test_invalid_configuration_is_reported_at_construction():
    """非法配置拒绝启动：构造策略时就抛，而不是运行期静默忽略一条规则。"""

    with pytest.raises(EgressConfigError):
        EgressPolicy(EgressSettings(_env_file=None, allowed_ports="https"))


def test_ipv4_mapped_helper_is_consistent():
    assert is_blocked_address("::ffff:10.0.0.1") is True
    assert is_blocked_address("::ffff:93.184.216.34") is False
    assert is_blocked_address("not-an-ip") is True


# --------------------------------------------------------------------------- #
# 强制代理模式（ADR-034 §4）
# --------------------------------------------------------------------------- #


def test_proxy_mode_skips_local_dns_but_keeps_host_rules():
    """有代理时不本地解析（代理才是硬边界），但 scheme/端口/域名规则仍在本地跑。"""

    def explode(_host):
        pytest.fail("代理模式下不应该做本地 DNS 解析")

    policy = _policy(
        proxy_url="http://egress-proxy:8888",
        resolver=explode,
        deny_hosts="blocked.example.com",
        allowed_ports="443",
    )

    target = policy.evaluate("https://example.com/")
    assert target.via_proxy is True
    assert target.addresses == ()
    assert target.private_exempt is False, "代理模式下本地不做私网判定，但也不声称豁免"

    with pytest.raises(EgressDenied) as denied_host:
        policy.evaluate("https://blocked.example.com/")
    assert denied_host.value.reason == "denied_host"

    with pytest.raises(EgressDenied) as denied_port:
        policy.evaluate("https://example.com:9999/")
    assert denied_port.value.reason == "port"


def test_proxy_mode_still_rejects_illegal_schemes_and_userinfo():
    policy = _policy(proxy_url="http://egress-proxy:8888")

    with pytest.raises(EgressDenied) as scheme:
        policy.evaluate("ftp://example.com/")
    assert scheme.value.reason == "scheme"

    with pytest.raises(EgressDenied) as userinfo:
        policy.evaluate("https://user:pass@example.com/")
    assert userinfo.value.reason == "userinfo"

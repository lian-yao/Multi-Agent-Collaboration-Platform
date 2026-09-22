"""出网策略配置（ADR-034，环境变量前缀 `EGRESS_`）。

列表类配置用**逗号分隔**而不是 JSON 数组：`deploy/.env` 与 compose 的
`${VAR:-default}` 插值都不是 JSON 语境，写成 JSON 只会让人写错还看不出错。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

EgressMode = Literal["public_only", "allowlist"]


class EgressSettings(BaseSettings):
    """出网策略参数。"""

    model_config = SettingsConfigDict(
        env_prefix="EGRESS_",
        env_file=".env",
        extra="ignore",
    )

    mode: EgressMode = "public_only"
    """`public_only`（默认，只放公网）或 `allowlist`（再要求域名命中白名单）。"""

    allow_hosts: str = ""
    """域名白名单（逗号分隔）。`allowlist` 模式下未命中的一律拒绝。"""

    deny_hosts: str = ""
    """域名黑名单（逗号分隔）。**优先于白名单**：同时命中时拒绝。"""

    internal_hosts: str = ""
    """平台内部依赖的**精确主机名**（逗号分隔），豁免私网判定。

    只放行列出的服务，不是"整个内网"。部署侧要列的是 Dapr/Redis/PG/Jaeger/
    search-gateway 这类 compose 服务名。
    """

    allowed_ports: str = "443"
    """允许的端口（逗号分隔）。默认只有 443；内网服务（如 search-gateway:8800）
    需要在这里显式加端口——豁免私网判定不等于豁免端口。"""

    model_exempt: bool = True
    """模型流量是否豁免私网判定（ADR-034 §3「甲」）。

    默认开：Ollama 与使用者自建的内网 OpenAI 兼容网关是既有能力，一并拦掉等于自停。
    豁免的只是**私网判定**，scheme / 端口 / 域名黑名单照样生效。
    """

    max_redirects: int = 5
    timeout_seconds: float = 8.0
    max_response_bytes: int = 1_000_000
    """响应体上限：出网是旁路，不能让一个超大响应把工具观测撑爆。"""

    proxy_url: str = ""
    """强制出网代理（如 `http://egress-proxy:8888`）。

    设置后：应用侧**不再自己做 DNS 解析与私网判定**（那段交给代理，它才是硬边界），
    但仍然执行 scheme / 端口 / 域名白黑名单；HTTP(S) 客户端一律走代理，
    因此可以配合"容器在 internal 网络里没有默认路由"把直连从物理上断掉。

    留空表示没有代理：这时应用侧自己做完整判定并**把连接钉在解析出的 IP 上**
    （ADR-034 §2 的默认路径）。
    """


@lru_cache
def get_egress_settings() -> EgressSettings:
    return EgressSettings()

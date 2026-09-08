"""集中管理 Dapr 运行时相关常量与配置。

设计文档 `doc/dapr-integration.md` 第 7 节规定：`app/core` 负责
"Dapr app-id、组件名、State Store 配置"。

本模块的所有默认值与部署配置一一对应，来源如下（改动时需同步）：

- `deploy/compose.yaml`                -> APP_ID / DAPR_HTTP_PORT / DAPR_GRPC_PORT
- `deploy/dapr/components/statestore.yaml` -> STATE_STORE_NAME
- `deploy/dapr/components/pubsub.yaml`     -> PUBSUB_NAME

支持通过环境变量覆盖（统一使用 ``DAPR_`` 前缀），便于本地开发时对接
非容器化的 Dapr sidecar。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class DaprSettings(BaseSettings):
    """Dapr 运行时配置，环境变量统一使用 ``DAPR_`` 前缀。"""

    model_config = SettingsConfigDict(
        env_prefix="DAPR_",
        env_file=".env",
        extra="ignore",
    )

    # 应用在 Dapr 侧车中的唯一标识。
    # 来源：deploy/compose.yaml 的 ``--app-id backend``。
    app_id: str = "backend"

    # Dapr sidecar 的 HTTP / gRPC 端口。
    # 来源：deploy/compose.yaml 的 ``--dapr-http-port 3500``。
    dapr_http_port: int = 3500
    dapr_grpc_port: int = 50001

    # 状态存储组件名。
    # 来源：deploy/dapr/components/statestore.yaml 的 ``metadata.name: statestore``。
    state_store_name: str = "statestore"

    # Pub/Sub 组件名。
    # 来源：deploy/dapr/components/pubsub.yaml 的 ``metadata.name: pubsub``。
    pubsub_name: str = "pubsub"

    # 应用自定义状态在 State Store 中的 key 前缀。
    #
    # 背景：statestore.yaml 里 ``keyPrefix: none``，即 Dapr 不会自动在 key 前
    # 加 app-id。为避免应用写业务状态（幂等键、活动结果缓存）时与 Dapr
    # Workflow 内部使用的 key 冲突，这里统一用一个前缀隔离。
    state_store_key_prefix: str = "agentrun"


@lru_cache
def get_dapr_settings() -> DaprSettings:
    """返回单例 DaprSettings（结果被缓存，进程内只初始化一次）。"""
    return DaprSettings()


# 常用常量：模块级便捷引用，默认取 DaprSettings 的值。
# 若通过环境变量覆盖，请改用 get_dapr_settings()。
APP_ID = get_dapr_settings().app_id
DAPR_HTTP_PORT = get_dapr_settings().dapr_http_port
DAPR_GRPC_PORT = get_dapr_settings().dapr_grpc_port
STATE_STORE_NAME = get_dapr_settings().state_store_name
PUBSUB_NAME = get_dapr_settings().pubsub_name
STATE_STORE_KEY_PREFIX = get_dapr_settings().state_store_key_prefix

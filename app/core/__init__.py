"""`app/core`：集中管理 Dapr 运行时配置。

设计文档 doc/dapr-integration.md 第 7 节约定，本模块负责：
Dapr app-id、组件名、State Store 配置。

用法::

    from app.core.dapr import APP_ID, STATE_STORE_NAME, get_dapr_settings

    dapr = get_dapr_settings()
    print(dapr.app_id, dapr.dapr_http_port)
"""

from app.core.dapr import (
    APP_ID,
    DAPR_GRPC_PORT,
    DAPR_HTTP_PORT,
    PUBSUB_NAME,
    STATE_STORE_KEY_PREFIX,
    STATE_STORE_NAME,
    DaprSettings,
    get_dapr_settings,
)

__all__ = [
    "APP_ID",
    "DAPR_GRPC_PORT",
    "DAPR_HTTP_PORT",
    "PUBSUB_NAME",
    "STATE_STORE_KEY_PREFIX",
    "STATE_STORE_NAME",
    "DaprSettings",
    "get_dapr_settings",
]

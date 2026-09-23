"""内置工具的运行期覆盖：搜索渠道（ADR-039、`doc/api.md` §5.24）。

职责边界：

- 覆盖值存储 → `app/core/checkpoint.py::ToolConfigRecord`（成员 B 的表）；
- 合并规则与校验 → 本模块；
- HTTP 契约与权限边界 → `app/api/main.py`（成员 D）；
- 生效点 → `app/tools/config.py::get_tool_settings`（构造内置工具时读一次）。

## 为什么要有这一层

ADR-037 把搜索渠道做成了 `TOOL_SEARCH_PROVIDER` 这一个**环境变量**，于是换渠道必须改
`.env` 再重建容器（env 只在容器创建时注入）。结果是「豆包 key 没配」只能靠报错发现，
界面上既看不到当前渠道、也换不了。本模块把渠道提升为**可运行期读写的覆盖值**，
环境变量退化为兜底默认。

## 合并规则

**生效值 = 环境配置（`TOOL_*`） ← 覆盖行中非空的字段。**

- `search_provider`：覆盖非空且合法即生效，否则跟随环境；
- `search_endpoint`：覆盖非空即生效，否则跟随环境；
- `search_api_key`：覆盖非空即生效，否则跟随环境。

三个字段都是「非空才算覆盖」，因此**空串与 `None` 同义**（= 没有覆盖）。这点与
`app/core/provider_config.py` 的 legacy 五列一致，但那里 `None` 表示"清除覆盖"而
这里是"没有覆盖"——因为本表只有单层覆盖，没有"清除后回到哪一层"的问题。

由此还有一条**判据上的坑**：清空覆盖（用户点「恢复环境配置」）之后，行**还在**，
只是三列都成了 `None`。此时 `bool(row)` 仍是真，于是「行存在 ⇒ 已覆盖环境配置」
会把界面永久钉在"已覆盖"上——覆盖值一个都没有，用户却再也回不到"跟随环境配置"、
「恢复环境配置」按钮也永远点不完。所以**判"有没有覆盖"必须看三列，不看行在不在**
（`_has_override`），这与"读到了点什么"是两件事。

## 换渠道必须同时换端点（本模块最容易踩的坑）

`search_endpoint` 只是**地址**，解析契约由 `search_provider` 决定：`duckduckgo` 走
GET + `Abstract/RelatedTopics`，`volcengine` 走 POST + `Bearer` + `Result.WebResults`。
而部署期的环境端点恰恰是给**某一条**渠道用的（`deploy/.env` 里
`TOOL_SEARCH_ENDPOINT=http://search-gateway:8800/search` 就是本地网关的 DuckDuckGo 契约）。

所以写入时有一条**额外规则**：`provider` 改成与当前生效渠道不同、且调用方没有给
`endpoint` 时，把端点一并落库。落哪个由 `_endpoint_for_switch` 决定：

1. 环境渠道就是新渠道 → 沿用**环境端点**（它一定是给这条渠道用的，
   所以「从豆包切回 DuckDuckGo」会正确落回本地网关，而不是没被墙的公网 DDG）；
2. 否则 → 该渠道的**官方默认端点**。

不这么做的话，「从本地网关切到豆包」会留下网关的地址——拿着豆包的 key 去打一个只认
DuckDuckGo 契约的网关，必然报解析错，而错误信息会指向完全无关的地方。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.observability.logging import get_logger, log_event
from app.tools.config import (
    DOUBAO_SEARCH_ENDPOINT,
    DUCKDUCKGO_SEARCH_ENDPOINT,
    ToolSettings,
)

logger = get_logger("tools.search_config")

ENDPOINT_MAX_LENGTH = 500
API_KEY_MAX_LENGTH = 500

SEARCH_CHANNELS: tuple[dict[str, Any], ...] = (
    {
        "id": "duckduckgo",
        "label": "DuckDuckGo 契约",
        "needs_api_key": False,
        "default_endpoint": DUCKDUCKGO_SEARCH_ENDPOINT,
        "description": (
            "GET + JSON（`Abstract` / `RelatedTopics`）。无需凭证；`api.duckduckgo.com` "
            "在部分网络不可达，一键部署默认指向同网络的本地网关（ADR-032），契约相同。"
        ),
    },
    {
        "id": "volcengine",
        "label": "火山引擎豆包搜索",
        "needs_api_key": True,
        "default_endpoint": DOUBAO_SEARCH_ENDPOINT,
        "description": (
            "POST + `Bearer` key（`Result.WebResults`）。检索质量最好但按量付费，"
            "必须配置 API key，否则工具直接报配置错，不发出无鉴权请求（ADR-037 §2）。"
        ),
    },
)
"""可选渠道目录：给前端下拉用，顺序即展示顺序。

`default_endpoint` 与 `app/tools/config.py` 的两个常量同源，不另写字面量——
端点改了这里必须跟着变，否则界面显示的默认地址与实际请求的地址会各说各话。
"""

ALLOWED_SEARCH_PROVIDERS: tuple[str, ...] = tuple(
    channel["id"] for channel in SEARCH_CHANNELS
)

__all__ = [
    "ALLOWED_SEARCH_PROVIDERS",
    "SEARCH_CHANNELS",
    "SearchConfigError",
    "current_provider",
    "default_search_endpoint",
    "effective_search_settings",
    "effective_search_view",
    "search_config_row",
    "update_search_config",
]


class SearchConfigError(ValueError):
    """覆盖值不合法（API 层据此返回 422）。"""


def default_search_endpoint(provider: str) -> str:
    """该渠道的官方默认端点；未知渠道回落到 DuckDuckGo。"""

    for channel in SEARCH_CHANNELS:
        if channel["id"] == provider:
            return str(channel["default_endpoint"])
    return DUCKDUCKGO_SEARCH_ENDPOINT


def search_config_row() -> dict[str, Any] | None:
    """读取覆盖行；存储不可用时记警告并返回 `None`（回退环境配置）。

    这里**不抛异常**：搜索配置读不到不该让一次协作失败。调用链在构造内置工具时，
    把异常抛出去会让「存储抖动」表现成「工具注册表构建失败」，那要难排查得多。
    """

    try:
        return checkpoint.get_tool_config()
    except Exception as exc:
        log_event(
            logger,
            "config.search.read_fallback",
            level=30,
            error=f"{type(exc).__name__}: {exc}",
            hint="回退 TOOL_* 环境配置",
        )
        return None


def _stored_provider(row: dict[str, Any] | None) -> str | None:
    """覆盖行里的渠道名；未写或非法一律按"没写"处理（防御手改过的库行）。"""

    value = (row or {}).get("search_provider")
    return value if isinstance(value, str) and value in ALLOWED_SEARCH_PROVIDERS else None


def _stored_endpoint(row: dict[str, Any] | None) -> str | None:
    value = (row or {}).get("search_endpoint")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stored_api_key(row: dict[str, Any] | None) -> str | None:
    value = (row or {}).get("search_api_key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _has_override(row: dict[str, Any] | None) -> bool:
    """这一行是否真的带来覆盖值。

    **不能**用 `bool(row)`：清空覆盖后行仍在（三列皆 `None`），行存在 != 有覆盖。
    见模块 docstring 末尾那条判据坑。
    """

    if not row:
        return False
    return bool(_stored_provider(row) or _stored_endpoint(row) or _stored_api_key(row))


def effective_search_settings(
    env: ToolSettings,
    *,
    row: Any = UNSET,
) -> ToolSettings:
    """把覆盖行合并进环境配置，返回生效的 `ToolSettings`。

    `row` 供调用方注入已读取的覆盖行；缺省时经 PostgreSQL 读取。

    端点这一路**不发明值**：覆盖为空就原样保留环境端点。换渠道时的端点一致性由
    `update_search_config` 在**写入时**解决（见模块 docstring）。
    """

    config = search_config_row() if row is UNSET else row
    if not _has_override(config):
        return env

    updates: dict[str, Any] = {}
    provider = _stored_provider(config)
    if provider and provider != env.search_provider:
        updates["search_provider"] = provider

    endpoint = _stored_endpoint(config)
    if endpoint:
        updates["search_endpoint"] = endpoint

    api_key = _stored_api_key(config)
    if api_key:
        updates["search_api_key"] = api_key

    return env.model_copy(update=updates) if updates else env


def current_provider(env: ToolSettings, *, row: Any = UNSET) -> str:
    """当前生效渠道：覆盖行优先，其次环境。界面与写入判据共用同一处口径。"""

    config = search_config_row() if row is UNSET else row
    return _stored_provider(config) or env.search_provider


def _endpoint_for_switch(provider: str, env: ToolSettings) -> str:
    """换渠道时端点落哪个，见模块 docstring「换渠道必须同时换端点」。"""

    env_endpoint = env.search_endpoint.strip()
    if env.search_provider == provider and env_endpoint:
        return env_endpoint
    return default_search_endpoint(provider)


def effective_search_view(
    env: ToolSettings,
    *,
    row: Any = UNSET,
) -> dict[str, Any]:
    """构造对外可见的生效配置（**不含 API key**，只回 `api_key_configured`）。

    `env` 必须是**纯环境配置**（`app/tools/config.py::env_tool_settings`），
    不是 `get_tool_settings()`——后者已经叠过一次覆盖，拿它当基线会让
    `env_provider` / `env_endpoint` 两项显示成生效值，界面就分不清「值从哪来」。
    """

    config = search_config_row() if row is UNSET else row
    effective = effective_search_settings(env, row=config)
    overridden = _has_override(config)
    # 没有覆盖值时那两列是**上一次写入**的残留，报出去会让界面写着
    # 「最近写入 … · probe」而实际上一次覆盖都没有——所以只在真有覆盖时上报。
    updated_at = (config or {}).get("updated_at") if overridden else None

    return {
        "provider": effective.search_provider,
        "endpoint": effective.search_endpoint,
        "api_key_configured": bool(effective.search_api_key),
        "timeout_seconds": effective.search_timeout_seconds,
        "max_results": effective.search_max_results,
        "env_provider": env.search_provider,
        "env_endpoint": env.search_endpoint,
        "env_api_key_configured": bool(env.search_api_key),
        "overridden": overridden,
        "channels": [dict(channel) for channel in SEARCH_CHANNELS],
        "updated_by": (config or {}).get("updated_by") if overridden else None,
        "updated_at": (
            updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
        ),
    }


def _validate_provider(provider: Any) -> str:
    if not isinstance(provider, str):
        raise SearchConfigError("provider 必须是字符串")
    resolved = provider.strip()
    if resolved not in ALLOWED_SEARCH_PROVIDERS:
        raise SearchConfigError(
            f"provider 必须是 {' 或 '.join(ALLOWED_SEARCH_PROVIDERS)} 之一"
        )
    return resolved


def _validate_endpoint(endpoint: Any) -> str | None:
    """空串 / `None` 都表示「没有端点覆盖」；非空必须是 http(s) URL。"""

    if endpoint is None:
        return None
    if not isinstance(endpoint, str):
        raise SearchConfigError("endpoint 必须是字符串")
    resolved = endpoint.strip()
    if not resolved:
        return None
    if len(resolved) > ENDPOINT_MAX_LENGTH:
        raise SearchConfigError(f"endpoint 不能超过 {ENDPOINT_MAX_LENGTH} 个字符")
    parts = urlsplit(resolved)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SearchConfigError("endpoint 必须是 http(s) URL")
    return resolved


def _validate_api_key(api_key: Any) -> str:
    if not isinstance(api_key, str):
        raise SearchConfigError("api_key 必须是字符串")
    resolved = api_key.strip()
    if not resolved:
        raise SearchConfigError("api_key 不能为空（清除请显式传 null）")
    if len(resolved) > API_KEY_MAX_LENGTH:
        raise SearchConfigError(f"api_key 不能超过 {API_KEY_MAX_LENGTH} 个字符")
    return resolved


def update_search_config(
    *,
    provider: Any = UNSET,
    endpoint: Any = UNSET,
    api_key: Any = UNSET,
    env: ToolSettings | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """校验并写入覆盖值；返回写入后的覆盖行。

    `UNSET` 表示调用方未提供该字段（保持原值），显式 `None` 表示清除该字段的覆盖。
    写入事实源失败由调用方显式暴露（API 返回 503），本模块不做静默降级。
    """

    if provider is not UNSET and provider is not None:
        provider = _validate_provider(provider)
    if endpoint is not UNSET:
        endpoint = _validate_endpoint(endpoint)
    if api_key is not UNSET and api_key is not None:
        api_key = _validate_api_key(api_key)

    # 先校验再读旧值：非法输入不触达存储（也避免为一次 422 建立数据库连接）。
    before = search_config_row()

    if provider is not UNSET and provider is not None:
        # 端点「没给」「给了空串」「显式 null」在换渠道时是同一件事：用户表达的都是
        # 「用这个渠道的默认地址」。必须把它们一起当成缺端点，否则前端把空输入框发成
        # `endpoint: ""` 时，换渠道会留下**上一条渠道**的端点——例如从本地网关切到
        # 豆包却还在打只认 DuckDuckGo 契约的网关，报错信息会指向完全无关的地方。
        endpoint_missing = endpoint is UNSET or not endpoint
        baseline = env if env is not None else _env_baseline()
        if (
            endpoint_missing
            and baseline is not None
            and current_provider(baseline, row=before) != provider
        ):
            endpoint = _endpoint_for_switch(provider, baseline)

    row = checkpoint.upsert_tool_config(
        search_provider=provider,
        search_endpoint=endpoint,
        search_api_key=api_key,
        updated_by=actor,
    )
    log_event(
        logger,
        "config.search.updated",
        actor=actor,
        before=_snapshot(before),
        after=_snapshot(row),
    )
    _invalidate_tool_caches()
    return row


def _invalidate_tool_caches() -> None:
    """失效两层进程级缓存，让下一次构建注册表时读到新渠道（ADR-039）。

    两层都要碰：

    ① `app/tools/config.py::get_tool_settings` 是 `@lru_cache` 的；
    ② `app/mcp/registry.py::build_tool_registry` 按进程缓存**已构造好的工具实例**，
       而 `WebSearchTool.__init__` 在构造时就把当时的 settings 存进了实例。

    只做 ① 的话，注册表里那个旧实例还攥着旧 settings——界面上「已保存」而实际不变。
    这类「保存成功但不生效」最难排查，所以两层一起失效。

    延迟 import 且失败只记日志：配置已经写进事实源了，失效通知不到不该让保存本身报错
    （与 `app/core/mcp_registry.py::_sync_orchestration_tools` 同一套做法）。
    """

    try:
        from app.tools.config import reset_tool_settings_cache

        reset_tool_settings_cache()
    except Exception as exc:
        log_event(
            logger,
            "config.search.settings_cache_reset_failed",
            level=30,
            error=f"{type(exc).__name__}: {exc}",
            hint="需重启 backend 才能让新渠道生效",
        )

    try:
        from app.mcp.registry import reset_tool_registry_cache

        reset_tool_registry_cache()
    except Exception as exc:
        log_event(
            logger,
            "config.search.registry_cache_reset_failed",
            level=30,
            error=f"{type(exc).__name__}: {exc}",
            hint="需重启 backend 才能让新渠道生效",
        )


def _env_baseline() -> ToolSettings | None:
    """纯环境配置；环境配置本身非法时返回 `None`（调用方据此跳过端点联动）。"""

    try:
        return ToolSettings()
    except Exception as exc:
        log_event(
            logger,
            "config.search.env_invalid",
            level=30,
            error=f"{type(exc).__name__}: {exc}",
            hint="环境配置非法，跳过换渠道时的端点联动",
        )
        return None


def _snapshot(row: dict[str, Any] | None) -> str:
    """脱敏快照：`search_api_key` 只记 set/unset，绝不记原值。"""

    if not row:
        return "none"
    return ",".join(
        [
            f"provider={row.get('search_provider')}",
            f"endpoint={row.get('search_endpoint')}",
            f"api_key={'set' if row.get('search_api_key') else 'unset'}",
        ]
    )

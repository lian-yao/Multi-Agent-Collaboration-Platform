"""模型 Provider 配置：存储合并、Redis 镜像与校验（`doc/api.md` §5.8、ADR-014）。

职责边界：

- 覆盖值存储 → `app/core/checkpoint.py::ProviderConfigRecord`（成员 B 的表）；
- 合并规则、校验与 Redis 镜像 → 本模块；
- HTTP 契约与权限边界 → `app/api/main.py`（成员 D）。

关键约定（ADR-014）：

1. **PostgreSQL 是事实源，Redis 只是镜像**：写路径先写表，再写 `provider:config`；
   镜像写失败只记警告，不影响写入成功。
2. **读路径 Redis 优先**：命中即返回；未命中回源表并回填；两处都不可用时回退
   `AGENT_*` 环境配置并记警告，读取不阻断业务（与 ADR-013 同构）。
3. **`api_key` 不回传、不落日志**：日志快照只记 `set`/`unset`。

合并顺序：环境配置 → 本模块的存储覆盖 → `agent_configs` 的角色覆盖（ADR-013）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from app.config import AgentSettings, get_settings
from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.observability.logging import get_logger, log_event

logger = get_logger("core.provider_config")

PROVIDER_CONFIG_CACHE_KEY = "provider:config"
ALLOWED_PROVIDERS = ("openai", "ollama")
MODEL_MAX_LENGTH = 200
BASE_URL_MAX_LENGTH = 500
API_KEY_MAX_LENGTH = 500
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0

__all__ = [
    "PROVIDER_CONFIG_CACHE_KEY",
    "ProviderConfigError",
    "effective_provider_view",
    "openai_api_key",
    "provider_config_row",
    "resolve_provider_settings",
    "sanitize_base_url",
    "set_redis_factory",
    "update_provider_config",
]


class ProviderConfigError(ValueError):
    """覆盖值不合法（API 层据此返回 422）。"""


_REDIS_FACTORY: Callable[[], Any] | None = None


def set_redis_factory(factory: Callable[[], Any] | None) -> None:
    """覆盖 Redis 客户端解析方式（主要供测试注入内存替身）。"""

    global _REDIS_FACTORY
    _REDIS_FACTORY = factory


def _default_redis_client() -> Any:
    from redis import Redis

    from app.core.storage import get_storage_settings

    return Redis.from_url(get_storage_settings().redis_url, decode_responses=True)


def _redis_client() -> Any:
    factory = _REDIS_FACTORY or _default_redis_client
    return factory()


def _read_mirror() -> dict[str, Any] | None:
    """读 Redis 镜像；不可用或内容损坏时记警告并返回 None（回源事实源）。"""

    try:
        raw = _redis_client().get(PROVIDER_CONFIG_CACHE_KEY)
    except Exception as exc:
        log_event(
            logger,
            "config.provider.mirror_read_failed",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        cached = json.loads(raw)
    except (TypeError, ValueError) as exc:
        log_event(
            logger,
            "config.provider.mirror_invalid",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    return cached if isinstance(cached, dict) else None


def _write_mirror(row: dict[str, Any]) -> None:
    """把配置镜像进 Redis；失败只记警告（镜像可丢失，事实源已落库）。"""

    payload = json.dumps(_serializable(row), ensure_ascii=False)
    try:
        _redis_client().set(PROVIDER_CONFIG_CACHE_KEY, payload)
    except Exception as exc:
        log_event(
            logger,
            "config.provider.mirror_write_failed",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="配置已写入 PostgreSQL，Redis 镜像缺失不影响生效",
        )


def _serializable(row: dict[str, Any]) -> dict[str, Any]:
    updated_at = row.get("updated_at")
    return {
        "provider": row.get("provider"),
        "model": row.get("model"),
        "base_url": row.get("base_url"),
        "api_key": row.get("api_key"),
        "temperature": row.get("temperature"),
        "updated_by": row.get("updated_by"),
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }


def provider_config_row() -> dict[str, Any] | None:
    """返回运行期覆盖行：Redis 优先，未命中回源 PostgreSQL 并回填。"""

    cached = _read_mirror()
    if cached is not None:
        return cached

    try:
        row = checkpoint.get_provider_config()
    except Exception as exc:
        log_event(
            logger,
            "config.provider.read_fallback",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="回退 AGENT_* 环境配置",
        )
        return None
    if row is not None:
        _write_mirror(row)
    return row


def sanitize_base_url(base_url: str | None) -> str | None:
    """去掉用户信息、query、fragment，避免把凭据带进响应。"""

    if not base_url:
        return None
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _model_field(provider: str) -> str:
    return "ollama_model" if provider == "ollama" else "openai_model"


def openai_api_key(settings: AgentSettings) -> str:
    """解析生效的 API 凭据：存储/环境配置优先，其次标准 `OPENAI_API_KEY`。"""

    return settings.openai_api_key or os.getenv("OPENAI_API_KEY", "")


def _base_url_field(provider: str) -> str:
    return "ollama_base_url" if provider == "ollama" else "openai_base_url"


def resolve_provider_settings(
    base: AgentSettings | None = None,
    *,
    row: dict[str, Any] | None = UNSET,  # type: ignore[assignment]
) -> AgentSettings:
    """把运行期 Provider 覆盖值合并进环境配置，返回生效配置。

    `row` 供调用方注入已读取的覆盖行；缺省时经 Redis/PostgreSQL 读取。
    """

    resolved = base or get_settings()
    config = provider_config_row() if row is UNSET else row
    if not config:
        return resolved

    updates: dict[str, Any] = {}
    provider = config.get("provider") or resolved.llm_provider
    if provider != resolved.llm_provider:
        updates["llm_provider"] = provider

    model = config.get("model")
    if model:
        updates[_model_field(provider)] = model

    base_url = config.get("base_url")
    if base_url:
        updates[_base_url_field(provider)] = base_url

    api_key = config.get("api_key")
    if api_key:
        updates["openai_api_key"] = api_key

    temperature = config.get("temperature")
    if temperature is not None:
        updates["temperature"] = temperature

    return resolved.model_copy(update=updates) if updates else resolved


def effective_provider_view(
    settings: AgentSettings,
    *,
    row: dict[str, Any] | None = UNSET,  # type: ignore[assignment]
) -> dict[str, Any]:
    """构造对外可见的生效配置（不含密钥）。"""

    config = provider_config_row() if row is UNSET else row
    provider = settings.llm_provider
    model = settings.ollama_model if provider == "ollama" else settings.openai_model
    base_url = settings.ollama_base_url if provider == "ollama" else settings.openai_base_url
    updated_at = (config or {}).get("updated_at")
    return {
        "provider": provider,
        "model": model,
        "base_url": sanitize_base_url(base_url),
        "temperature": settings.temperature,
        "api_key_configured": bool(openai_api_key(settings)),
        "updated_by": (config or {}).get("updated_by"),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def _validate_provider(provider: Any) -> str:
    if not isinstance(provider, str):
        raise ProviderConfigError("provider 必须是字符串")
    resolved = provider.strip()
    if resolved not in ALLOWED_PROVIDERS:
        raise ProviderConfigError(
            f"provider 必须是 {' 或 '.join(ALLOWED_PROVIDERS)}"
        )
    return resolved


def _validate_model(model: Any) -> str:
    if not isinstance(model, str):
        raise ProviderConfigError("model 必须是字符串")
    resolved = model.strip()
    if not resolved:
        raise ProviderConfigError("model 不能为空")
    if len(resolved) > MODEL_MAX_LENGTH:
        raise ProviderConfigError(f"model 不能超过 {MODEL_MAX_LENGTH} 个字符")
    return resolved


def _validate_base_url(base_url: Any) -> str | None:
    if not isinstance(base_url, str):
        raise ProviderConfigError("base_url 必须是字符串")
    resolved = base_url.strip()
    if not resolved:
        return None
    if len(resolved) > BASE_URL_MAX_LENGTH:
        raise ProviderConfigError(f"base_url 不能超过 {BASE_URL_MAX_LENGTH} 个字符")
    parts = urlsplit(resolved)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ProviderConfigError("base_url 必须是 http(s) URL")
    return resolved


def _validate_api_key(api_key: Any) -> str:
    if not isinstance(api_key, str):
        raise ProviderConfigError("api_key 必须是字符串")
    resolved = api_key.strip()
    if not resolved:
        raise ProviderConfigError("api_key 不能为空（清除请显式传 null）")
    if len(resolved) > API_KEY_MAX_LENGTH:
        raise ProviderConfigError(f"api_key 不能超过 {API_KEY_MAX_LENGTH} 个字符")
    return resolved


def _validate_temperature(temperature: Any) -> float:
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ProviderConfigError("temperature 必须是数值")
    resolved = float(temperature)
    if resolved < TEMPERATURE_MIN or resolved > TEMPERATURE_MAX:
        raise ProviderConfigError(
            f"temperature 必须在 {TEMPERATURE_MIN}–{TEMPERATURE_MAX} 之间"
        )
    return resolved


def update_provider_config(
    *,
    provider: Any = UNSET,
    model: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    temperature: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    """校验并写入覆盖值，随后刷新 Redis 镜像；返回写入后的覆盖行。

    `UNSET` 表示调用方未提供该字段（保持原值），显式 `None` 表示清除覆盖。
    写入事实源失败由调用方显式暴露（API 返回 503），本模块不做静默降级。
    """

    if provider is not UNSET and provider is not None:
        provider = _validate_provider(provider)
    if model is not UNSET and model is not None:
        model = _validate_model(model)
    if base_url is not UNSET and base_url is not None:
        base_url = _validate_base_url(base_url)
    if api_key is not UNSET and api_key is not None:
        api_key = _validate_api_key(api_key)
    if temperature is not UNSET and temperature is not None:
        temperature = _validate_temperature(temperature)

    # 先校验再读旧值：非法输入不触达存储（也避免为一次 422 建立数据库连接）。
    before = provider_config_row()
    row = checkpoint.upsert_provider_config(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        updated_by=actor,
    )
    _write_mirror(row)
    log_event(
        logger,
        "config.provider.updated",
        actor=actor,
        before=_snapshot(before),
        after=_snapshot(row),
    )
    return row


def _snapshot(row: dict[str, Any] | None) -> str:
    """脱敏快照：`api_key` 只记 set/unset，绝不记原值。"""

    if not row:
        return "none"
    return ",".join(
        [
            f"provider={row.get('provider')}",
            f"model={row.get('model')}",
            f"base_url={'set' if row.get('base_url') else 'unset'}",
            f"api_key={'set' if row.get('api_key') else 'unset'}",
            f"temperature={row.get('temperature')}",
        ]
    )

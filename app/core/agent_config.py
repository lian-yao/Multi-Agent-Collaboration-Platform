"""Agent 配置覆盖：读时合并与写入校验（`doc/api.md` §5.7、ADR-013、ADR-017）。

职责边界：

- 覆盖值存储 → `app/core/checkpoint.py::AgentConfigRecord`（成员 B 的表）；
- 合并规则与校验 → 本模块；
- HTTP 契约与权限边界 → `app/api/main.py`（成员 D）。

关键约定（ADR-013、ADR-017）：

1. 只存覆盖字段，列值为 NULL 表示回退下一层配置；
2. 读取失败回退环境配置并记日志（配置读取不阻断业务）；
3. 写入失败由调用方显式暴露（API 返回 503），本模块不做静默降级。

合并顺序（低 → 高，逐字段回退）：
环境配置 → `provider_configs.default_llm_model_id` → `provider_configs` legacy 五列
→ 本模块的角色覆盖。角色覆盖里 `llm_model_id` 先解析成 Provider 端点与特化参数，
再被同一行的 `model` / `temperature` / `top_p` / `max_output_tokens` / `reasoning_type`
逐项覆盖。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import AgentSettings, get_settings
from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.core.provider_config import resolve_provider_settings
from app.observability.logging import get_logger, log_event

logger = get_logger("core.agent_config")

MODEL_MAX_LENGTH = 200
MODEL_ID_MAX_LENGTH = 80
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0
TOP_P_MIN = 0.0
TOP_P_MAX = 1.0
REASONING_TYPES = ("none", "openai", "gemini", "anthropic")

OVERRIDE_FIELDS = (
    "model",
    "temperature",
    "llm_model_id",
    "top_p",
    "max_output_tokens",
    "reasoning_type",
)
"""可覆盖字段名；`override_keys`（§5.7）就是其中当前非 NULL 的那些。"""

__all__ = [
    "AgentConfigError",
    "OVERRIDE_FIELDS",
    "agent_config_overrides",
    "effective_override_keys",
    "list_agent_registry",
    "get_agent_registry",
    "create_agent_registry",
    "delete_agent_registry",
    "resolve_agent_settings",
    "update_agent_config",
]


class AgentConfigError(ValueError):
    """覆盖值不合法（API 层据此返回 422）。"""


def _validate_model(model: str) -> str:
    if not isinstance(model, str):
        raise AgentConfigError("model 必须是字符串")
    resolved = model.strip()
    if not resolved:
        raise AgentConfigError("model 不能为空")
    if len(resolved) > MODEL_MAX_LENGTH:
        raise AgentConfigError(f"model 不能超过 {MODEL_MAX_LENGTH} 个字符")
    return resolved


def _validate_llm_model_id(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentConfigError("llm_model_id 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise AgentConfigError("llm_model_id 不能为空（清除请显式传 null）")
    if len(resolved) > MODEL_ID_MAX_LENGTH:
        raise AgentConfigError(f"llm_model_id 不能超过 {MODEL_ID_MAX_LENGTH} 个字符")
    if not _model_exists(resolved):
        raise AgentConfigError(f"llm_model_id 指向的模型不存在：{resolved}")
    return resolved


def _model_exists(model_id: str) -> bool:
    """注册表读不到时不拦截写入：最终写入会因存储不可用返回 503，
    用一次读取失败换取「模型不存在」的假象更难排查（与 provider_config 同构）。"""

    try:
        return checkpoint.get_llm_model(model_id) is not None
    except Exception as exc:
        log_event(
            logger,
            "config.agent.model_check_skipped",
            level=logging.WARNING,
            llm_model_id=model_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return True


def _validate_temperature(temperature: float) -> float:
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise AgentConfigError("temperature 必须是数值")
    resolved = float(temperature)
    if resolved < TEMPERATURE_MIN or resolved > TEMPERATURE_MAX:
        raise AgentConfigError(
            f"temperature 必须在 {TEMPERATURE_MIN}–{TEMPERATURE_MAX} 之间"
        )
    return resolved


def _validate_top_p(top_p: float) -> float:
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)):
        raise AgentConfigError("top_p 必须是数值")
    resolved = float(top_p)
    if resolved < TOP_P_MIN or resolved > TOP_P_MAX:
        raise AgentConfigError(f"top_p 必须在 {TOP_P_MIN}–{TOP_P_MAX} 之间")
    return resolved


def _validate_max_output_tokens(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigError("max_output_tokens 必须是整数")
    if value < 1:
        raise AgentConfigError("max_output_tokens 必须 ≥ 1")
    return value


def _validate_reasoning_type(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentConfigError("reasoning_type 必须是字符串")
    resolved = value.strip()
    if resolved not in REASONING_TYPES:
        raise AgentConfigError(
            f"reasoning_type 必须是 {' 或 '.join(REASONING_TYPES)}"
        )
    return resolved


def agent_config_overrides() -> dict[str, dict[str, Any]]:
    """返回按角色索引的覆盖行；读取失败时返回空表并记一次警告。"""

    try:
        rows = checkpoint.list_agent_configs()
    except Exception as exc:  # 配置读取失败不能阻断业务（ADR-013）
        log_event(
            logger,
            "config.read_fallback",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="回退 AGENT_* 环境配置",
        )
        return {}
    return {row["agent_id"]: row for row in rows}


def list_agent_registry() -> list[dict[str, Any]]:
    """角色目录全量（内置 + 自定义）。读取失败返回空表，不阻断配置页。"""

    try:
        return checkpoint.list_agent_registry()
    except Exception as exc:
        log_event(
            logger,
            "config.agent.registry_read_fallback",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="角色目录回退为空",
        )
        return []


def get_agent_registry(agent_id: str) -> dict[str, Any] | None:
    return checkpoint.get_agent_registry(agent_id)


def create_agent_registry(
    *,
    agent_id: str,
    name: str,
    role: str,
    description: str | None = None,
    system_prompt: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return checkpoint.create_agent_registry(
        agent_id=agent_id,
        name=name,
        role=role,
        description=description,
        system_prompt=system_prompt,
        enabled=enabled,
    )


def delete_agent_registry(agent_id: str) -> bool:
    return checkpoint.delete_agent_registry(agent_id)


def effective_override_keys(row: dict[str, Any] | None) -> list[str]:
    """该角色当前**被显式覆盖**的字段名（§5.7 的 `override_keys`）。"""

    if not row:
        return []
    return [field for field in OVERRIDE_FIELDS if row.get(field) is not None]


def resolve_agent_settings(
    agent_id: str,
    base: AgentSettings | None = None,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    provider_row: dict[str, Any] | None = UNSET,
) -> AgentSettings:
    """把默认路由、legacy 覆盖与角色覆盖依次合并进环境配置，返回生效配置。

    `overrides` 用于调用方（例如 API 进程或测试）注入已读取的覆盖行，
    缺省时从 `agent_configs` 读取；`provider_row` 同理，缺省时经
    `app/core/provider_config.py` 读取（Redis → PostgreSQL → 环境回退）。
    """

    resolved = resolve_provider_settings(base or get_settings(), row=provider_row)
    rows = agent_config_overrides() if overrides is None else overrides
    override = rows.get(agent_id)
    if not override:
        return resolved

    updates: dict[str, Any] = {}

    # 1) 注册表引用：重新指向该模型所属 Provider 的端点、凭据与特化参数。
    llm_model_id = override.get("llm_model_id")
    if llm_model_id:
        registry_updates = _registry_overrides(str(llm_model_id))
        if registry_updates:
            resolved = resolved.model_copy(update=registry_updates)

    # 2) 同行的逐字段覆盖：优先级最高。
    model = override.get("model")
    temperature = override.get("temperature")
    top_p = override.get("top_p")
    max_output_tokens = override.get("max_output_tokens")
    reasoning_type = override.get("reasoning_type")
    if model:
        updates[_model_field(resolved)] = model
    if temperature is not None:
        updates["temperature"] = temperature
    if top_p is not None:
        updates["top_p"] = top_p
    if max_output_tokens is not None:
        updates["max_tokens"] = max_output_tokens
    if reasoning_type is not None:
        updates["reasoning_type"] = reasoning_type
    return resolved.model_copy(update=updates) if updates else resolved


def _registry_overrides(model_id: str) -> dict[str, Any]:
    """注册表读取失败或引用悬空时返回空字典（不阻断阶段执行）。"""

    from app.core.model_registry import resolve_provider_settings_from_model

    try:
        resolved = resolve_provider_settings_from_model(model_id)
    except Exception as exc:
        log_event(
            logger,
            "config.agent.model_unavailable",
            level=logging.WARNING,
            llm_model_id=model_id,
            error=f"{type(exc).__name__}: {exc}",
            hint="回退环境配置",
        )
        return {}
    if resolved is None:
        log_event(
            logger,
            "config.agent.model_dangling",
            level=logging.WARNING,
            llm_model_id=model_id,
            hint="条目或其 Provider 不存在，按未绑定处理",
        )
        return {}
    return resolved[0]


def _model_field(settings: AgentSettings) -> str:
    """覆盖值落在当前 provider 对应的模型字段上（provider 不通过本模块修改）。"""

    return "ollama_model" if settings.llm_provider == "ollama" else "openai_model"


def update_agent_config(
    agent_id: str,
    *,
    model: Any = UNSET,
    temperature: Any = UNSET,
    llm_model_id: Any = UNSET,
    top_p: Any = UNSET,
    max_output_tokens: Any = UNSET,
    reasoning_type: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    """校验并写入覆盖值，成功后记审计日志；返回写入后的覆盖行。

    `UNSET` 表示调用方未提供该字段（保持原值），显式 `None` 表示清除覆盖。
    """

    before = checkpoint.get_agent_config(agent_id)
    if model is not UNSET and model is not None:
        model = _validate_model(model)
    if temperature is not UNSET and temperature is not None:
        temperature = _validate_temperature(temperature)
    if llm_model_id is not UNSET and llm_model_id is not None:
        llm_model_id = _validate_llm_model_id(llm_model_id)
    if top_p is not UNSET and top_p is not None:
        top_p = _validate_top_p(top_p)
    if max_output_tokens is not UNSET and max_output_tokens is not None:
        max_output_tokens = _validate_max_output_tokens(max_output_tokens)
    if reasoning_type is not UNSET and reasoning_type is not None:
        reasoning_type = _validate_reasoning_type(reasoning_type)

    row = checkpoint.upsert_agent_config(
        agent_id,
        model=model,
        temperature=temperature,
        llm_model_id=llm_model_id,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        reasoning_type=reasoning_type,
        updated_by=actor,
    )
    log_event(
        logger,
        "config.agent.updated",
        agent_id=agent_id,
        actor=actor,
        before=_snapshot(before),
        after=_snapshot(row),
    )
    return row


def _snapshot(row: dict[str, Any] | None) -> str:
    if not row:
        return "none"
    return ",".join(
        f"{field}={row.get(field)}" for field in OVERRIDE_FIELDS
    )

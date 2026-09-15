"""Agent 配置覆盖：读时合并与写入校验（`doc/api.md` §5.7、ADR-013）。

职责边界：

- 覆盖值存储 → `app/core/checkpoint.py::AgentConfigRecord`（成员 B 的表）；
- 合并规则与校验 → 本模块；
- HTTP 契约与权限边界 → `app/api/main.py`（成员 D）。

关键约定（ADR-013）：

1. 只存覆盖字段，列值为 NULL 表示回退环境配置；
2. 读取失败回退环境配置并记日志（配置读取不阻断业务）；
3. 写入失败由调用方显式暴露（API 返回 503），本模块不做静默降级。
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
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0

__all__ = [
    "AgentConfigError",
    "agent_config_overrides",
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


def _validate_temperature(temperature: float) -> float:
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise AgentConfigError("temperature 必须是数值")
    resolved = float(temperature)
    if resolved < TEMPERATURE_MIN or resolved > TEMPERATURE_MAX:
        raise AgentConfigError(
            f"temperature 必须在 {TEMPERATURE_MIN}–{TEMPERATURE_MAX} 之间"
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


def resolve_agent_settings(
    agent_id: str,
    base: AgentSettings | None = None,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    provider_row: dict[str, Any] | None = UNSET,
) -> AgentSettings:
    """把运行期 Provider 配置与角色覆盖值依次合并进环境配置，返回生效配置。

    `overrides` 用于调用方（例如 API 进程或测试）注入已读取的覆盖行，
    缺省时从 `agent_configs` 读取；`provider_row` 同理，缺省时经
    `app/core/provider_config.py` 读取（Redis → PostgreSQL → 环境回退）。

    合并顺序：环境配置 → Provider 覆盖（ADR-014）→ 角色覆盖（ADR-013）。
    """

    resolved = resolve_provider_settings(base or get_settings(), row=provider_row)
    rows = agent_config_overrides() if overrides is None else overrides
    override = rows.get(agent_id)
    if not override:
        return resolved

    updates: dict[str, Any] = {}
    model = override.get("model")
    temperature = override.get("temperature")
    if model:
        updates[_model_field(resolved)] = model
    if temperature is not None:
        updates["temperature"] = temperature
    return resolved.model_copy(update=updates) if updates else resolved


def _model_field(settings: AgentSettings) -> str:
    """覆盖值落在当前 provider 对应的模型字段上（provider 不通过 API 修改）。"""

    return "ollama_model" if settings.llm_provider == "ollama" else "openai_model"


def update_agent_config(
    agent_id: str,
    *,
    model: Any = UNSET,
    temperature: Any = UNSET,
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

    row = checkpoint.upsert_agent_config(
        agent_id,
        model=model,
        temperature=temperature,
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
    return f"model={row['model']},temperature={row['temperature']}"

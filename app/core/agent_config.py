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
import re
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

NAME_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 1000
SYSTEM_PROMPT_MAX_LENGTH = 8000
ICON_MAX_LENGTH = 32

TOOL_NAME_MAX_LENGTH = 120
TOOL_NAMES_MAX_ITEMS = 60
"""工具白名单的边界（§5.7）：单名 1–120 字符，一次最多授权 60 个工具。

上限取 60 而不是更大：`/api/v1/tools` 的目录当前是个位数到几十的量级，
给一个远超目录规模的额度只会让「拼错名字」这种错误以「配置成功但工具没出现」
的形式潜伏下来。
"""

OVERRIDE_FIELDS = (
    "model",
    "temperature",
    "llm_model_id",
    "top_p",
    "max_output_tokens",
    "reasoning_type",
    "tool_names",
)
"""可覆盖字段名；`override_keys`（§5.7）就是其中当前非 NULL 的那些。

`tool_names` 与其余六个字段的语义差别：那六个是「值」，逐字段回退下一层；
`tool_names` 是「授权边界」，NULL 表示不限制（沿用全量注册表），
因此它不参与 `resolve_agent_settings` 的层级合并，只被 `resolve_agent_tools` 读取。
"""

__all__ = [
    "AgentConfigError",
    "OVERRIDE_FIELDS",
    "AgentProfileError",
    "dispatchable_agents",
    "agent_config_overrides",
    "effective_override_keys",
    "list_agent_registry",
    "get_agent_registry",
    "create_agent_registry",
    "delete_agent_registry",
    "resolve_agent_settings",
    "resolve_agent_tools",
    "update_agent_config",
    "update_agent_registry",
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



def _validate_tool_names(value: Any) -> list[str]:
    """校验工具白名单：字符串数组、去重保序、单名与总量都有上限。

    **不去目录里核名字是否真实存在**，与 `llm_model_id` 的做法刻意不同：
    那边指的是 `llm_models.id` 主键引用，悬空就是配置错误；这边指的是**逻辑名**，
    而目录是「内置工具 + 已发现的登记工具」的函数——用户完全可以先给角色授权、
    过一会儿才在配置页「发现」那个 Server 的工具。名字不存在时的表现是
    「该工具不在注册表里、模型看不到它」，而不是配置写入失败。
    """

    if not isinstance(value, (list, tuple)):
        raise AgentConfigError("tool_names 必须是字符串数组")
    resolved: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise AgentConfigError("tool_names 的每一项都必须是字符串")
        name = item.strip()
        if not name:
            raise AgentConfigError("tool_names 不能包含空名")
        if len(name) > TOOL_NAME_MAX_LENGTH:
            raise AgentConfigError(
                f"单个工具名不能超过 {TOOL_NAME_MAX_LENGTH} 个字符"
            )
        if name in seen:
            continue
        seen.add(name)
        resolved.append(name)
    if len(resolved) > TOOL_NAMES_MAX_ITEMS:
        raise AgentConfigError(f"tool_names 最多 {TOOL_NAMES_MAX_ITEMS} 项")
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


class AgentProfileError(AgentConfigError):
    """目录字段（名称 / 描述 / 系统提示 / 图标）不合法。"""


_ICON_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_icon(value: Any) -> str:
    """图标键只校验**形状**，不查枚举白名单。

    与 `tool_names` 不查工具目录同源的取舍（ADR-036）：图标集在
    `frontend/src/components/AgentGlyph.tsx`，后端跟着它维护一份枚举会把「加一个图标」
    变成两处改动，而两处一旦不同步就会出现「后端拒绝一个前端刚给的合法键」。
    形状校验挡的是拼错的大小写、空格、路径注入这类**请求**错误；语义正确性由前端
    的图标选择器保证，`AgentGlyph` 对未知键回退到按 role/name 推断，不会画成空白。
    """

    if not isinstance(value, str):
        raise AgentProfileError("icon 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise AgentProfileError("icon 不能为空（清除请显式传 null）")
    if len(resolved) > ICON_MAX_LENGTH:
        raise AgentProfileError(f"icon 不能超过 {ICON_MAX_LENGTH} 个字符")
    if not _ICON_PATTERN.match(resolved):
        raise AgentProfileError("icon 只允许小写字母、数字与下划线，且以字母开头")
    return resolved


def _validate_profile_text(
    value: Any, *, field: str, max_length: int, allow_blank: bool
) -> str | None:
    """目录文本字段的通用校验：`None` = 清空（可空字段才有意义）。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentProfileError(f"{field} 必须是字符串")
    resolved = value.strip()
    if not resolved and not allow_blank:
        raise AgentProfileError(f"{field} 不能为空")
    if len(resolved) > max_length:
        raise AgentProfileError(f"{field} 不能超过 {max_length} 个字符")
    return resolved


def update_agent_registry(
    agent_id: str,
    *,
    name: Any = UNSET,
    description: Any = UNSET,
    system_prompt: Any = UNSET,
    icon: Any = UNSET,
    enabled: Any = UNSET,
) -> dict[str, Any] | None:
    """更新角色**目录**字段（名称 / 描述 / 系统提示 / 图标 / 启停），返回更新后的条目。

    三态照 `update_agent_config` 的哨兵惯例，两组端点因此**读起来一致**：

    - **缺省（`UNSET`）** = 不改动；
    - **显式 `None`** = 清除，只对可空列（`description` / `system_prompt` / `icon`）
      成立；`name` / `enabled` 传 `None` 直接报错——它们没有「空值」这个状态。
    - **值** = 写入。

    与覆盖组（`update_agent_config`）的差别不在 `None` 的含义，而在 `None` 的**后果**：
    覆盖组清掉一个字段会**回退到下一层**（默认路由 / 环境配置），目录组清掉一个字段
    就是**那一列变空**，没有回退链。界面上要给出不同的提示（「恢复为环境配置」对
    「清空」是错的），两组字段写的是两张表（`agent_configs` 对 `agent_registry`），
    所以走两个端点而不是挤进同一个 PATCH（ADR-036）。

    传空串要分两说：`description` / `system_prompt` 传空串等价于清空（存 `None`）——
    「有提示词」与「提示词是空串」在执行期没有区别；`name` 传空串报错，一个没有名字的
    角色在画布上会变成空白节点。

    角色不存在返回 ``None``（上层转 404）。**内置角色也允许改**：ADR-036 把三个内置
    角色降级成普通种子，提示词与启停都可改；只有删除仍受保护。
    """

    payload: dict[str, Any] = {}
    if name is not UNSET:
        if name is None:
            raise AgentProfileError("name 不能为空")
        payload["name"] = _validate_profile_text(
            name, field="name", max_length=NAME_MAX_LENGTH, allow_blank=False
        )
    if description is not UNSET:
        payload["description"] = _validate_profile_text(
            description,
            field="description",
            max_length=DESCRIPTION_MAX_LENGTH,
            allow_blank=True,
        )
    if system_prompt is not UNSET:
        payload["system_prompt"] = _validate_profile_text(
            system_prompt,
            field="system_prompt",
            max_length=SYSTEM_PROMPT_MAX_LENGTH,
            allow_blank=True,
        )
    if icon is not UNSET:
        payload["icon"] = None if icon is None else _validate_icon(icon)
    if enabled is not UNSET:
        if enabled is None:
            raise AgentProfileError("enabled 不能为空")
        if not isinstance(enabled, bool):
            raise AgentProfileError("enabled 必须是布尔值")
        payload["enabled"] = enabled

    if not payload:
        raise AgentProfileError(
            "至少需要提供 name/description/system_prompt/icon/enabled 之一"
        )
    return checkpoint.update_agent_registry(agent_id, **payload)


def dispatchable_agents() -> list[dict[str, Any]]:
    """可被主 Agent 调度（动态编排）的角色：目录里 `enabled=True` 的全部条目。

    **这是 ADR-036 的核心接缝**：在此之前 planner 的候选集是写死的 `RoleId` 三选一
    （`app/agents/roles.py`），于是「新增了一个角色却永远不会被选中」——角色是内置的，
    不是模块化的。现在候选集来自注册表，三个内置角色只是默认 enabled 的普通条目。

    停用只作用于**动态调度**：固定三步流水线（用户显式选「固定三步」）仍会调用
    collect/analyze/report —— 那条链路的角色是拓扑本身的一部分，不是「选出来的」。
    把停用也套到静态链路上，会让「停用一个角色」等同于「静态链路直接跑不起来」，
    而用户的意图通常是「别再让主 agent 自动派给它」。

    读取失败返回空表（ADR-013：配置读取不阻断业务）。空表会让 planner 拿到零个候选，
    `generate_plan` 据此走兜底计划而不是硬编一组角色。
    """

    try:
        rows = checkpoint.list_agent_registry()
    except Exception as exc:
        log_event(
            logger,
            "config.agent.dispatch_candidates_fallback",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="调度候选集回退为空，编排将走兜底计划",
        )
        return []
    return [row for row in rows if row.get("enabled", True)]




def create_agent_registry(
    *,
    agent_id: str,
    name: str,
    role: str,
    description: str | None = None,
    system_prompt: str | None = None,
    icon: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return checkpoint.create_agent_registry(
        agent_id=agent_id,
        name=name,
        role=role,
        description=description,
        system_prompt=system_prompt,
        icon=_validate_icon(icon) if icon is not None else None,
        enabled=enabled,
    )


def delete_agent_registry(agent_id: str) -> bool:
    return checkpoint.delete_agent_registry(agent_id)


def effective_override_keys(row: dict[str, Any] | None) -> list[str]:
    """该角色当前**被显式覆盖**的字段名（§5.7 的 `override_keys`）。"""

    if not row:
        return []
    return [field for field in OVERRIDE_FIELDS if row.get(field) is not None]



def resolve_agent_tools(
    agent_id: str,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[str] | None:
    """该角色被授权使用的工具名；``None`` 表示**未配置** = 不加限制。

    与 `resolve_agent_settings` 的关键差别：工具白名单不是「可逐字段回退的数值」，
    而是一条独立的授权边界，因此**不做层级合并**——没有覆盖行，或覆盖值为 NULL，
    都读作「未配置」，执行期沿用全量注册表（与加这个字段之前的行为逐字一致）。

    返回空列表是有意义的第三种状态：「该角色一个工具都不许用」。它必须与 ``None``
    分得开，否则「显式取消全部授权」会被静默读成「回到全量」，把用户刚做的收紧
    反向放大成放开——这正是配置类字段最容易出的那一类错。

    读取失败回退「未配置」并记一次警告（ADR-013：配置读取不阻断业务）。
    """

    rows = agent_config_overrides() if overrides is None else overrides
    row = rows.get(agent_id)
    if not row:
        return None
    configured = row.get("tool_names")
    if configured is None:
        return None
    if not isinstance(configured, (list, tuple)):
        log_event(
            logger,
            "config.agent.tool_names_invalid",
            level=logging.WARNING,
            agent_id=agent_id,
            hint="覆盖值不是数组，按未配置处理",
        )
        return None
    return [str(name) for name in configured]

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
    tool_names: Any = UNSET,
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
    if tool_names is not UNSET and tool_names is not None:
        tool_names = _validate_tool_names(tool_names)

    row = checkpoint.upsert_agent_config(
        agent_id,
        model=model,
        temperature=temperature,
        llm_model_id=llm_model_id,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        reasoning_type=reasoning_type,
        tool_names=tool_names,
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

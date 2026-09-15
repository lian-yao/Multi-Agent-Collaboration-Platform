"""模型 Provider / 模型注册表（`doc/api.md` §5.9–§5.12、ADR-017）。

职责边界：

- 表结构 → `app/core/checkpoint.py` 的 `LlmProviderRecord` / `LlmModelRecord`；
- 校验、预设目录、批量导入、生效解析 → 本模块；
- HTTP 契约与错误码映射 → `app/api/main.py`；
- 远端探测 → `app/core/model_discovery.py`。

关键约定：

1. **`api_key` 只写不回读**：出参一律用 `api_key_configured: bool`（ADR-014、ADR-017 §3）。
2. **`PATCH` 的空串表示「不改动」**：避免表单重提交把已配置的密钥清空；
   显式 `null` 才是清除。
3. **批量导入幂等**：`(provider_id, model)` 已存在的条目计入 `skipped` 而不报错。
4. **逻辑引用**：`agent_configs.llm_model_id` 与 `provider_configs.default_llm_model_id`
   都不建外键；解析时悬空即按「未绑定」处理并逐字段回退。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.core.model_discovery import (
    DiscoveredModel,
    ModelDiscoveryError,
    discover_models,
)
from app.observability.logging import get_logger, log_event

logger = get_logger("core.model_registry")

PROVIDER_ID_MAX_LENGTH = 50
PROVIDER_NAME_MAX_LENGTH = 100
BASE_URL_MAX_LENGTH = 500
API_KEY_MAX_LENGTH = 500
MODEL_MAX_LENGTH = 200
MODEL_ID_MAX_LENGTH = 80
MODEL_NAME_MAX_LENGTH = 200
CUSTOM_HEADERS_MAX_ITEMS = 20
CUSTOM_HEADER_VALUE_MAX_LENGTH = 500
CUSTOM_PARAMETERS_MAX_ITEMS = 32
CUSTOM_PARAMETER_KEY_MAX_LENGTH = 100
BATCH_IMPORT_MAX_ITEMS = 200

TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 2.0
TOP_P_MIN, TOP_P_MAX = 0.0, 1.0

PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

API_TYPES = (
    "openai-compatible",
    "openai-responses",
    "anthropic",
    "gemini",
    "amazon-bedrock",
)

REASONING_TYPES = ("none", "openai", "gemini", "anthropic")

MODALITIES = ("text", "vision", "pdf")

CUSTOM_PARAMETER_TYPES = ("text", "number", "boolean", "json")

PRESET_DEFAULT_API_TYPE: dict[str, str] = {
    "openai": "openai-responses",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "amazon-bedrock": "amazon-bedrock",
}
PRESET_DEFAULT_API_TYPE_FALLBACK = "openai-compatible"

PRESET_SUPPORTED_API_TYPES: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic", "openai-compatible"),
    "gemini": ("gemini", "openai-compatible"),
    "deepseek": ("openai-compatible", "anthropic", "openai-responses"),
    "amazon-bedrock": ("amazon-bedrock", "openai-compatible"),
}
PRESET_SUPPORTED_API_TYPES_FALLBACK = (
    "openai-compatible",
    "openai-responses",
    "anthropic",
    "gemini",
)


class ProviderPreset(dict):
    """预设目录条目。

    用 dict 子类而非 dataclass：条目直接作为 JSON 响应体的一部分返回，
    避免在每个接口里做一次转换（预设目录是静态常量，无行为）。
    """


def _preset(
    preset_type: str,
    label: str,
    *,
    monogram: str,
    tint: str,
    category: str,
    default_base_url: str = "",
    requires_api_key: bool = True,
    api_key_url: str | None = None,
) -> ProviderPreset:
    default_api_type = PRESET_DEFAULT_API_TYPE.get(
        preset_type, PRESET_DEFAULT_API_TYPE_FALLBACK
    )
    supported = PRESET_SUPPORTED_API_TYPES.get(
        preset_type, PRESET_SUPPORTED_API_TYPES_FALLBACK
    )
    if default_api_type not in supported:
        supported = (default_api_type, *supported)
    return ProviderPreset(
        preset_type=preset_type,
        label=label,
        monogram=monogram,
        tint=tint,
        category=category,
        default_api_type=default_api_type,
        supported_api_types=list(supported),
        default_base_url=default_base_url,
        requires_api_key=requires_api_key,
        api_key_url=api_key_url,
        supports_model_discovery=default_api_type
        in {"openai-compatible", "openai-responses", "anthropic", "gemini"},
    )


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": _preset(
        "openai",
        "OpenAI",
        monogram="OA",
        tint="green",
        category="main",
        default_base_url="https://api.openai.com/v1",
        api_key_url="https://platform.openai.com/api-keys",
    ),
    "anthropic": _preset(
        "anthropic",
        "Anthropic",
        monogram="An",
        tint="amber",
        category="main",
        default_base_url="https://api.anthropic.com",
        api_key_url="https://console.anthropic.com/settings/keys",
    ),
    "gemini": _preset(
        "gemini",
        "Google Gemini",
        monogram="Ge",
        tint="teal",
        category="main",
        default_base_url="https://generativelanguage.googleapis.com",
        api_key_url="https://aistudio.google.com/apikey",
    ),
    "xai": _preset(
        "xai",
        "xAI Grok",
        monogram="xAI",
        tint="ink",
        category="main",
        default_base_url="https://api.x.ai/v1",
        api_key_url="https://console.x.ai",
    ),
    "mistral": _preset(
        "mistral",
        "Mistral AI",
        monogram="Mi",
        tint="rose",
        category="main",
        default_base_url="https://api.mistral.ai/v1",
        api_key_url="https://console.mistral.ai/api-keys",
    ),
    "perplexity": _preset(
        "perplexity",
        "Perplexity",
        monogram="Px",
        tint="teal",
        category="main",
        default_base_url="https://api.perplexity.ai",
    ),
    "groq": _preset(
        "groq",
        "Groq",
        monogram="Gq",
        tint="orange",
        category="main",
        default_base_url="https://api.groq.com/openai/v1",
        api_key_url="https://console.groq.com/keys",
    ),
    "together-ai": _preset(
        "together-ai",
        "Together AI",
        monogram="Tg",
        tint="indigo",
        category="main",
        default_base_url="https://api.together.xyz/v1",
        api_key_url="https://api.together.xyz/settings/api-keys",
    ),
    "cerebras": _preset(
        "cerebras",
        "Cerebras",
        monogram="Cb",
        tint="orange",
        category="main",
        default_base_url="https://api.cerebras.ai/v1",
    ),
    "deepseek": _preset(
        "deepseek",
        "DeepSeek",
        monogram="深度",
        tint="blue",
        category="cn",
        default_base_url="https://api.deepseek.com/v1",
        api_key_url="https://platform.deepseek.com/api_keys",
    ),
    "moonshot": _preset(
        "moonshot",
        "Moonshot / Kimi",
        monogram="Ki",
        tint="purple",
        category="cn",
        default_base_url="https://api.moonshot.cn/v1",
        api_key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    "zhipu": _preset(
        "zhipu",
        "智谱 GLM",
        monogram="智谱",
        tint="indigo",
        category="cn",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    "doubao": _preset(
        "doubao",
        "豆包 / 方舟",
        monogram="豆包",
        tint="rose",
        category="cn",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    "siliconflow": _preset(
        "siliconflow",
        "硅基流动",
        monogram="硅基",
        tint="blue",
        category="cn",
        default_base_url="https://api.siliconflow.cn/v1",
    ),
    "stepfun": _preset(
        "stepfun",
        "阶跃星辰",
        monogram="阶跃",
        tint="purple",
        category="cn",
        default_base_url="https://api.stepfun.com/v1",
    ),
    "minimax": _preset(
        "minimax",
        "MiniMax",
        monogram="MM",
        tint="pink",
        category="cn",
        default_base_url="https://api.minimax.chat/v1",
    ),
    "hunyuan": _preset(
        "hunyuan",
        "腾讯混元",
        monogram="混元",
        tint="teal",
        category="cn",
        default_base_url="https://api.hunyuan.cloud.tencent.com/v1",
    ),
    "openrouter": _preset(
        "openrouter",
        "OpenRouter",
        monogram="OR",
        tint="purple",
        category="gateway",
        default_base_url="https://openrouter.ai/api/v1",
        api_key_url="https://openrouter.ai/keys",
    ),
    "apimart": _preset(
        "apimart",
        "APIMart",
        monogram="AM",
        tint="ink",
        category="gateway",
        api_key_url="https://apimart.ai",
    ),
    "azure-openai": _preset(
        "azure-openai",
        "Azure OpenAI",
        monogram="Az",
        tint="blue",
        category="cloud",
    ),
    "amazon-bedrock": _preset(
        "amazon-bedrock",
        "Amazon Bedrock",
        monogram="Br",
        tint="amber",
        category="cloud",
    ),
    "ollama": _preset(
        "ollama",
        "Ollama（本地）",
        monogram="Ol",
        tint="slate",
        category="local",
        default_base_url="http://localhost:11434",
        requires_api_key=False,
    ),
    "lm-studio": _preset(
        "lm-studio",
        "LM Studio（本地）",
        monogram="LM",
        tint="slate",
        category="local",
        default_base_url="http://localhost:1234/v1",
        requires_api_key=False,
    ),
    "openai-compatible": _preset(
        "openai-compatible",
        "OpenAI 兼容（自定义）",
        monogram="自定义",
        tint="slate",
        category="custom",
    ),
}

PROVIDER_CATEGORIES: list[dict[str, str]] = [
    {"id": "all", "label": "全部"},
    {"id": "main", "label": "国际主流"},
    {"id": "cn", "label": "国内"},
    {"id": "gateway", "label": "聚合网关"},
    {"id": "cloud", "label": "云托管"},
    {"id": "local", "label": "本地"},
    {"id": "custom", "label": "自定义"},
]

__all__ = [
    "API_TYPES",
    "BATCH_IMPORT_MAX_ITEMS",
    "DuplicateEntryError",
    "MODALITIES",
    "ModelNotFoundError",
    "ModelRegistryError",
    "ProviderInUseError",
    "ProviderNotFoundError",
    "PROVIDER_PRESETS",
    "PROVIDER_CATEGORIES",
    "REASONING_TYPES",
    "batch_import_models",
    "create_model",
    "create_provider",
    "delete_model",
    "delete_provider",
    "discover_provider_models",
    "effective_model_view",
    "get_default_api_type_for_preset",
    "get_supported_api_types_for_preset",
    "list_models",
    "list_providers",
    "provider_preset_catalog",
    "resolve_llm_model",
    "resolve_provider_settings_from_model",
    "update_model",
    "update_provider",
]


class ModelRegistryError(ValueError):
    """取值不合法（API 层据此返回 422）。"""


class ProviderNotFoundError(ModelRegistryError):
    """Provider 条目不存在（API 层据此返回 404）。"""

    code = "PROVIDER_NOT_FOUND"

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"Provider 不存在：{provider_id}")
        self.provider_id = provider_id


class ModelNotFoundError(ModelRegistryError):
    """模型条目不存在（API 层据此返回 404）。"""

    code = "MODEL_NOT_FOUND"

    def __init__(self, model_id: str) -> None:
        super().__init__(f"模型条目不存在：{model_id}")
        self.model_id = model_id


class DuplicateEntryError(ModelRegistryError):
    """同类条目已存在（API 层据此返回 409 `VALIDATION_ERROR`，见 `doc/api.md` §5.9/§5.10）。"""

    code = "VALIDATION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProviderInUseError(ModelRegistryError):
    """仍有启用的模型引用该 Provider（API 层据此返回 409）。"""

    code = "PROVIDER_IN_USE"

    def __init__(self, provider_id: str, enabled_models: int) -> None:
        super().__init__(
            f"Provider 仍有 {enabled_models} 个启用中的模型，"
            f"请先停用或删除，或加 ?force=true 强制级联删除"
        )
        self.provider_id = provider_id
        self.enabled_models = enabled_models


def get_default_api_type_for_preset(preset_type: str) -> str:
    return PRESET_DEFAULT_API_TYPE.get(preset_type, PRESET_DEFAULT_API_TYPE_FALLBACK)


def get_supported_api_types_for_preset(preset_type: str) -> list[str]:
    preset = PROVIDER_PRESETS.get(preset_type)
    if preset is None:
        return list(PRESET_SUPPORTED_API_TYPES_FALLBACK)
    return list(preset["supported_api_types"])


def provider_preset_catalog() -> dict[str, Any]:
    """`GET /api/v1/config/provider-presets` 的响应体（`doc/api.md` §5.12）。"""

    return {
        "items": [dict(preset) for preset in PROVIDER_PRESETS.values()],
        "categories": [dict(category) for category in PROVIDER_CATEGORIES],
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _validate_provider_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelRegistryError("id 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise ModelRegistryError("id 不能为空")
    if len(resolved) > PROVIDER_ID_MAX_LENGTH:
        raise ModelRegistryError(f"id 不能超过 {PROVIDER_ID_MAX_LENGTH} 个字符")
    if not PROVIDER_ID_PATTERN.match(resolved):
        raise ModelRegistryError("id 只能包含字母、数字、点、下划线与短横线")
    return resolved


def _validate_text(
    value: Any, *, field: str, max_length: int, allow_empty: bool = False
) -> str | None:
    if not isinstance(value, str):
        raise ModelRegistryError(f"{field} 必须是字符串")
    resolved = value.strip()
    if not resolved and not allow_empty:
        raise ModelRegistryError(f"{field} 不能为空")
    if len(resolved) > max_length:
        raise ModelRegistryError(f"{field} 不能超过 {max_length} 个字符")
    return resolved or None


def _validate_preset_type(value: Any) -> str:
    resolved = _validate_text(value, field="preset_type", max_length=40)
    assert resolved is not None
    if resolved not in PROVIDER_PRESETS:
        raise ModelRegistryError(f"未知的 preset_type：{resolved}")
    return resolved


def _validate_api_type(value: Any) -> str:
    resolved = _validate_text(value, field="api_type", max_length=30)
    assert resolved is not None
    if resolved not in API_TYPES:
        raise ModelRegistryError(f"api_type 必须是 {' 或 '.join(API_TYPES)}")
    return resolved


def _validate_base_url(value: Any) -> str | None:
    if not isinstance(value, str):
        raise ModelRegistryError("base_url 必须是字符串")
    resolved = value.strip()
    if not resolved:
        return None
    if len(resolved) > BASE_URL_MAX_LENGTH:
        raise ModelRegistryError(f"base_url 不能超过 {BASE_URL_MAX_LENGTH} 个字符")
    if not resolved.startswith(("http://", "https://")):
        raise ModelRegistryError("base_url 必须是 http(s) URL")
    return resolved


def _validate_api_key(value: Any) -> str | None:
    if not isinstance(value, str):
        raise ModelRegistryError("api_key 必须是字符串")
    resolved = value.strip()
    if not resolved:
        return None
    if len(resolved) > API_KEY_MAX_LENGTH:
        raise ModelRegistryError(f"api_key 不能超过 {API_KEY_MAX_LENGTH} 个字符")
    return resolved


def _validate_string_map(
    value: Any, *, field: str, max_items: int, value_max_length: int
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ModelRegistryError(f"{field} 必须是对象")
    if len(value) > max_items:
        raise ModelRegistryError(f"{field} 最多 {max_items} 项")
    resolved: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ModelRegistryError(f"{field} 的键必须是非空字符串")
        if not isinstance(item, str):
            raise ModelRegistryError(f"{field} 的值必须是字符串")
        if len(item) > value_max_length:
            raise ModelRegistryError(
                f"{field} 的值不能超过 {value_max_length} 个字符"
            )
        resolved[key.strip()] = item
    return resolved


def _validate_optional_number(
    value: Any, *, field: str, minimum: float, maximum: float
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRegistryError(f"{field} 必须是数值")
    resolved = float(value)
    if resolved < minimum or resolved > maximum:
        raise ModelRegistryError(f"{field} 必须在 {minimum}–{maximum} 之间")
    return resolved


def _validate_optional_int(value: Any, *, field: str, minimum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelRegistryError(f"{field} 必须是整数")
    if value < minimum:
        raise ModelRegistryError(f"{field} 必须 ≥ {minimum}")
    return value


def _validate_reasoning_type(value: Any) -> str:
    resolved = _validate_text(value, field="reasoning_type", max_length=20)
    assert resolved is not None
    if resolved not in REASONING_TYPES:
        raise ModelRegistryError(
            f"reasoning_type 必须是 {' 或 '.join(REASONING_TYPES)}"
        )
    return resolved


def _validate_modalities(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ModelRegistryError("modalities 必须是数组")
    resolved: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in MODALITIES:
            raise ModelRegistryError(f"modalities 只能是 {' / '.join(MODALITIES)}")
        if item not in resolved:
            resolved.append(item)
    return resolved


def _validate_custom_parameters(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ModelRegistryError("custom_parameters 必须是数组")
    if len(value) > CUSTOM_PARAMETERS_MAX_ITEMS:
        raise ModelRegistryError(
            f"custom_parameters 最多 {CUSTOM_PARAMETERS_MAX_ITEMS} 项"
        )
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ModelRegistryError("custom_parameters 的每一项必须是对象")
        key = _validate_text(
            item.get("key"), field="custom_parameters[].key", max_length=CUSTOM_PARAMETER_KEY_MAX_LENGTH
        )
        assert key is not None
        if key in seen:
            raise ModelRegistryError(f"custom_parameters 的 key 重复：{key}")
        seen.add(key)
        raw_value = item.get("value", "")
        if not isinstance(raw_value, str):
            raise ModelRegistryError("custom_parameters[].value 必须是字符串")
        param_type = item.get("type")
        if param_type is None:
            param_type = "text"
        if not isinstance(param_type, str) or param_type not in CUSTOM_PARAMETER_TYPES:
            raise ModelRegistryError(
                f"custom_parameters[].type 只能是 {' / '.join(CUSTOM_PARAMETER_TYPES)}"
            )
        resolved.append({"key": key, "value": raw_value, "type": param_type})
    return resolved


def _validate_model_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelRegistryError("id 必须是字符串")
    resolved = value.strip()
    if not resolved:
        raise ModelRegistryError("id 不能为空")
    if len(resolved) > MODEL_ID_MAX_LENGTH:
        raise ModelRegistryError(f"id 不能超过 {MODEL_ID_MAX_LENGTH} 个字符")
    return resolved


def derive_model_id(provider_id: str, model: str) -> str:
    """批量导入与非显式 id 场景下的条目 id：`{provider_id}:{model}`。"""

    return f"{provider_id}:{model}"[:MODEL_ID_MAX_LENGTH]


# --------------------------------------------------------------------------- #
# Provider 注册表
# --------------------------------------------------------------------------- #


def _provider_view(row: dict[str, Any], *, model_count: int) -> dict[str, Any]:
    """出参视图：永不包含 `api_key`，只回 `api_key_configured`。"""

    return {
        "id": row["id"],
        "name": row["name"],
        "preset_type": row["preset_type"],
        "api_type": row["api_type"],
        "base_url": row["base_url"],
        "api_key_configured": bool(row.get("api_key")),
        "custom_headers": row.get("custom_headers") or {},
        "additional_settings": row.get("additional_settings") or {},
        "enabled": row["enabled"],
        "model_count": model_count,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


def list_providers(
    *,
    rows: list[dict[str, Any]] | None = None,
    model_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`GET /api/v1/config/providers` 的响应体。`rows`/`model_rows` 供测试注入。"""

    provider_rows = checkpoint.list_llm_providers() if rows is None else rows
    models = checkpoint.list_llm_models() if model_rows is None else model_rows
    counts: dict[str, int] = {}
    for model in models:
        counts[model["provider_id"]] = counts.get(model["provider_id"], 0) + 1
    items = [
        _provider_view(row, model_count=counts.get(row["id"], 0))
        for row in provider_rows
    ]
    return {"items": items, "total": len(items)}


def get_provider(provider_id: str) -> dict[str, Any]:
    row = checkpoint.get_llm_provider(provider_id)
    if row is None:
        raise ProviderNotFoundError(provider_id)
    models = checkpoint.list_llm_models(provider_id=provider_id)
    view = _provider_view(row, model_count=len(models))
    view["models"] = [_model_view(item) for item in models]
    return view


def create_provider(
    *,
    provider_id: Any,
    name: Any,
    preset_type: Any = "openai-compatible",
    api_type: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    custom_headers: Any = UNSET,
    additional_settings: Any = UNSET,
    enabled: Any = True,
    actor: str | None = None,
) -> dict[str, Any]:
    resolved_id = _validate_provider_id(provider_id)
    resolved_name = _validate_text(
        name, field="name", max_length=PROVIDER_NAME_MAX_LENGTH
    )
    assert resolved_name is not None
    resolved_preset = _validate_preset_type(preset_type)
    resolved_api_type = (
        get_default_api_type_for_preset(resolved_preset)
        if api_type is UNSET or api_type is None
        else _validate_api_type(api_type)
    )
    resolved_base_url = (
        None if base_url is UNSET else _validate_base_url(base_url)
    )
    resolved_api_key = None if api_key is UNSET else _validate_api_key(api_key)
    resolved_headers = (
        {} if custom_headers is UNSET else _validate_string_map(
            custom_headers,
            field="custom_headers",
            max_items=CUSTOM_HEADERS_MAX_ITEMS,
            value_max_length=CUSTOM_HEADER_VALUE_MAX_LENGTH,
        )
    )
    resolved_settings = (
        {} if additional_settings is UNSET else _validate_additional_settings(additional_settings)
    )
    if not isinstance(enabled, bool):
        raise ModelRegistryError("enabled 必须是布尔值")
    if checkpoint.get_llm_provider(resolved_id) is not None:
        raise DuplicateEntryError(f"Provider id 已存在：{resolved_id}")

    row = checkpoint.create_llm_provider(
        provider_id=resolved_id,
        name=resolved_name,
        preset_type=resolved_preset,
        api_type=resolved_api_type,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        custom_headers=resolved_headers,
        additional_settings=resolved_settings,
        enabled=enabled,
        updated_by=actor,
    )
    log_event(
        logger,
        "config.provider_registry.created",
        provider_id=resolved_id,
        actor=actor,
        api_type=resolved_api_type,
        api_key="set" if resolved_api_key else "unset",
    )
    return _provider_view(row, model_count=0)


def _validate_additional_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelRegistryError("additional_settings 必须是对象")
    return dict(value)


def update_provider(
    provider_id: str,
    *,
    name: Any = UNSET,
    preset_type: Any = UNSET,
    api_type: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    custom_headers: Any = UNSET,
    additional_settings: Any = UNSET,
    enabled: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    """部分更新 Provider。

    显式 `null` 的语义是「回退该字段的默认值」：`preset_type` → `openai-compatible`，
    `api_type` → 当前 preset 的默认协议族（这两列在库中非空）。
    `api_key` 的空串表示**不修改**（ADR-017 §5）。
    """

    current = checkpoint.get_llm_provider(provider_id)
    if current is None:
        raise ProviderNotFoundError(provider_id)

    fields: dict[str, Any] = {}
    if name is not UNSET:
        resolved = _validate_text(name, field="name", max_length=PROVIDER_NAME_MAX_LENGTH)
        assert resolved is not None
        fields["name"] = resolved
    if preset_type is not UNSET:
        fields["preset_type"] = (
            "openai-compatible"
            if preset_type is None
            else _validate_preset_type(preset_type)
        )
    if api_type is not UNSET:
        effective_preset = fields.get("preset_type", current["preset_type"])
        fields["api_type"] = (
            get_default_api_type_for_preset(effective_preset)
            if api_type is None
            else _validate_api_type(api_type)
        )
    if base_url is not UNSET:
        fields["base_url"] = None if base_url is None else _validate_base_url(base_url)
    if api_key is not UNSET:
        if isinstance(api_key, str) and not api_key.strip():
            pass  # 空串 = 不修改已配置的凭据
        else:
            fields["api_key"] = None if api_key is None else _validate_api_key(api_key)
    if custom_headers is not UNSET:
        fields["custom_headers"] = (
            {}
            if custom_headers is None
            else _validate_string_map(
                custom_headers,
                field="custom_headers",
                max_items=CUSTOM_HEADERS_MAX_ITEMS,
                value_max_length=CUSTOM_HEADER_VALUE_MAX_LENGTH,
            )
        )
    if additional_settings is not UNSET:
        fields["additional_settings"] = (
            {}
            if additional_settings is None
            else _validate_additional_settings(additional_settings)
        )
    if enabled is not UNSET:
        if not isinstance(enabled, bool):
            raise ModelRegistryError("enabled 必须是布尔值")
        fields["enabled"] = enabled

    row = checkpoint.update_llm_provider(provider_id, updated_by=actor, **fields)
    if row is None:
        raise ProviderNotFoundError(provider_id)
    log_event(
        logger,
        "config.provider_registry.updated",
        provider_id=provider_id,
        actor=actor,
        fields=",".join(sorted(fields)) or "none",
        api_key="set" if fields.get("api_key") else "unchanged",
    )
    models = checkpoint.list_llm_models(provider_id=provider_id)
    return _provider_view(row, model_count=len(models))


def delete_provider(
    provider_id: str, *, force: bool = False, actor: str | None = None
) -> None:
    if checkpoint.get_llm_provider(provider_id) is None:
        raise ProviderNotFoundError(provider_id)
    enabled_models = checkpoint.count_enabled_models(provider_id)
    if enabled_models and not force:
        raise ProviderInUseError(provider_id, enabled_models)
    deleted_models = len(checkpoint.list_llm_models(provider_id=provider_id))
    if not checkpoint.delete_llm_provider(provider_id):
        raise ProviderNotFoundError(provider_id)
    log_event(
        logger,
        "config.provider_registry.deleted",
        provider_id=provider_id,
        actor=actor,
        cascade_models=deleted_models,
        force=force,
    )


# --------------------------------------------------------------------------- #
# 模型注册表
# --------------------------------------------------------------------------- #


def _model_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "model": row["model"],
        "name": row["name"],
        "enabled": row["enabled"],
        "reasoning_type": row.get("reasoning_type") or "none",
        "temperature": row.get("temperature"),
        "top_p": row.get("top_p"),
        "max_context_tokens": row.get("max_context_tokens"),
        "max_output_tokens": row.get("max_output_tokens"),
        "custom_parameters": row.get("custom_parameters") or [],
        "modalities": row.get("modalities") or [],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


def list_models(
    *,
    provider_id: str | None = None,
    enabled: bool | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    model_rows = (
        checkpoint.list_llm_models(provider_id=provider_id, enabled=enabled)
        if rows is None
        else rows
    )
    items = [_model_view(row) for row in model_rows]
    return {"items": items, "total": len(items)}


def create_model(
    *,
    provider_id: Any,
    model: Any,
    model_id: Any = UNSET,
    name: Any = UNSET,
    enabled: Any = True,
    reasoning_type: Any = UNSET,
    temperature: Any = UNSET,
    top_p: Any = UNSET,
    max_context_tokens: Any = UNSET,
    max_output_tokens: Any = UNSET,
    custom_parameters: Any = UNSET,
    modalities: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    if model is UNSET or model is None:
        raise ModelRegistryError("model 不能为空")
    fields = _validate_model_fields(
        model=model,
        name=name,
        reasoning_type=reasoning_type,
        temperature=temperature,
        top_p=top_p,
        max_context_tokens=max_context_tokens,
        max_output_tokens=max_output_tokens,
        custom_parameters=custom_parameters,
        modalities=modalities,
    )
    resolved_provider_id = _validate_provider_id(provider_id)
    if checkpoint.get_llm_provider(resolved_provider_id) is None:
        raise ProviderNotFoundError(resolved_provider_id)
    if not isinstance(enabled, bool):
        raise ModelRegistryError("enabled 必须是布尔值")

    resolved_id = (
        derive_model_id(resolved_provider_id, fields["model"])
        if model_id is UNSET or model_id is None
        else _validate_model_id(model_id)
    )
    if checkpoint.get_llm_model(resolved_id) is not None:
        raise DuplicateEntryError(f"模型 id 已存在：{resolved_id}")
    if (
        checkpoint.find_llm_model_by_provider_and_name(
            resolved_provider_id, fields["model"]
        )
        is not None
    ):
        raise DuplicateEntryError(
            f"该 Provider 下已存在模型：{fields['model']}"
        )

    row = checkpoint.create_llm_model(
        model_id=resolved_id,
        provider_id=resolved_provider_id,
        enabled=enabled,
        updated_by=actor,
        **fields,
    )
    log_event(
        logger,
        "config.model.created",
        model_id=resolved_id,
        provider_id=resolved_provider_id,
        actor=actor,
    )
    return _model_view(row)


def _validate_model_fields(
    *,
    model: Any = UNSET,
    name: Any = UNSET,
    reasoning_type: Any = UNSET,
    temperature: Any = UNSET,
    top_p: Any = UNSET,
    max_context_tokens: Any = UNSET,
    max_output_tokens: Any = UNSET,
    custom_parameters: Any = UNSET,
    modalities: Any = UNSET,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if model is not UNSET:
        resolved_model = _validate_text(
            model, field="model", max_length=MODEL_MAX_LENGTH
        )
        assert resolved_model is not None
        fields["model"] = resolved_model
    if name is not UNSET:
        fields["name"] = (
            None
            if name is None
            else _validate_text(
                name, field="name", max_length=MODEL_NAME_MAX_LENGTH, allow_empty=True
            )
        )
    if reasoning_type is not UNSET:
        fields["reasoning_type"] = _validate_reasoning_type(reasoning_type)
    if temperature is not UNSET:
        fields["temperature"] = (
            None
            if temperature is None
            else _validate_optional_number(
                temperature, field="temperature", minimum=TEMPERATURE_MIN, maximum=TEMPERATURE_MAX
            )
        )
    if top_p is not UNSET:
        fields["top_p"] = (
            None
            if top_p is None
            else _validate_optional_number(
                top_p, field="top_p", minimum=TOP_P_MIN, maximum=TOP_P_MAX
            )
        )
    if max_context_tokens is not UNSET:
        fields["max_context_tokens"] = (
            None
            if max_context_tokens is None
            else _validate_optional_int(
                max_context_tokens, field="max_context_tokens", minimum=1
            )
        )
    if max_output_tokens is not UNSET:
        fields["max_output_tokens"] = (
            None
            if max_output_tokens is None
            else _validate_optional_int(
                max_output_tokens, field="max_output_tokens", minimum=1
            )
        )
    if custom_parameters is not UNSET:
        fields["custom_parameters"] = (
            [] if custom_parameters is None else _validate_custom_parameters(custom_parameters)
        )
    if modalities is not UNSET:
        fields["modalities"] = (
            [] if modalities is None else _validate_modalities(modalities)
        )
    return fields


def update_model(model_id: str, *, actor: str | None = None, **fields: Any) -> dict[str, Any]:
    """部分更新模型条目。

    `enabled` 是布尔开关，不走 `_validate_model_fields` 的取值校验族，单独处理；
    `reasoning_type` 在库中非空，因此传 `None` 会落到 `none` 而不是 NULL
    （API 层已把显式 `null` 归一到 `"none"`，这里是兜底）。
    """

    existing = checkpoint.get_llm_model(model_id)
    if existing is None:
        raise ModelNotFoundError(model_id)

    enabled = fields.pop("enabled", UNSET)
    if enabled is not UNSET and not isinstance(enabled, bool):
        raise ModelRegistryError("enabled 必须是布尔值")

    validated = _validate_model_fields(**fields)
    if enabled is not UNSET:
        validated["enabled"] = enabled
    if "model" in validated:
        duplicate = checkpoint.find_llm_model_by_provider_and_name(
            existing["provider_id"], validated["model"]
        )
        if duplicate is not None and duplicate["id"] != model_id:
            raise DuplicateEntryError(
                f"该 Provider 下已存在模型：{validated['model']}"
            )
    if actor is not None:
        validated["updated_by"] = actor
    row = checkpoint.update_llm_model(model_id, **validated)
    if row is None:
        raise ModelNotFoundError(model_id)
    log_event(
        logger,
        "config.model.updated",
        model_id=model_id,
        actor=actor,
        fields=",".join(sorted(validated)) or "none",
    )
    return _model_view(row)


def delete_model(model_id: str, *, actor: str | None = None) -> None:
    if not checkpoint.delete_llm_model(model_id):
        raise ModelNotFoundError(model_id)
    log_event(logger, "config.model.deleted", model_id=model_id, actor=actor)


def batch_import_models(
    *,
    provider_id: Any,
    models: Any,
    name_prefix: Any = "",
    enabled: Any = True,
    defaults: Any = UNSET,
    actor: str | None = None,
) -> dict[str, Any]:
    """批量引入模型（`doc/api.md` §5.10）。

    已存在的 `(provider_id, model)` 计入 `skipped` 而不报错，因此重复提交幂等。
    只写 `model` / `name` / `enabled` 与 `defaults`，不覆盖已存在条目的参数。
    """

    resolved_provider_id = _validate_provider_id(provider_id)
    if checkpoint.get_llm_provider(resolved_provider_id) is None:
        raise ProviderNotFoundError(resolved_provider_id)

    if not isinstance(models, list):
        raise ModelRegistryError("models 必须是数组")
    if not models:
        raise ModelRegistryError("models 不能为空")
    if len(models) > BATCH_IMPORT_MAX_ITEMS:
        raise ModelRegistryError(f"单次批量导入最多 {BATCH_IMPORT_MAX_ITEMS} 条")
    if not isinstance(enabled, bool):
        raise ModelRegistryError("enabled 必须是布尔值")
    if not isinstance(name_prefix, str):
        raise ModelRegistryError("name_prefix 必须是字符串")

    default_fields: dict[str, Any] = {}
    if defaults is not UNSET and defaults is not None:
        if not isinstance(defaults, dict):
            raise ModelRegistryError("defaults 必须是对象")
        unknown = set(defaults) - {
            "temperature",
            "top_p",
            "max_context_tokens",
            "max_output_tokens",
            "reasoning_type",
            "modalities",
        }
        if unknown:
            raise ModelRegistryError(
                f"defaults 不支持字段：{'、'.join(sorted(unknown))}"
            )
        default_fields = _validate_model_fields(
            temperature=defaults.get("temperature", UNSET),
            top_p=defaults.get("top_p", UNSET),
            max_context_tokens=defaults.get("max_context_tokens", UNSET),
            max_output_tokens=defaults.get("max_output_tokens", UNSET),
            reasoning_type=defaults.get("reasoning_type", UNSET),
            modalities=defaults.get("modalities", UNSET),
        )

    # 去空白 + 去重，保持用户选择顺序。
    requested: list[str] = []
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, str):
            raise ModelRegistryError("models 的每一项必须是字符串")
        name = item.strip()
        if not name:
            continue
        if len(name) > MODEL_MAX_LENGTH:
            raise ModelRegistryError(f"模型名不能超过 {MODEL_MAX_LENGTH} 个字符")
        if name in seen:
            continue
        seen.add(name)
        requested.append(name)
    if not requested:
        raise ModelRegistryError("models 去空白后为空")

    existing_names = {
        row["model"]
        for row in checkpoint.list_llm_models(provider_id=resolved_provider_id)
    }

    created: list[str] = []
    skipped: list[dict[str, str]] = []
    pending: list[dict[str, Any]] = []
    for name in requested:
        if name in existing_names:
            skipped.append({"model": name, "reason": "already_exists"})
            continue
        model_id = derive_model_id(resolved_provider_id, name)
        if checkpoint.get_llm_model(model_id) is not None:
            skipped.append({"model": name, "reason": "id_conflict"})
            continue
        pending.append(
            {
                "id": model_id,
                "provider_id": resolved_provider_id,
                "model": name,
                "name": f"{name_prefix}{name}" if name_prefix else name,
                "enabled": enabled,
                "reasoning_type": default_fields.get("reasoning_type", "none"),
                "temperature": default_fields.get("temperature"),
                "top_p": default_fields.get("top_p"),
                "max_context_tokens": default_fields.get("max_context_tokens"),
                "max_output_tokens": default_fields.get("max_output_tokens"),
                "custom_parameters": [],
                "modalities": default_fields.get("modalities", ["text"]),
                "updated_by": actor,
            }
        )

    created = checkpoint.create_llm_models_bulk(pending)
    created_set = set(created)
    for row in pending:
        if row["id"] not in created_set:
            skipped.append({"model": row["model"], "reason": "already_exists"})

    log_event(
        logger,
        "config.model.batch_imported",
        provider_id=resolved_provider_id,
        actor=actor,
        created=len(created),
        skipped=len(skipped),
        requested=len(requested),
    )
    return {
        "provider_id": resolved_provider_id,
        "created": created,
        "skipped": skipped,
        "total_requested": len(requested),
    }


def discover_provider_models(
    provider_id: str, *, fetcher: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """`GET /api/v1/config/providers/{id}/models/discover` 的响应体。

    `ModelDiscoveryError` 原样向上抛，由 API 层映射成 `502 PROVIDER_DISCOVERY_FAILED`。
    """

    row = checkpoint.get_llm_provider(provider_id)
    if row is None:
        raise ProviderNotFoundError(provider_id)
    models: list[DiscoveredModel]
    models, source_url = discover_models(row, fetcher=fetcher)
    existing = [
        item["model"]
        for item in checkpoint.list_llm_models(provider_id=provider_id)
    ]
    return {
        "provider_id": provider_id,
        "source": "remote",
        "source_url": source_url,
        "items": [
            {"id": model.id, "name": model.name, "owned_by": model.owned_by}
            for model in models
        ],
        "existing": existing,
        "total": len(models),
    }


# --------------------------------------------------------------------------- #
# 生效解析
# --------------------------------------------------------------------------- #


def resolve_llm_model(model_id: str) -> dict[str, Any] | None:
    """按条目 id 解析模型 + Provider；条目或其 Provider 不存在时返回 None。

    悬空引用（条目被删除）按「未绑定」处理，不抛错（ADR-017）。
    """

    model = checkpoint.get_llm_model(model_id)
    if model is None:
        return None
    provider = checkpoint.get_llm_provider(model["provider_id"])
    if provider is None:
        return None
    return {"model": model, "provider": provider}


def resolve_provider_settings_from_model(
    model_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """把模型条目翻译成 `AgentSettings` 的更新字段与脱敏视图。

    返回 `(updates, view)`：`updates` 直接喂给 `AgentSettings.model_copy(update=...)`，
    `view` 是 `llm_provider` / `preset_type` / `api_type` / `llm_model_id` /
    `provider_name` 等展示与构造所需字段。
    """

    resolved = resolve_llm_model(model_id)
    if resolved is None:
        return None
    model = resolved["model"]
    provider = resolved["provider"]

    api_type = provider["api_type"]
    preset_type = provider["preset_type"]
    provider_kind = "ollama" if preset_type == "ollama" else "openai"

    updates: dict[str, Any] = {
        "llm_provider": provider_kind,
        "api_type": api_type,
        "preset_type": preset_type,
        "llm_model_id": model["id"],
        "provider_name": provider["name"],
        "custom_headers": provider.get("custom_headers") or {},
        "reasoning_type": model.get("reasoning_type") or "none",
    }
    model_name = model["model"]
    base_url = provider.get("base_url")
    api_key = provider.get("api_key")

    if provider_kind == "ollama":
        updates["ollama_model"] = model_name
        if base_url:
            updates["ollama_base_url"] = base_url
    else:
        updates["openai_model"] = model_name
        if base_url:
            updates["openai_base_url"] = base_url
        if api_key:
            updates["openai_api_key"] = api_key

    if model.get("temperature") is not None:
        updates["temperature"] = model["temperature"]
    if model.get("top_p") is not None:
        updates["top_p"] = model["top_p"]
    if model.get("max_output_tokens") is not None:
        updates["max_tokens"] = model["max_output_tokens"]

    return updates, {
        "llm_model_id": model["id"],
        "model_ref": model_name,
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "preset_type": preset_type,
        "api_type": api_type,
        "custom_parameters": model.get("custom_parameters") or [],
        "modalities": model.get("modalities") or [],
    }


def effective_model_view(model_id: str) -> dict[str, Any] | None:
    """`llm_model_id` 指向的条目的脱敏摘要；悬空时返回 None。"""

    resolved = resolve_llm_model(model_id)
    if resolved is None:
        return None
    model = resolved["model"]
    provider = resolved["provider"]
    return {
        "id": model["id"],
        "model": model["model"],
        "name": model.get("name"),
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "preset_type": provider["preset_type"],
        "api_type": provider["api_type"],
        "enabled": model["enabled"],
    }

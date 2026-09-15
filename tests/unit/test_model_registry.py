"""模型 Provider / 模型注册表与远端发现（`doc/api.md` §5.9、§5.10、§5.12、ADR-017）。

单元级证据：预设目录契约、取值校验、重复检测、`api_key` 只写不回读、
批量导入幂等、探测端点推导与错误脱敏、生效解析。

存储用内存替身（`FakeStore`）：`model_registry` 与 `checkpoint` 的边界是「dict 进
dict 出」，替身可以完整复现读写语义而不需要 PostgreSQL；真实表结构由
`test_agent_config.py` / `test_provider_config.py` 的表契约用例与 e2e 覆盖。
"""

from __future__ import annotations

import pytest

from app.core import checkpoint, model_discovery, model_registry
from app.core.checkpoint import UNSET

# --------------------------------------------------------------------------- #
# 内存存储替身
# --------------------------------------------------------------------------- #


class FakeStore:
    """复现 `checkpoint` 里注册表相关函数的读写语义。"""

    def __init__(self) -> None:
        self.providers: dict[str, dict] = {}
        self.models: dict[str, dict] = {}

    # ---- 安装 ----

    def install(self, monkeypatch) -> "FakeStore":
        for name in (
            "get_llm_provider",
            "list_llm_providers",
            "create_llm_provider",
            "update_llm_provider",
            "delete_llm_provider",
            "count_enabled_models",
            "get_llm_model",
            "list_llm_models",
            "find_llm_model_by_provider_and_name",
            "create_llm_model",
            "create_llm_models_bulk",
            "update_llm_model",
            "delete_llm_model",
        ):
            monkeypatch.setattr(checkpoint, name, getattr(self, name))
        return self

    # ---- Provider ----

    def get_llm_provider(self, provider_id: str) -> dict | None:
        row = self.providers.get(provider_id)
        return dict(row) if row else None

    def list_llm_providers(self) -> list[dict]:
        return [dict(row) for row in sorted(self.providers.values(), key=lambda r: r["id"])]

    def create_llm_provider(
        self,
        *,
        provider_id: str,
        name: str,
        preset_type: str,
        api_type: str,
        base_url=None,
        api_key=None,
        custom_headers=None,
        additional_settings=None,
        enabled=True,
        updated_by=None,
    ) -> dict:
        row = {
            "id": provider_id,
            "name": name,
            "preset_type": preset_type,
            "api_type": api_type,
            "base_url": base_url,
            "api_key": api_key,
            "custom_headers": dict(custom_headers or {}),
            "additional_settings": dict(additional_settings or {}),
            "enabled": enabled,
            "created_at": None,
            "updated_at": None,
            "updated_by": updated_by,
        }
        self.providers[provider_id] = row
        return dict(row)

    def update_llm_provider(self, provider_id: str, *, updated_by=None, **fields) -> dict | None:
        row = self.providers.get(provider_id)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not UNSET:
                row[key] = value
        row["updated_by"] = updated_by
        return dict(row)

    def delete_llm_provider(self, provider_id: str) -> bool:
        if provider_id not in self.providers:
            return False
        for model_id in [
            mid for mid, row in self.models.items() if row["provider_id"] == provider_id
        ]:
            self.models.pop(model_id)
        self.providers.pop(provider_id)
        return True

    def count_enabled_models(self, provider_id: str) -> int:
        return sum(
            1
            for row in self.models.values()
            if row["provider_id"] == provider_id and row["enabled"]
        )

    # ---- 模型 ----

    def get_llm_model(self, model_id: str) -> dict | None:
        row = self.models.get(model_id)
        return dict(row) if row else None

    def list_llm_models(self, *, provider_id=None, enabled=None) -> list[dict]:
        rows = sorted(self.models.values(), key=lambda r: (r["provider_id"], r["model"]))
        if provider_id is not None:
            rows = [row for row in rows if row["provider_id"] == provider_id]
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is enabled]
        return [dict(row) for row in rows]

    def find_llm_model_by_provider_and_name(self, provider_id: str, model: str) -> dict | None:
        for row in self.models.values():
            if row["provider_id"] == provider_id and row["model"] == model:
                return dict(row)
        return None

    def _new_model_row(self, **fields) -> dict:
        return {
            "id": fields["model_id"],
            "provider_id": fields["provider_id"],
            "model": fields["model"],
            "name": fields.get("name"),
            "enabled": fields.get("enabled", True),
            "reasoning_type": fields.get("reasoning_type", "none"),
            "temperature": fields.get("temperature"),
            "top_p": fields.get("top_p"),
            "max_context_tokens": fields.get("max_context_tokens"),
            "max_output_tokens": fields.get("max_output_tokens"),
            "custom_parameters": list(fields.get("custom_parameters") or []),
            "modalities": list(fields.get("modalities") or []),
            "created_at": None,
            "updated_at": None,
            "updated_by": fields.get("updated_by"),
        }

    def create_llm_model(self, **fields) -> dict:
        row = self._new_model_row(**fields)
        self.models[row["id"]] = row
        return dict(row)

    def create_llm_models_bulk(self, rows: list[dict]) -> list[str]:
        created: list[str] = []
        for row in rows:
            exists = any(
                existing["provider_id"] == row["provider_id"]
                and existing["model"] == row["model"]
                for existing in self.models.values()
            )
            if exists or row["id"] in self.models:
                continue
            self.models[row["id"]] = {**row, "created_at": None, "updated_at": None}
            created.append(row["id"])
        return created

    def update_llm_model(self, model_id: str, **fields) -> dict | None:
        row = self.models.get(model_id)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not UNSET:
                row[key] = value
        return dict(row)

    def delete_llm_model(self, model_id: str) -> bool:
        return self.models.pop(model_id, None) is not None

    # ---- 便捷构造 ----

    def add_provider(self, provider_id: str = "gw", **overrides) -> dict:
        return model_registry.create_provider(
            provider_id=provider_id,
            name=overrides.pop("name", "自建网关"),
            preset_type=overrides.pop("preset_type", "openai-compatible"),
            **overrides,
        )


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    return FakeStore().install(monkeypatch)


# --------------------------------------------------------------------------- #
# §5.12 预设目录
# --------------------------------------------------------------------------- #


REQUIRED_PRESET_KEYS = {
    "preset_type",
    "label",
    "monogram",
    "tint",
    "category",
    "default_api_type",
    "supported_api_types",
    "default_base_url",
    "requires_api_key",
    "api_key_url",
    "supports_model_discovery",
}


def test_preset_catalog_shape():
    catalog = model_registry.provider_preset_catalog()

    assert catalog["categories"][0] == {"id": "all", "label": "全部"}
    category_ids = {category["id"] for category in catalog["categories"]}
    assert {"main", "cn", "gateway", "cloud", "local"} <= category_ids

    for preset in catalog["items"]:
        assert set(preset) == REQUIRED_PRESET_KEYS
        assert preset["category"] in category_ids
        assert preset["default_api_type"] in preset["supported_api_types"]
        # 前端用它做无 logo 时的标记与配色，不能为空
        assert preset["monogram"]
        assert preset["tint"]


def test_preset_catalog_marks_discovery_support():
    presets = {p["preset_type"]: p for p in model_registry.provider_preset_catalog()["items"]}

    assert presets["openai"]["supports_model_discovery"] is True
    assert presets["deepseek"]["default_base_url"] == "https://api.deepseek.com/v1"
    # 无统一清单端点、且需要请求签名：明确标为不支持，前端据此禁用「拉取模型」
    assert presets["amazon-bedrock"]["supports_model_discovery"] is False
    # 本地运行时不需要凭据
    assert presets["ollama"]["requires_api_key"] is False


def test_default_api_type_falls_back_for_unknown_preset():
    assert model_registry.get_default_api_type_for_preset("does-not-exist") == "openai-compatible"
    assert model_registry.get_default_api_type_for_preset("deepseek") == "openai-compatible"
    assert model_registry.get_default_api_type_for_preset("anthropic") == "anthropic"


# --------------------------------------------------------------------------- #
# §5.9 Provider 注册表
# --------------------------------------------------------------------------- #


def test_create_provider_derives_api_type_from_preset(store):
    row = model_registry.create_provider(
        provider_id="anthropic-main", name="Anthropic", preset_type="anthropic"
    )

    assert row["api_type"] == "anthropic"
    assert row["api_key_configured"] is False
    assert row["model_count"] == 0


def test_create_provider_never_exposes_api_key(store):
    row = model_registry.create_provider(
        provider_id="gw", name="自建网关", api_key="sk-secret"
    )

    assert row["api_key_configured"] is True
    assert "api_key" not in row
    assert "sk-secret" not in str(row)


@pytest.mark.parametrize(
    "provider_id",
    ["", "   ", "has space", "斜杠/不行", "x" * 51],
)
def test_create_provider_rejects_bad_id(store, provider_id):
    with pytest.raises(model_registry.ModelRegistryError):
        model_registry.create_provider(provider_id=provider_id, name="x")


def test_create_provider_rejects_unknown_preset(store):
    with pytest.raises(model_registry.ModelRegistryError):
        model_registry.create_provider(
            provider_id="gw", name="x", preset_type="not-a-preset"
        )


def test_duplicate_provider_id_is_a_conflict(store):
    store.add_provider("gw")

    with pytest.raises(model_registry.DuplicateEntryError) as excinfo:
        store.add_provider("gw")

    # 契约里重复条目是 409，因此必须是 `DuplicateEntryError`（`ModelRegistryError` 子类）
    assert isinstance(excinfo.value, model_registry.ModelRegistryError)
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_update_provider_blank_api_key_keeps_existing(store):
    store.add_provider("gw", api_key="sk-original")

    row = model_registry.update_provider("gw", api_key="   ", name="改名")

    assert row["api_key_configured"] is True
    assert store.get_llm_provider("gw")["api_key"] == "sk-original"


def test_update_provider_explicit_null_clears_api_key(store):
    store.add_provider("gw", api_key="sk-original")

    row = model_registry.update_provider("gw", api_key=None)

    assert row["api_key_configured"] is False
    assert store.get_llm_provider("gw")["api_key"] is None


def test_update_provider_null_api_type_falls_back_to_preset_default(store):
    store.add_provider("anthropic-main", preset_type="anthropic")

    row = model_registry.update_provider("anthropic-main", api_type=None)

    assert row["api_type"] == "anthropic"


def test_update_provider_rejects_bad_base_url(store):
    store.add_provider("gw")

    with pytest.raises(model_registry.ModelRegistryError, match="http"):
        model_registry.update_provider("gw", base_url="ftp://example.com")


def test_update_missing_provider_is_not_found(store):
    with pytest.raises(model_registry.ProviderNotFoundError):
        model_registry.update_provider("missing", name="x")


def test_delete_provider_refuses_while_models_enabled(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    with pytest.raises(model_registry.ProviderInUseError) as excinfo:
        model_registry.delete_provider("gw")

    assert excinfo.value.code == "PROVIDER_IN_USE"
    assert store.get_llm_provider("gw") is not None


def test_delete_provider_force_cascades_models(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    model_registry.delete_provider("gw", force=True)

    assert store.get_llm_provider("gw") is None
    assert store.list_llm_models() == []


def test_delete_provider_without_enabled_models_needs_no_force(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini", enabled=False)

    model_registry.delete_provider("gw")

    assert store.get_llm_provider("gw") is None


def test_list_providers_counts_models(store):
    store.add_provider("gw")
    store.add_provider("other")
    model_registry.create_model(provider_id="gw", model="a")
    model_registry.create_model(provider_id="gw", model="b")

    data = model_registry.list_providers()
    counts = {item["id"]: item["model_count"] for item in data["items"]}

    assert data["total"] == 2
    assert counts == {"gw": 2, "other": 0}


# --------------------------------------------------------------------------- #
# §5.10 模型注册表
# --------------------------------------------------------------------------- #


def test_create_model_derives_id_and_defaults(store):
    store.add_provider("gw")

    row = model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    assert row["id"] == "gw:gpt-4o-mini"
    assert row["reasoning_type"] == "none"
    assert row["temperature"] is None
    assert row["modalities"] == []
    assert row["enabled"] is True


def test_create_model_requires_existing_provider(store):
    with pytest.raises(model_registry.ProviderNotFoundError) as excinfo:
        model_registry.create_model(provider_id="missing", model="m")

    assert excinfo.value.code == "PROVIDER_NOT_FOUND"


def test_create_model_rejects_duplicate_model_name_in_same_provider(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    with pytest.raises(model_registry.DuplicateEntryError, match="已存在"):
        model_registry.create_model(provider_id="gw", model="gpt-4o-mini")


def test_create_model_rejects_duplicate_pair_even_with_custom_id(store):
    """`(provider_id, model)` 唯一：换个 id 也不能重复登记同一个模型（§5.10）。"""

    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    with pytest.raises(model_registry.DuplicateEntryError, match="已存在模型"):
        model_registry.create_model(
            provider_id="gw", model="gpt-4o-mini", model_id="custom-id"
        )


def test_create_model_allows_same_name_in_different_provider(store):
    store.add_provider("gw")
    store.add_provider("other")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")

    row = model_registry.create_model(provider_id="other", model="gpt-4o-mini")

    assert row["id"] == "other:gpt-4o-mini"


def test_create_model_rejects_duplicate_explicit_id(store):
    store.add_provider("gw")
    store.add_provider("other")
    model_registry.create_model(provider_id="gw", model="a", model_id="shared")

    with pytest.raises(model_registry.DuplicateEntryError, match="id 已存在"):
        model_registry.create_model(provider_id="other", model="b", model_id="shared")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"temperature": 2.5},
        {"temperature": -0.5},
        {"top_p": 1.5},
        {"max_context_tokens": 0},
        {"max_output_tokens": -1},
        {"reasoning_type": "unknown"},
        {"modalities": ["audio"]},
        {"custom_parameters": [{"key": "a", "value": "1", "type": "date"}]},
        {
            "custom_parameters": [
                {"key": "same", "value": "1", "type": "text"},
                {"key": "same", "value": "2", "type": "text"},
            ]
        },
    ],
)
def test_create_model_rejects_invalid_specialized_parameters(store, kwargs):
    store.add_provider("gw")

    with pytest.raises(model_registry.ModelRegistryError):
        model_registry.create_model(provider_id="gw", model="m", **kwargs)


def test_create_model_normalizes_custom_parameters_and_modalities(store):
    store.add_provider("gw")

    row = model_registry.create_model(
        provider_id="gw",
        model="m",
        custom_parameters=[{"key": " thinking_budget ", "value": "2048", "type": "number"}],
        modalities=["text", "vision", "text"],
    )

    assert row["custom_parameters"] == [
        {"key": "thinking_budget", "value": "2048", "type": "number"}
    ]
    assert row["modalities"] == ["text", "vision"]


def test_create_model_requires_model_name(store, ):
    store.add_provider("gw")

    with pytest.raises(model_registry.ModelRegistryError, match="model"):
        model_registry.create_model(provider_id="gw", model=None)


def test_update_model_toggles_enabled(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="m")

    row = model_registry.update_model("gw:m", enabled=False)

    assert row["enabled"] is False
    assert store.get_llm_model("gw:m")["enabled"] is False


def test_update_model_rejects_non_boolean_enabled(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="m")

    with pytest.raises(model_registry.ModelRegistryError, match="enabled"):
        model_registry.update_model("gw:m", enabled="yes")


def test_update_model_clears_numeric_parameters_with_null(store):
    store.add_provider("gw")
    model_registry.create_model(
        provider_id="gw", model="m", temperature=0.5, top_p=0.9, max_output_tokens=1024
    )

    row = model_registry.update_model("gw:m", temperature=None, top_p=None)

    assert row["temperature"] is None
    assert row["top_p"] is None
    assert row["max_output_tokens"] == 1024  # 未提交的字段保持原值


def test_update_model_detects_rename_conflict(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="a")
    model_registry.create_model(provider_id="gw", model="b")

    with pytest.raises(model_registry.DuplicateEntryError):
        model_registry.update_model("gw:b", model="a")


def test_update_model_allows_renaming_to_itself(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="a", model_id="custom-id")

    row = model_registry.update_model("custom-id", model="a")

    assert row["model"] == "a"


def test_update_missing_model_is_not_found(store):
    with pytest.raises(model_registry.ModelNotFoundError) as excinfo:
        model_registry.update_model("missing", name="x")

    assert excinfo.value.code == "MODEL_NOT_FOUND"


def test_delete_missing_model_is_not_found(store):
    with pytest.raises(model_registry.ModelNotFoundError):
        model_registry.delete_model("missing")


# --------------------------------------------------------------------------- #
# §5.10 批量引入
# --------------------------------------------------------------------------- #


def test_batch_import_creates_and_dedupes(store):
    store.add_provider("gw")

    result = model_registry.batch_import_models(
        provider_id="gw", models=[" a ", "b", "a", "  ", "c"]
    )

    assert result["created"] == ["gw:a", "gw:b", "gw:c"]
    assert result["skipped"] == []
    assert result["total_requested"] == 3  # 去空白、去重后的条数
    assert result["provider_id"] == "gw"


def test_batch_import_skips_existing_and_is_idempotent(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="a")

    first = model_registry.batch_import_models(provider_id="gw", models=["a", "b"])
    second = model_registry.batch_import_models(provider_id="gw", models=["a", "b"])

    assert first["created"] == ["gw:b"]
    assert first["skipped"] == [{"model": "a", "reason": "already_exists"}]
    # 重复提交不报错也不重复建：第二次全部计入 skipped
    assert second["created"] == []
    assert second["skipped"] == [
        {"model": "a", "reason": "already_exists"},
        {"model": "b", "reason": "already_exists"},
    ]


def test_batch_import_skips_id_conflict(store):
    store.add_provider("gw")
    store.add_provider("other")
    model_registry.create_model(
        provider_id="other", model="a", model_id="gw:a"
    )

    result = model_registry.batch_import_models(provider_id="gw", models=["a"])

    assert result["created"] == []
    assert result["skipped"] == [{"model": "a", "reason": "id_conflict"}]


def test_batch_import_does_not_overwrite_existing_parameters(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="a", temperature=0.9)

    model_registry.batch_import_models(
        provider_id="gw",
        models=["a", "b"],
        defaults={"temperature": 0.1},
    )

    assert store.get_llm_model("gw:a")["temperature"] == 0.9  # 已存在条目不被覆盖
    assert store.get_llm_model("gw:b")["temperature"] == 0.1  # 新条目吃到默认值


def test_batch_import_applies_name_prefix_and_modalities(store):
    store.add_provider("gw")

    model_registry.batch_import_models(
        provider_id="gw",
        models=["m1"],
        name_prefix="网关 / ",
        enabled=False,
        defaults={"modalities": ["text", "vision"], "reasoning_type": "openai"},
    )

    row = store.get_llm_model("gw:m1")
    assert row["name"] == "网关 / m1"
    assert row["enabled"] is False
    assert row["modalities"] == ["text", "vision"]
    assert row["reasoning_type"] == "openai"


def test_batch_import_requires_existing_provider(store):
    with pytest.raises(model_registry.ProviderNotFoundError):
        model_registry.batch_import_models(provider_id="missing", models=["a"])


def test_batch_import_rejects_empty_and_too_many(store):
    store.add_provider("gw")

    with pytest.raises(model_registry.ModelRegistryError, match="不能为空"):
        model_registry.batch_import_models(provider_id="gw", models=[])
    with pytest.raises(model_registry.ModelRegistryError, match="200"):
        model_registry.batch_import_models(
            provider_id="gw", models=[f"m{i}" for i in range(201)]
        )
    with pytest.raises(model_registry.ModelRegistryError, match="去空白后为空"):
        model_registry.batch_import_models(provider_id="gw", models=["   "])


def test_batch_import_rejects_unknown_defaults_key(store):
    store.add_provider("gw")

    with pytest.raises(model_registry.ModelRegistryError, match="不支持字段"):
        model_registry.batch_import_models(
            provider_id="gw", models=["a"], defaults={"max_tokens": 10}
        )


# --------------------------------------------------------------------------- #
# §5.10 远端发现
# --------------------------------------------------------------------------- #


class RecordingFetcher:
    """按**完整 URL** 返回预设载荷，并记录调用。

    用精确匹配而不是后缀匹配：候选端点常常互为后缀（`/v1/models` 与 `/models`），
    后缀匹配会让「回退到第二个候选」这类用例悄悄假通过。
    """

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, **kwargs):
        self.calls.append((url, dict(headers)))
        if url not in self.payloads:
            raise model_discovery.ModelDiscoveryError(f"未预设 {url}")
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        return payload


def _provider(**overrides) -> dict:
    row = {
        "id": "gw",
        "preset_type": "openai-compatible",
        "api_type": "openai-compatible",
        "base_url": "https://gw.example.com/v1",
        "api_key": "sk-secret",
        "custom_headers": {},
    }
    row.update(overrides)
    return row


def test_discover_uses_models_path_for_openai_compatible():
    fetcher = RecordingFetcher(
        {"https://gw.example.com/v1/models": {"data": [{"id": "gpt-4o-mini"}]}}
    )

    models, source_url = model_discovery.discover_models(_provider(), fetcher=fetcher)

    assert [model.id for model in models] == ["gpt-4o-mini"]
    assert source_url == "https://gw.example.com/v1/models"
    assert fetcher.calls[0][1]["Authorization"] == "Bearer sk-secret"


def test_discover_falls_back_to_models_without_v1():
    fetcher = RecordingFetcher({"https://gw.example.com/models": {"data": [{"id": "m"}]}})

    _, source_url = model_discovery.discover_models(_provider(), fetcher=fetcher)

    assert source_url == "https://gw.example.com/models"
    # 首个候选先试 /v1/models，失败后才退回 /models
    assert [url for url, _ in fetcher.calls] == [
        "https://gw.example.com/v1/models",
        "https://gw.example.com/models",
    ]


def test_discover_uses_ollama_tags_when_preset_is_ollama():
    fetcher = RecordingFetcher(
        {"http://localhost:11434/api/tags": {"models": [{"name": "qwen2.5-coder:7b"}]}}
    )

    models, source_url = model_discovery.discover_models(
        _provider(
            preset_type="ollama",
            api_type="openai-compatible",
            base_url="http://localhost:11434",
        ),
        fetcher=fetcher,
    )

    assert [model.id for model in models] == ["qwen2.5-coder:7b"]
    assert source_url == "http://localhost:11434/api/tags"
    # 本地运行时不应带上凭据头
    assert "Authorization" not in fetcher.calls[0][1]


def test_discover_uses_anthropic_version_header():
    fetcher = RecordingFetcher(
        {"https://api.anthropic.com/v1/models": {"data": [{"id": "claude-3-5-sonnet"}]}}
    )

    model_discovery.discover_models(
        _provider(api_type="anthropic", base_url="https://api.anthropic.com"),
        fetcher=fetcher,
    )

    headers = fetcher.calls[0][1]
    assert headers["x-api-key"] == "sk-secret"
    assert headers["anthropic-version"] == model_discovery.ANTHROPIC_VERSION


def test_discover_strips_gemini_models_prefix():
    fetcher = RecordingFetcher(
        {
            "https://generativelanguage.googleapis.com/v1beta/models": {
                "models": [
                    {"name": "models/gemini-1.5-pro", "displayName": "Gemini 1.5 Pro"}
                ]
            }
        }
    )

    models, _ = model_discovery.discover_models(
        _provider(api_type="gemini", base_url="https://generativelanguage.googleapis.com"),
        fetcher=fetcher,
    )

    assert models[0].id == "gemini-1.5-pro"
    assert models[0].name == "Gemini 1.5 Pro"
    assert fetcher.calls[0][1]["x-goog-api-key"] == "sk-secret"


def test_discover_passes_custom_headers_through():
    fetcher = RecordingFetcher(
        {"https://gw.example.com/v1/models": {"data": [{"id": "m"}]}}
    )

    model_discovery.discover_models(
        _provider(custom_headers={"X-Tenant": "t-1"}), fetcher=fetcher
    )

    assert fetcher.calls[0][1]["X-Tenant"] == "t-1"


def test_discover_dedupes_preserving_order():
    fetcher = RecordingFetcher(
        {"https://gw.example.com/v1/models": {"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}}
    )

    models, _ = model_discovery.discover_models(_provider(), fetcher=fetcher)

    assert [model.id for model in models] == ["b", "a"]


def test_discover_accepts_top_level_array():
    fetcher = RecordingFetcher({"https://gw.example.com/v1/models": ["m-1", "m-2"]})

    models, _ = model_discovery.discover_models(_provider(), fetcher=fetcher)

    assert [model.id for model in models] == ["m-1", "m-2"]


def test_discover_rejects_unsupported_api_type():
    with pytest.raises(model_discovery.ModelDiscoveryError, match="不支持自动发现"):
        model_discovery.discover_models(
            _provider(api_type="amazon-bedrock", base_url="https://bedrock.example.com"),
            fetcher=RecordingFetcher({}),
        )


def test_discover_rejects_missing_or_invalid_base_url():
    with pytest.raises(model_discovery.ModelDiscoveryError, match="未配置 base_url"):
        model_discovery.discover_models(_provider(base_url=""), fetcher=RecordingFetcher({}))
    with pytest.raises(model_discovery.ModelDiscoveryError, match="http"):
        model_discovery.discover_models(
            _provider(base_url="ftp://gw.example.com"), fetcher=RecordingFetcher({})
        )


def test_discover_reports_attempted_endpoints_without_credentials():
    fetcher = RecordingFetcher(
        {
            "https://gw.example.com/v1/models": model_discovery.ModelDiscoveryError(
                "远端返回 HTTP 401"
            ),
            "https://gw.example.com/models": model_discovery.ModelDiscoveryError(
                "远端返回 HTTP 401"
            ),
        }
    )

    with pytest.raises(model_discovery.ModelDiscoveryError) as excinfo:
        model_discovery.discover_models(_provider(), fetcher=fetcher)

    message = str(excinfo.value)
    assert "已尝试" in message
    assert "sk-secret" not in message
    assert "gw.example.com" in message


def test_discover_empty_payload_is_an_error():
    fetcher = RecordingFetcher(
        {
            "https://gw.example.com/v1/models": {"data": []},
            "https://gw.example.com/models": {"data": []},
        }
    )

    with pytest.raises(model_discovery.ModelDiscoveryError, match="为空"):
        model_discovery.discover_models(_provider(), fetcher=fetcher)


def test_default_fetcher_redacts_url_in_http_error(monkeypatch):
    import urllib.error

    def raise_http(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://gw.example.com/v1/models?key=secret", 403, "Forbidden", {}, None
        )

    monkeypatch.setattr(model_discovery.urllib.request, "urlopen", raise_http)

    with pytest.raises(model_discovery.ModelDiscoveryError) as excinfo:
        model_discovery.discover_models(_provider(), fetcher=None)

    assert "HTTP 403" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_discover_provider_models_reports_existing(store):
    store.add_provider("gw", base_url="https://gw.example.com/v1")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini")
    fetcher = RecordingFetcher(
        {
            "https://gw.example.com/v1/models": {
                "data": [
                    {"id": "gpt-4o-mini", "owned_by": "openai"},
                    {"id": "gpt-4o"},
                ]
            }
        }
    )

    result = model_registry.discover_provider_models("gw", fetcher=fetcher)

    assert result["source"] == "remote"
    assert result["source_url"] == "https://gw.example.com/v1/models"
    assert result["total"] == 2
    assert result["existing"] == ["gpt-4o-mini"]
    assert result["items"][0] == {
        "id": "gpt-4o-mini",
        "name": "gpt-4o-mini",
        "owned_by": "openai",
    }


def test_discover_provider_models_requires_existing_provider(store):
    with pytest.raises(model_registry.ProviderNotFoundError):
        model_registry.discover_provider_models("missing")


# --------------------------------------------------------------------------- #
# 生效解析
# --------------------------------------------------------------------------- #


def test_resolve_binds_openai_style_provider(store):
    store.add_provider(
        "gw",
        api_type="openai-compatible",
        base_url="https://gw.example.com/v1",
        api_key="sk-secret",
        name="自建网关",
    )
    model_registry.create_model(
        provider_id="gw",
        model="gpt-4o-mini",
        temperature=0.3,
        top_p=0.9,
        max_output_tokens=2048,
    )

    updates, view = model_registry.resolve_provider_settings_from_model("gw:gpt-4o-mini")

    assert updates["llm_provider"] == "openai"
    assert updates["openai_model"] == "gpt-4o-mini"
    assert updates["openai_base_url"] == "https://gw.example.com/v1"
    assert updates["openai_api_key"] == "sk-secret"
    assert updates["temperature"] == 0.3
    assert updates["top_p"] == 0.9
    assert updates["max_tokens"] == 2048
    assert updates["llm_model_id"] == "gw:gpt-4o-mini"
    assert view["provider_name"] == "自建网关"


def test_resolve_binds_ollama_style_provider(store):
    store.add_provider(
        "local", preset_type="ollama", api_type="openai-compatible",
        base_url="http://localhost:11434",
    )
    model_registry.create_model(provider_id="local", model="qwen2.5-coder:7b")

    updates, _ = model_registry.resolve_provider_settings_from_model("local:qwen2.5-coder:7b")

    assert updates["llm_provider"] == "ollama"
    assert updates["ollama_model"] == "qwen2.5-coder:7b"
    assert updates["ollama_base_url"] == "http://localhost:11434"
    # 本地运行时不写凭据字段
    assert "openai_api_key" not in updates


def test_resolve_leaves_unset_parameters_absent(store):
    store.add_provider("gw")
    model_registry.create_model(provider_id="gw", model="m")

    updates, _ = model_registry.resolve_provider_settings_from_model("gw:m")

    assert "temperature" not in updates
    assert "top_p" not in updates
    assert "max_tokens" not in updates


def test_resolve_returns_none_for_dangling_reference(store):
    # 条目不存在：按「未绑定」处理，不抛错（ADR-017）
    assert model_registry.resolve_llm_model("missing") is None
    assert model_registry.resolve_provider_settings_from_model("missing") is None
    assert model_registry.effective_model_view("missing") is None


def test_effective_view_summarizes_entry(store):
    store.add_provider("gw", name="自建网关")
    model_registry.create_model(provider_id="gw", model="gpt-4o-mini", name="GPT-4o mini")

    view = model_registry.effective_model_view("gw:gpt-4o-mini")

    assert view["id"] == "gw:gpt-4o-mini"
    assert view["provider_id"] == "gw"
    assert view["provider_name"] == "自建网关"
    assert view["preset_type"] == "openai-compatible"
    assert view["enabled"] is True

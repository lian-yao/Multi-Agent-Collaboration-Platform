"""Provider 配置的合并、镜像与校验（`doc/api.md` §5.8、ADR-014）。

单元级证据：存储读取顺序（Redis → PostgreSQL → 环境回退）、镜像写失败不阻断、
密钥脱敏与字段校验。真实 PostgreSQL / Redis 的联调证据见 D7-D8 验收记录，
本文件走内存替身，不代表真实存储环境的验收。
"""

import json
import logging
import re

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import AgentSettings
from app.core import checkpoint, provider_config
from app.core.checkpoint import UNSET


def _settings(**overrides) -> AgentSettings:
    return AgentSettings(_env_file=None, **overrides)


def _row(**overrides) -> dict:
    row = {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key": None,
        "temperature": None,
        "default_llm_model_id": None,
        "updated_by": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_provider_configs_table_matches_data_model():
    table = checkpoint.Base.metadata.tables["provider_configs"]

    assert list(table.columns.keys()) == [
        "id",
        "provider",
        "model",
        "base_url",
        "api_key",
        "temperature",
        "default_llm_model_id",
        "updated_by",
        "updated_at",
    ]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "id VARCHAR(20) NOT NULL" in ddl
    assert "provider VARCHAR(20)" in ddl
    assert "model VARCHAR(200)" in ddl
    assert "base_url VARCHAR(500)" in ddl
    assert "api_key TEXT" in ddl
    assert "temperature FLOAT" in ddl
    assert "default_llm_model_id VARCHAR(80)" in ddl
    assert table.primary_key.columns.keys() == ["id"]
    # 默认路由是逻辑引用：不建外键，条目删除时按未设置处理（ADR-017）。
    assert table.columns["default_llm_model_id"].foreign_keys == set()


def test_resolve_merges_stored_config_over_environment():
    base = _settings(
        llm_provider="openai",
        openai_model="env-model",
        openai_base_url="https://env.example.com/v1",
        openai_api_key="env-key",
        temperature=0.2,
    )

    resolved = provider_config.resolve_provider_settings(
        base,
        row=_row(
            provider="openai",
            model="stored-model",
            base_url="https://api.example.com/v1",
            api_key="stored-key",
            temperature=0.6,
        ),
    )

    assert resolved.openai_model == "stored-model"
    assert resolved.openai_base_url == "https://api.example.com/v1"
    assert resolved.openai_api_key == "stored-key"
    assert resolved.temperature == 0.6


def test_resolve_switches_provider_and_targets_matching_model_field():
    base = _settings(llm_provider="openai", openai_model="gpt-env", ollama_model="env:7b")

    resolved = provider_config.resolve_provider_settings(
        base, row=_row(provider="ollama", model="qwen2.5-coder:7b")
    )

    assert resolved.llm_provider == "ollama"
    assert resolved.ollama_model == "qwen2.5-coder:7b"
    # 未被覆盖的字段保持环境配置
    assert resolved.openai_model == "gpt-env"


def test_resolve_returns_base_when_no_stored_row():
    base = _settings(openai_model="env-model")

    assert provider_config.resolve_provider_settings(base, row=None) is base


def test_effective_view_masks_key_and_sanitizes_base_url():
    settings = _settings(
        llm_provider="openai",
        openai_model="gpt-4o-mini",
        openai_base_url="https://user:secret@api.example.com/v1?key=secret#frag",
        openai_api_key="sk-secret",
        temperature=0.3,
    )

    view = provider_config.effective_provider_view(settings, row=_row(updated_by="req-1"))

    assert view["base_url"] == "https://api.example.com/v1"
    assert view["api_key_configured"] is True
    assert "sk-secret" not in json.dumps(view)
    assert "secret" not in json.dumps(view)
    assert view["updated_by"] == "req-1"


def test_effective_view_reports_env_key_from_standard_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    settings = _settings(openai_model="gpt-4o-mini")

    view = provider_config.effective_provider_view(settings, row=None)

    assert view["api_key_configured"] is True


def test_row_read_prefers_redis_mirror(monkeypatch, memory_redis):
    memory_redis.data[provider_config.PROVIDER_CONFIG_CACHE_KEY] = json.dumps(
        _row(model="cached-model")
    )

    def unexpected() -> dict:
        raise AssertionError("命中镜像时不应回源 PostgreSQL")

    monkeypatch.setattr(checkpoint, "get_provider_config", unexpected)

    assert provider_config.provider_config_row()["model"] == "cached-model"


def test_row_read_falls_back_to_postgres_and_backfills_mirror(monkeypatch, memory_redis):
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: _row(model="stored-model"))

    row = provider_config.provider_config_row()

    assert row["model"] == "stored-model"
    cached = json.loads(memory_redis.data[provider_config.PROVIDER_CONFIG_CACHE_KEY])
    assert cached["model"] == "stored-model"


def test_row_read_falls_back_to_environment_when_store_unavailable(monkeypatch, caplog):
    def broken() -> dict:
        raise OSError("database down")

    monkeypatch.setattr(checkpoint, "get_provider_config", broken)
    base = _settings(openai_model="env-model")

    with caplog.at_level(logging.WARNING):
        resolved = provider_config.resolve_provider_settings(base)

    assert resolved is base
    assert "event=config.provider.read_fallback" in caplog.text


def test_row_read_ignores_broken_mirror(monkeypatch):
    class BrokenRedis:
        def get(self, key: str) -> str:
            raise ConnectionError("redis down")

        def set(self, key: str, value: str) -> None:
            raise ConnectionError("redis down")

    provider_config.set_redis_factory(lambda: BrokenRedis())
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: _row(model="stored-model"))

    assert provider_config.provider_config_row()["model"] == "stored-model"


def test_update_writes_store_then_mirror(monkeypatch, caplog, memory_redis):
    captured: dict = {}

    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: None)

    def fake_upsert(**kwargs) -> dict:
        captured.update(kwargs)
        return _row(
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
            temperature=kwargs.get("temperature"),
        )

    monkeypatch.setattr(checkpoint, "upsert_provider_config", fake_upsert)

    with caplog.at_level(logging.INFO):
        row = provider_config.update_provider_config(
            provider="openai",
            model=" gpt-4o-mini ",
            base_url="https://api.example.com/v1",
            api_key="sk-secret",
            temperature=0.4,
            actor="req-1",
        )

    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"  # 去空白后写入
    assert captured["updated_by"] == "req-1"
    assert row["model"] == "gpt-4o-mini"
    # 镜像同步写入，且日志只记 set/unset，不出现密钥原值
    assert provider_config.PROVIDER_CONFIG_CACHE_KEY in memory_redis.data
    assert "event=config.provider.updated" in caplog.text
    assert "api_key=set" in caplog.text
    assert "sk-secret" not in caplog.text


def test_update_ignores_mirror_failure(monkeypatch, caplog):
    class BrokenRedis:
        def get(self, key: str) -> None:
            return None

        def set(self, key: str, value: str) -> None:
            raise ConnectionError("redis down")

    provider_config.set_redis_factory(lambda: BrokenRedis())
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: None)
    monkeypatch.setattr(
        checkpoint, "upsert_provider_config", lambda **kwargs: _row(model="stored-model")
    )

    with caplog.at_level(logging.WARNING):
        row = provider_config.update_provider_config(model="stored-model")

    assert row["model"] == "stored-model"  # 事实源写入成功即视为成功
    assert "event=config.provider.mirror_write_failed" in caplog.text


def test_update_passes_unset_through_for_partial_write(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: None)

    def fake_upsert(**kwargs) -> dict:
        captured.update(kwargs)
        return _row(model="stored-model")

    monkeypatch.setattr(checkpoint, "upsert_provider_config", fake_upsert)

    provider_config.update_provider_config(model="stored-model")

    assert captured["provider"] is UNSET
    assert captured["base_url"] is UNSET
    assert captured["api_key"] is UNSET
    assert captured["temperature"] is UNSET


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("provider", "anthropic", "provider 必须是"),
        ("provider", "", "provider 必须是"),
        ("model", "   ", "model 不能为空"),
        ("model", "x" * 201, "model 不能超过"),
        ("base_url", "ftp://api.example.com", "base_url 必须是 http(s) URL"),
        ("base_url", "not-a-url", "base_url 必须是 http(s) URL"),
        ("base_url", "x" * 501, "base_url 不能超过"),
        ("api_key", "   ", "api_key 不能为空"),
        ("api_key", "x" * 501, "api_key 不能超过"),
        ("temperature", -0.1, "temperature 必须在"),
        ("temperature", 2.1, "temperature 必须在"),
        ("temperature", "0.5", "temperature 必须是数值"),
    ],
)
def test_update_rejects_invalid_values(field, value, message):
    with pytest.raises(provider_config.ProviderConfigError, match=re.escape(message)):
        provider_config.update_provider_config(**{field: value})


def test_blank_base_url_is_cleared_to_none(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: None)

    def fake_upsert(**kwargs) -> dict:
        captured.update(kwargs)
        return _row()

    monkeypatch.setattr(checkpoint, "upsert_provider_config", fake_upsert)

    provider_config.update_provider_config(base_url="   ")

    assert captured["base_url"] is None

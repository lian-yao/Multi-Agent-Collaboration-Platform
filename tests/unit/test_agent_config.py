"""Agent 配置覆盖的合并、校验与表契约（`doc/api.md` §5.7、ADR-013）。"""

import logging

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import AgentSettings
from app.core import agent_config, checkpoint
from app.core.checkpoint import UNSET


def _settings(**overrides) -> AgentSettings:
    return AgentSettings(_env_file=None, **overrides)


def test_agent_configs_table_matches_data_model():
    table = checkpoint.Base.metadata.tables["agent_configs"]
    assert list(table.columns.keys()) == [
        "agent_id",
        "model",
        "temperature",
        "updated_by",
        "updated_at",
    ]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "agent_id VARCHAR(50) NOT NULL" in ddl
    assert "model VARCHAR(200)" in ddl
    assert "temperature FLOAT" in ddl
    assert "updated_by VARCHAR(100)" in ddl
    assert "updated_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert table.primary_key.columns.keys() == ["agent_id"]


def test_resolve_applies_override(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "collector", "model": "custom:1b", "temperature": 0.9}],
    )

    resolved = agent_config.resolve_agent_settings(
        "collector",
        _settings(llm_provider="ollama", ollama_model="env:7b", temperature=0.2),
    )

    assert resolved.ollama_model == "custom:1b"
    assert resolved.temperature == 0.9
    # 未覆盖的 provider 字段保持环境配置
    assert resolved.llm_provider == "ollama"


def test_resolve_falls_back_per_field(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "analyst", "model": None, "temperature": 0.5}],
    )

    resolved = agent_config.resolve_agent_settings(
        "analyst",
        _settings(ollama_model="env:7b", temperature=0.2),
    )

    assert resolved.ollama_model == "env:7b"
    assert resolved.temperature == 0.5


def test_resolve_uses_openai_field_for_openai_provider(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "reporter", "model": "gpt-x", "temperature": None}],
    )

    resolved = agent_config.resolve_agent_settings(
        "reporter",
        _settings(llm_provider="openai", openai_model="gpt-env", temperature=0.2),
    )

    assert resolved.openai_model == "gpt-x"
    assert resolved.temperature == 0.2


def test_resolve_returns_base_when_agent_has_no_override(monkeypatch):
    monkeypatch.setattr(checkpoint, "list_agent_configs", lambda: [])
    base = _settings(ollama_model="env:7b")

    assert agent_config.resolve_agent_settings("collector", base) is base


def test_resolve_falls_back_when_read_fails(monkeypatch, caplog):
    def broken() -> list[dict]:
        raise OSError("database down")

    monkeypatch.setattr(checkpoint, "list_agent_configs", broken)
    base = _settings(ollama_model="env:7b")

    with caplog.at_level(logging.WARNING):
        resolved = agent_config.resolve_agent_settings("collector", base)

    assert resolved is base
    assert "event=config.read_fallback" in caplog.text


def test_update_validates_and_writes(monkeypatch, caplog):
    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)

    def fake_upsert(agent_id, *, model=UNSET, temperature=UNSET, updated_by=None):
        captured.update(
            agent_id=agent_id, model=model, temperature=temperature, updated_by=updated_by
        )
        return {"agent_id": agent_id, "model": model, "temperature": temperature}

    monkeypatch.setattr(checkpoint, "upsert_agent_config", fake_upsert)

    with caplog.at_level(logging.INFO):
        row = agent_config.update_agent_config(
            "collector", model="  new:1b  ", temperature=0.4, actor="req-1"
        )

    assert captured == {
        "agent_id": "collector",
        "model": "new:1b",  # 去空白后写入
        "temperature": 0.4,
        "updated_by": "req-1",
    }
    assert row["model"] == "new:1b"
    assert "event=config.agent.updated" in caplog.text


def test_update_passes_unset_and_none_through(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)

    def fake_upsert(agent_id, *, model=UNSET, temperature=UNSET, updated_by=None):
        captured.update(model=model, temperature=temperature)
        return {"agent_id": agent_id, "model": None, "temperature": None}

    monkeypatch.setattr(checkpoint, "upsert_agent_config", fake_upsert)

    agent_config.update_agent_config("collector", model=None)
    assert captured == {"model": None, "temperature": UNSET}

    captured.clear()
    agent_config.update_agent_config("collector", temperature=0.1)
    assert captured == {"model": UNSET, "temperature": 0.1}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "   "},
        {"model": "x" * 201},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
    ],
)
def test_update_rejects_invalid_values(monkeypatch, kwargs):
    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(
        checkpoint,
        "upsert_agent_config",
        lambda *args, **rest: pytest.fail("非法值不应写入"),
    )

    with pytest.raises(agent_config.AgentConfigError):
        agent_config.update_agent_config("collector", **kwargs)

"""`GET/PUT /api/v1/config/provider` 契约（`doc/api.md` §5.8、ADR-014）。

用内存覆盖表 + 内存 Redis 镜像替身，验证权限边界、脱敏与生效值合并；
真实 PostgreSQL / Redis 的联调证据见 D7-D8 验收记录。
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import app.api.main as api_main
from app.config import AdminSettings, AgentSettings
from app.core import checkpoint, provider_config
from app.core.checkpoint import UNSET

TOKEN = "test-admin-token"
ENV_SETTINGS = dict(
    _env_file=None,
    llm_provider="openai",
    openai_model="env-model",
    openai_base_url="https://env.example.com/v1",
    openai_api_key="env-key",
    temperature=0.2,
)
ROW_FIELDS = ("provider", "model", "base_url", "api_key", "temperature")


class MemoryRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture
def provider_api(monkeypatch):
    """隔离环境配置、覆盖表与 Redis 镜像。"""

    redis = MemoryRedis()
    provider_config.set_redis_factory(lambda: redis)
    state: dict = {"row": None, "calls": []}

    def upsert(**kwargs) -> dict:
        state["calls"].append(kwargs)
        row = dict(state["row"] or {field: None for field in ROW_FIELDS})
        for field in ROW_FIELDS:
            if kwargs.get(field, UNSET) is not UNSET:
                row[field] = kwargs[field]
        row["updated_by"] = kwargs.get("updated_by")
        row["updated_at"] = None
        state["row"] = row
        return row

    monkeypatch.setattr(api_main, "get_settings", lambda: AgentSettings(**ENV_SETTINGS))
    monkeypatch.setattr(
        api_main, "get_admin_settings", lambda: AdminSettings(_env_file=None, admin_token=TOKEN)
    )
    monkeypatch.setattr(checkpoint, "get_provider_config", lambda: state["row"])
    monkeypatch.setattr(checkpoint, "upsert_provider_config", upsert)
    yield TestClient(api_main.app), state, redis
    provider_config.set_redis_factory(None)


def _put(client, body=None, token=TOKEN, headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["X-Admin-Token"] = token
    return client.put("/api/v1/config/provider", json=body or {}, headers=request_headers)


def test_get_returns_effective_env_config_without_key(provider_api):
    client, _, _ = provider_api

    response = client.get("/api/v1/config/provider")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openai"
    assert body["model"] == "env-model"
    assert body["base_url"] == "https://env.example.com/v1"
    assert body["api_key_configured"] is True
    assert "api_key" not in body
    assert "env-key" not in response.text


@pytest.mark.parametrize("token", [None, "wrong-token", ""])
def test_put_rejected_without_valid_token(provider_api, token):
    client, state, redis = provider_api

    response = _put(client, body={"model": "gpt-4o-mini"}, token=token)

    assert response.status_code == 403
    assert response.json()["code"] == "CONFIG_WRITE_FORBIDDEN"
    assert TOKEN not in response.text
    assert state["calls"] == []
    assert redis.data == {}


def test_put_fails_closed_when_token_not_configured(monkeypatch, provider_api):
    client, state, _ = provider_api
    monkeypatch.setattr(
        api_main, "get_admin_settings", lambda: AdminSettings(_env_file=None, admin_token="")
    )

    response = _put(client, body={"model": "gpt-4o-mini"})

    assert response.status_code == 403
    assert state["calls"] == []


def test_put_persists_stored_values_and_mirrors_them(provider_api):
    client, state, redis = provider_api

    response = _put(
        client,
        body={
            "model": "gpt-4o-mini",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-secret",
            "temperature": 0.4,
        },
        headers={"X-Request-ID": "req-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "gpt-4o-mini"
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["temperature"] == 0.4
    assert body["updated_by"] == "req-1"
    assert "api_key" not in body
    assert "sk-secret" not in response.text

    # 覆盖值落事实源，并镜像进 Redis
    assert state["calls"][0]["model"] == "gpt-4o-mini"
    cached = json.loads(redis.data[provider_config.PROVIDER_CONFIG_CACHE_KEY])
    assert cached["api_key"] == "sk-secret"

    # 后续读取返回覆盖后的生效值
    follow_up = client.get("/api/v1/config/provider")
    assert follow_up.json()["model"] == "gpt-4o-mini"


def test_put_explicit_null_clears_override(provider_api):
    client, _, _ = provider_api
    _put(client, body={"model": "gpt-4o-mini", "temperature": 0.4})

    response = _put(client, body={"model": None})

    assert response.status_code == 200
    assert response.json()["model"] == "env-model"  # 回退环境配置
    assert response.json()["temperature"] == 0.4  # 其他覆盖不受影响


def test_put_switching_provider_reports_matching_model(provider_api):
    client, _, _ = provider_api

    response = _put(client, body={"provider": "ollama", "model": "qwen2.5-coder:7b"})

    assert response.status_code == 200
    assert response.json()["provider"] == "ollama"
    assert response.json()["model"] == "qwen2.5-coder:7b"


def test_put_empty_body_is_rejected(provider_api):
    client, state, _ = provider_api

    response = _put(client, body={})

    assert response.status_code == 422
    assert state["calls"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "anthropic"},
        {"base_url": "ftp://api.example.com"},
        {"base_url": "not-a-url"},
        {"api_key": "   "},
        {"temperature": 3},
    ],
)
def test_put_invalid_values_are_rejected(provider_api, body):
    client, state, _ = provider_api

    response = _put(client, body=body)

    assert response.status_code == 422
    assert state["calls"] == []


def test_put_storage_failure_returns_503(monkeypatch, provider_api):
    client, _, _ = provider_api

    def broken(**kwargs):
        raise SQLAlchemyError("database down")

    monkeypatch.setattr(checkpoint, "upsert_provider_config", broken)

    response = _put(client, body={"model": "gpt-4o-mini"})

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"

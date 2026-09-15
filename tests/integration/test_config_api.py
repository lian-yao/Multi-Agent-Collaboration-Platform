"""`PATCH /api/v1/config/agents/{agent_id}` 契约（`doc/api.md` §5.7、ADR-013）。

写入不鉴权（ADR-015，2026-09-15 取消 `ADMIN_TOKEN`），因此这里不再有 403 用例。
"""

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.config import AgentSettings
from app.core.checkpoint import UNSET

ENV_SETTINGS = dict(
    _env_file=None,
    llm_provider="ollama",
    ollama_model="env:7b",
    temperature=0.2,
)


@pytest.fixture
def config_api(monkeypatch):
    """隔离环境配置与覆盖表：内存覆盖表替身。"""

    state: dict = {"rows": {}, "calls": []}

    def reader() -> dict:
        return state["rows"]

    def update(agent_id, *, model=UNSET, temperature=UNSET, actor=None):
        state["calls"].append(
            {"agent_id": agent_id, "model": model, "temperature": temperature, "actor": actor}
        )
        row = state["rows"].setdefault(agent_id, {"agent_id": agent_id, "model": None,
                                                  "temperature": None})
        if model is not UNSET:
            row["model"] = model
        if temperature is not UNSET:
            row["temperature"] = temperature
        return row

    monkeypatch.setattr(api_main, "get_settings", lambda: AgentSettings(**ENV_SETTINGS))
    monkeypatch.setattr(api_main, "agent_config_reader", reader)
    monkeypatch.setattr(api_main, "update_agent_config", update)
    return TestClient(api_main.app), state


def _patch(client, agent_id="collector", body=None, headers=None):
    request_headers = dict(headers or {})
    return client.patch(f"/api/v1/config/agents/{agent_id}", json=body or {}, headers=request_headers)


def test_write_succeeds_without_any_token(config_api):
    """取消令牌后，裸请求即可写入（ADR-015）。"""

    client, state = config_api

    response = _patch(client, body={"temperature": 0.5})

    assert response.status_code == 200
    assert response.json()["temperature"] == 0.5
    assert state["calls"] == [
        {"agent_id": "collector", "model": UNSET, "temperature": 0.5, "actor": None}
    ]


def test_patch_updates_effective_config(config_api):
    client, state = config_api

    response = _patch(client, body={"model": "custom:1b", "temperature": 0.4})

    assert response.status_code == 200
    assert response.json() == {
        "id": "collector",
        "name": "信息收集 Agent",
        "role": "collector",
        "model": "custom:1b",
        "provider": "ollama",
        "temperature": 0.4,
        "status": "idle",
    }
    assert state["calls"] == [
        {"agent_id": "collector", "model": "custom:1b", "temperature": 0.4, "actor": None}
    ]

    # 详情与列表都返回生效值；未覆盖的角色仍是环境配置
    assert client.get("/api/v1/agents/collector").json()["model"] == "custom:1b"
    items = {item["id"]: item for item in client.get("/api/v1/agents").json()["items"]}
    assert items["collector"]["temperature"] == 0.4
    assert items["analyst"]["model"] == "env:7b"
    assert items["analyst"]["temperature"] == 0.2


def test_partial_patch_keeps_other_field(config_api):
    client, _ = config_api
    _patch(client, body={"model": "custom:1b"})

    response = _patch(client, body={"temperature": 0.9})

    assert response.status_code == 200
    assert response.json()["model"] == "custom:1b"
    assert response.json()["temperature"] == 0.9


def test_null_clears_override(config_api):
    client, state = config_api
    _patch(client, body={"model": "custom:1b", "temperature": 0.9})

    response = _patch(client, body={"model": None})

    assert response.status_code == 200
    assert response.json()["model"] == "env:7b"  # 回退环境配置
    assert response.json()["temperature"] == 0.9
    assert state["calls"][-1]["model"] is None


def test_request_id_is_recorded_as_actor(config_api):
    client, state = config_api

    _patch(client, body={"temperature": 0.3}, headers={"X-Request-ID": "req-42"})

    assert state["calls"][-1]["actor"] == "req-42"


def test_unknown_agent_is_404(config_api):
    client, state = config_api

    response = _patch(client, agent_id="missing", body={"temperature": 0.3})

    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_NOT_FOUND"
    assert state["calls"] == []


@pytest.mark.parametrize(
    "body",
    [{}, {"temperature": 2.5}, {"temperature": -0.1}, {"model": "   "}, {"model": "x" * 201}],
)
def test_invalid_body_is_422(config_api, body):
    client, state = config_api

    response = _patch(client, body=body)

    assert response.status_code == 422
    assert state["calls"] == []


def test_write_failure_is_503(config_api, monkeypatch):
    client, _ = config_api

    def broken(*args, **kwargs):
        raise OSError("database down")

    monkeypatch.setattr(api_main, "update_agent_config", broken)

    response = _patch(client, body={"temperature": 0.3})

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"

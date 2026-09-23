"""`GET/PUT /api/v1/config/search` 契约（`doc/api.md` §5.24、ADR-039）。

用内存覆盖表替身，验证生效值合并、端点联动、脱敏与缓存失效；
真实 PostgreSQL 的联调证据见 ADR-039 的验收记录。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import app.api.main as api_main
import app.mcp.registry as mcp_registry
import app.tools.config as tools_config
from app.core import checkpoint
from app.core.checkpoint import UNSET
from app.tools.config import (
    DOUBAO_SEARCH_ENDPOINT,
    DUCKDUCKGO_SEARCH_ENDPOINT,
    ToolSettings,
)

ENV_SETTINGS = dict(
    _env_file=None,
    search_provider="duckduckgo",
    search_endpoint="http://search-gateway:8800/search",
    search_api_key="",
)
"""一键部署的环境基线：本地网关 + 无凭证（`deploy/.env` 的默认形态）。"""

ROW_FIELDS = ("search_provider", "search_endpoint", "search_api_key")


@pytest.fixture
def search_api(monkeypatch):
    """隔离环境基线、覆盖表与两层缓存失效口。"""

    state: dict = {"row": None, "calls": [], "settings_resets": 0, "registry_resets": 0}

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

    def reset_settings() -> None:
        state["settings_resets"] += 1

    def reset_registry() -> None:
        state["registry_resets"] += 1

    monkeypatch.setattr(api_main, "env_tool_settings", lambda: ToolSettings(**ENV_SETTINGS))
    monkeypatch.setattr(checkpoint, "get_tool_config", lambda: state["row"])
    monkeypatch.setattr(checkpoint, "upsert_tool_config", upsert)
    monkeypatch.setattr(tools_config, "reset_tool_settings_cache", reset_settings)
    monkeypatch.setattr(mcp_registry, "reset_tool_registry_cache", reset_registry)
    yield TestClient(api_main.app), state


def _put(client, body=None, headers=None):
    return client.put(
        "/api/v1/config/search", json=body or {}, headers=dict(headers or {})
    )


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def test_get_returns_env_baseline_without_key(search_api):
    client, _ = search_api

    response = client.get("/api/v1/config/search")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "duckduckgo"
    assert body["endpoint"] == "http://search-gateway:8800/search"
    assert body["api_key_configured"] is False
    assert body["overridden"] is False
    assert body["env_provider"] == "duckduckgo"
    assert body["env_endpoint"] == "http://search-gateway:8800/search"
    assert "api_key" not in body


def test_get_exposes_channel_catalog(search_api):
    """前端的渠道下拉靠这份目录，两个渠道都必须出现且标注是否要 key。"""

    client, _ = search_api

    channels = client.get("/api/v1/config/search").json()["channels"]

    by_id = {channel["id"]: channel for channel in channels}
    assert set(by_id) == {"duckduckgo", "volcengine"}
    assert by_id["duckduckgo"]["needs_api_key"] is False
    assert by_id["duckduckgo"]["default_endpoint"] == DUCKDUCKGO_SEARCH_ENDPOINT
    assert by_id["volcengine"]["needs_api_key"] is True
    assert by_id["volcengine"]["default_endpoint"] == DOUBAO_SEARCH_ENDPOINT


# --------------------------------------------------------------------------- #
# 写：换渠道的端点联动（本模块最容易错的一处）
# --------------------------------------------------------------------------- #


def test_put_switch_channel_materializes_channel_endpoint(search_api):
    """切到豆包且不给端点 ⇒ 端点落库为豆包官方地址。

    不这么做就会拿着豆包的 key 去打只认 DuckDuckGo 契约的本地网关。
    """

    client, state = search_api

    response = _put(client, body={"provider": "volcengine"})

    assert response.status_code == 200
    assert response.json()["provider"] == "volcengine"
    assert response.json()["endpoint"] == DOUBAO_SEARCH_ENDPOINT
    assert state["calls"][0]["search_endpoint"] == DOUBAO_SEARCH_ENDPOINT


def test_put_blank_endpoint_does_not_bypass_switch(search_api):
    """回归：前端把空输入框发成 `endpoint: \"\"` 时，端点联动不能被绕过。"""

    client, state = search_api

    response = _put(client, body={"provider": "volcengine", "endpoint": ""})

    assert response.status_code == 200
    assert response.json()["endpoint"] == DOUBAO_SEARCH_ENDPOINT
    assert state["calls"][0]["search_endpoint"] == DOUBAO_SEARCH_ENDPOINT


def test_put_frontend_switch_payload_keeps_channel_endpoint(search_api):
    """前端换渠道时发的是 `{provider, endpoint: null}`（见 SearchChannelPanel 的
    `buildSearchUpdate`）；`null` 与空串必须走同一条联动，否则界面一换渠道就换错端点。
    """

    client, state = search_api

    response = _put(client, body={"provider": "volcengine", "endpoint": None})

    assert response.status_code == 200
    assert response.json()["endpoint"] == DOUBAO_SEARCH_ENDPOINT
    assert state["calls"][0]["search_endpoint"] == DOUBAO_SEARCH_ENDPOINT


def test_put_switch_back_keeps_env_endpoint(search_api):
    """切回 DuckDuckGo ⇒ 沿用环境端点（保住本地网关），而不是公网 DDG。"""

    client, _ = search_api
    _put(client, body={"provider": "volcengine"})

    response = _put(client, body={"provider": "duckduckgo"})

    assert response.status_code == 200
    assert response.json()["endpoint"] == "http://search-gateway:8800/search"


def test_put_same_provider_does_not_freeze_endpoint(search_api):
    """渠道没变 ⇒ 不发明端点，空端点即「回退环境端点」。"""

    client, state = search_api

    response = _put(client, body={"provider": "duckduckgo", "endpoint": ""})

    assert response.status_code == 200
    assert response.json()["endpoint"] == "http://search-gateway:8800/search"
    assert state["calls"][0]["search_endpoint"] is None


def test_put_explicit_endpoint_wins_over_channel_default(search_api):
    """显式端点优先于渠道默认：自建网关、代理地址都靠这一条。"""

    client, _ = search_api

    response = _put(
        client,
        body={"provider": "volcengine", "endpoint": "https://proxy.example.com/search"},
    )

    assert response.status_code == 200
    assert response.json()["endpoint"] == "https://proxy.example.com/search"


# --------------------------------------------------------------------------- #
# 写：凭据只写入不回读
# --------------------------------------------------------------------------- #


def test_put_api_key_is_write_only(search_api):
    client, state = search_api

    response = _put(client, body={"api_key": "sk-secret"})

    assert response.status_code == 200
    assert response.json()["api_key_configured"] is True
    assert "api_key" not in response.json()
    assert "sk-secret" not in response.text
    assert state["calls"][0]["search_api_key"] == "sk-secret"


def test_put_explicit_null_clears_key(search_api):
    client, state = search_api
    _put(client, body={"provider": "volcengine", "api_key": "sk-secret"})

    response = _put(client, body={"api_key": None})

    assert response.status_code == 200
    assert response.json()["api_key_configured"] is False
    assert state["row"]["search_api_key"] is None
    # 其他覆盖不受影响
    assert response.json()["provider"] == "volcengine"


def test_get_after_clearing_every_override_reports_not_overridden(search_api):
    """回归（2026-09-24 实机踩到）：清空覆盖之后**行还在**（三列皆 None）。

    此时必须报 `overridden=False`、且不报残留的 `updated_by`/`updated_at`。
    若按 `bool(row)` 判，界面会永久停在「已覆盖环境配置」——覆盖值一个都没有，
    用户却再也回不到「跟随环境配置」，「恢复环境配置」也永远点不完。
    """

    client, state = search_api
    _put(client, body={"provider": "volcengine", "api_key": "sk-secret"})

    cleared = _put(client, body={"provider": None, "endpoint": None, "api_key": None})

    assert cleared.status_code == 200
    row = state["row"]
    assert row is not None, "覆盖行应当留在库里，只是没有覆盖值"
    assert all(row[field] is None for field in ROW_FIELDS)
    body = cleared.json()
    assert body["overridden"] is False
    assert body["updated_by"] is None
    assert body["updated_at"] is None
    # 回退环境基线
    assert body["provider"] == "duckduckgo"
    assert body["endpoint"] == "http://search-gateway:8800/search"
    assert body["api_key_configured"] is False


# --------------------------------------------------------------------------- #
# 写：输入校验与故障
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "baidu"},
        {"endpoint": "ftp://search.example.com"},
        {"endpoint": "not-a-url"},
        {"api_key": "   "},
    ],
)
def test_put_invalid_values_are_rejected(search_api, body):
    client, state = search_api

    response = _put(client, body=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert state["calls"] == []


def test_put_empty_body_is_rejected(search_api):
    client, state = search_api

    response = _put(client, body={})

    assert response.status_code == 422
    assert state["calls"] == []


def test_put_storage_failure_returns_503(search_api, monkeypatch):
    client, _ = search_api

    def broken(**kwargs):
        raise SQLAlchemyError("database down")

    monkeypatch.setattr(checkpoint, "upsert_tool_config", broken)

    response = _put(client, body={"provider": "volcengine"})

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# 缓存失效
# --------------------------------------------------------------------------- #


def test_write_invalidates_both_tool_caches(search_api):
    """两层都要失效：只清 settings 缓存的话，注册表里的旧工具实例仍攥着旧配置。"""

    client, state = search_api

    _put(client, body={"provider": "volcengine"})

    assert state["settings_resets"] == 1
    assert state["registry_resets"] == 1


def test_get_does_not_touch_caches(search_api):
    client, state = search_api

    client.get("/api/v1/config/search")

    assert state["settings_resets"] == 0
    assert state["registry_resets"] == 0


# --------------------------------------------------------------------------- #
# 记录写入者
# --------------------------------------------------------------------------- #


def test_put_records_actor_and_returns_it(search_api):
    client, state = search_api

    response = _put(
        client, body={"provider": "volcengine"}, headers={"X-Request-ID": "req-7"}
    )

    assert response.status_code == 200
    assert state["calls"][0]["updated_by"] == "req-7"
    assert response.json()["updated_by"] == "req-7"

# --------------------------------------------------------------------------- #
# 端点与渠道的契约错配：只诊断，不篡改
# --------------------------------------------------------------------------- #


def test_endpoint_provider_mismatch_only_diagnoses():
    """显式端点一律尊重（成员 C 冻结的契约），错配只回一句人话。

    第一版曾在 `ToolSettings` 校验器里把端点改写成豆包默认，直接踩掉了
    `test_search_tool_volcengine.py::test_explicit_endpoint_wins_over_provider_default`。
    校验器分不出「有意接到自建网关」与「端点只是取了默认值」，所以改成只诊断。
    """

    gateway = "http://search-gateway:8800/search"

    def settings(**kwargs) -> ToolSettings:
        return ToolSettings(_env_file=None, **kwargs)

    # 内置网关是 DuckDuckGo 契约：配成豆包渠道就是错配
    mismatch = settings(search_provider="volcengine", search_endpoint=gateway)
    assert mismatch.search_endpoint == gateway, "必须原样保留显式端点"
    assert "契约不匹配" in mismatch.endpoint_provider_mismatch()

    # 正常组合一律不报警
    assert settings(search_provider="volcengine").endpoint_provider_mismatch() == ""
    assert settings(search_provider="duckduckgo", search_endpoint=gateway).endpoint_provider_mismatch() == ""
    # 自建网关 / 代理是操作者的显式选择，不判
    assert (
        settings(
            search_provider="volcengine",
            search_endpoint="https://proxy.example.com/search",
        ).endpoint_provider_mismatch()
        == ""
    )


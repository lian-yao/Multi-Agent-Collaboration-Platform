"""`PATCH /api/v1/config/agents/{agent_id}` 契约（`doc/api.md` §5.7、ADR-013）。

写入不鉴权（ADR-015，2026-09-15 取消 `ADMIN_TOKEN`），因此这里不再有 403 用例。
"""

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.config import AgentSettings
from app.core import model_registry
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

    def update(
        agent_id,
        *,
        model=UNSET,
        temperature=UNSET,
        llm_model_id=UNSET,
        top_p=UNSET,
        max_output_tokens=UNSET,
        reasoning_type=UNSET,
        tool_names=UNSET,
        actor=None,
    ):
        state["calls"].append(
            {
                "agent_id": agent_id,
                "model": model,
                "temperature": temperature,
                "llm_model_id": llm_model_id,
                "top_p": top_p,
                "max_output_tokens": max_output_tokens,
                "reasoning_type": reasoning_type,
                "tool_names": tool_names,
                "actor": actor,
            }
        )
        row = state["rows"].setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "model": None,
                "temperature": None,
                "llm_model_id": None,
                "top_p": None,
                "max_output_tokens": None,
                "reasoning_type": None,
                "tool_names": None,
            },
        )
        for field, value in (
            ("model", model),
            ("temperature", temperature),
            ("llm_model_id", llm_model_id),
            ("top_p", top_p),
            ("max_output_tokens", max_output_tokens),
            ("reasoning_type", reasoning_type),
            ("tool_names", tool_names),
        ):
            if value is not UNSET:
                row[field] = value
        return row

    monkeypatch.setattr(api_main, "get_settings", lambda: AgentSettings(**ENV_SETTINGS))
    monkeypatch.setattr(api_main, "agent_config_reader", reader)
    monkeypatch.setattr(api_main, "update_agent_config", update)
    # 角色目录隔离：让 `_agent_response` 走 `_AGENT_NAMES` 回退（builtin=True/
    # description=None/enabled=True），不触碰真实 agent_registry 表。
    monkeypatch.setattr(api_main, "list_agent_registry", lambda: [])
    # 模型清单走固定替身：这些用例只关心 §5.7 的契约，不触碰真实注册表。
    monkeypatch.setattr(
        model_registry,
        "list_models",
        lambda **kwargs: {
            "items": [
                {
                    "id": "gateway-main:gpt-4o-mini",
                    "provider_id": "gateway-main",
                    "model": "gpt-4o-mini",
                    "name": "GPT-4o mini",
                    "enabled": True,
                }
            ],
            "total": 1,
        },
    )
    # 生效解析同样走替身：真实解析要读 llm_models / llm_providers 两张表。
    monkeypatch.setattr(
        model_registry,
        "resolve_provider_settings_from_model",
        lambda model_id: ({"llm_model_id": model_id}, {"llm_model_id": model_id}),
    )
    return TestClient(api_main.app), state


def _call(**overrides) -> dict:
    """补全 §5.7 的写入调用记录：未断言的字段保持 UNSET 语义。"""

    base = {
        "agent_id": "collector",
        "model": UNSET,
        "temperature": UNSET,
        "llm_model_id": UNSET,
        "top_p": UNSET,
        "max_output_tokens": UNSET,
        "reasoning_type": UNSET,
        "tool_names": UNSET,
        "actor": None,
    }
    base.update(overrides)
    return base


def _patch(client, agent_id="collector", body=None, headers=None):
    request_headers = dict(headers or {})
    return client.patch(f"/api/v1/config/agents/{agent_id}", json=body or {}, headers=request_headers)


def test_write_succeeds_without_any_token(config_api):
    """取消令牌后，裸请求即可写入（ADR-015）。"""

    client, state = config_api

    response = _patch(client, body={"temperature": 0.5})

    assert response.status_code == 200
    assert response.json()["temperature"] == 0.5
    assert state["calls"] == [_call(temperature=0.5)]


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
        "provider_name": None,
        "llm_model_id": None,
        "temperature": 0.4,
        "top_p": None,
        "max_output_tokens": None,
        "reasoning_type": "none",
        "status": "idle",
        # 只覆盖了两个字段，`override_keys` 就该只有这两个（§5.7）。
        "override_keys": ["model", "temperature"],
        # 角色目录回退到内置 `_AGENT_NAMES` 时的元数据。
        "builtin": True,
        "description": None,
        "enabled": True,
        # 没碰过工具白名单 → 仍是「未配置」（不等于空名单）。
        "tool_names": None,
        # 目录回退时这两个字段就是空（ADR-036）：不在 API 层编默认值。
        "system_prompt": None,
        "icon": None,
    }
    assert state["calls"] == [
        _call(model="custom:1b", temperature=0.4)
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


# --------------------------------------------------------------------------- #
# ADR-017：注册表绑定与特化调参（§5.7 扩展）
# --------------------------------------------------------------------------- #


def test_patch_binds_registry_model_and_specialized_parameters(config_api):
    """`llm_model_id` / `top_p` / `max_output_tokens` / `reasoning_type` 一次写入。"""

    client, state = config_api

    response = _patch(
        client,
        body={
            "llm_model_id": "gateway-main:gpt-4o-mini",
            "top_p": 0.9,
            "max_output_tokens": 2048,
            "reasoning_type": "openai",
        },
    )

    assert response.status_code == 200
    assert response.json()["llm_model_id"] == "gateway-main:gpt-4o-mini"
    assert response.json()["top_p"] == 0.9
    assert response.json()["max_output_tokens"] == 2048
    assert response.json()["reasoning_type"] == "openai"
    assert response.json()["override_keys"] == [
        "llm_model_id",
        "top_p",
        "max_output_tokens",
        "reasoning_type",
    ]
    assert state["calls"] == [
        _call(
            llm_model_id="gateway-main:gpt-4o-mini",
            top_p=0.9,
            max_output_tokens=2048,
            reasoning_type="openai",
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"top_p": 1.5},
        {"top_p": -0.1},
        {"max_output_tokens": 0},
        {"llm_model_id": "x" * 81},
    ],
)
def test_patch_rejects_out_of_range_specialized_parameters(config_api, body):
    """越界取值在 Pydantic 层拦成 422，不触达写入路径。"""

    client, state = config_api

    response = _patch(client, body=body)

    assert response.status_code == 422
    assert state["calls"] == []


def test_config_agents_list_returns_roles_and_model_catalog(config_api):
    """`GET /api/v1/config/agents` 一次返回全部角色 + 可选模型清单（§5.7）。"""

    client, _ = config_api

    response = client.get("/api/v1/config/agents")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == ["collector", "analyst", "reporter"]
    assert all(item["override_keys"] == [] for item in body["items"])
    assert body["available_models"] == [
        {
            "id": "gateway-main:gpt-4o-mini",
            "provider_id": "gateway-main",
            "model": "gpt-4o-mini",
            "name": "GPT-4o mini",
            "enabled": True,
        }
    ]


def test_config_agents_list_reflects_overrides(config_api):
    client, _ = config_api
    _patch(client, agent_id="analyst", body={"temperature": 0.7})

    items = {item["id"]: item for item in client.get("/api/v1/config/agents").json()["items"]}

    assert items["analyst"]["temperature"] == 0.7
    assert items["analyst"]["override_keys"] == ["temperature"]
    assert items["collector"]["override_keys"] == []


def test_config_agents_list_survives_model_catalog_failure(config_api, monkeypatch):
    """模型清单读失败不能把配置页打挂：退化为空清单（配置读取不阻断业务）。"""

    client, _ = config_api

    def broken(**kwargs):
        raise OSError("registry down")

    monkeypatch.setattr(api_main.model_registry, "list_models", broken)

    response = client.get("/api/v1/config/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["available_models"] == []
    assert [item["id"] for item in body["items"]] == ["collector", "analyst", "reporter"]
def test_patch_writes_tool_names_and_null_restores_unlimited(config_api):
    """工具白名单的三种状态都要能写进去（ADR-034）。

    `null` = 未配置（不加限制）、`[]` = 显式取消全部、非空 = 只允许这些。
    这条用例守的是「空数组与 null 不能混淆」这个最容易出错的接缝。
    """

    client, state = config_api

    response = _patch(client, body={"tool_names": ["calculator", "web_search"]})
    assert response.status_code == 200
    assert state["calls"][-1]["tool_names"] == ["calculator", "web_search"]
    assert response.json()["tool_names"] == ["calculator", "web_search"]

    response = _patch(client, body={"tool_names": []})
    assert state["calls"][-1]["tool_names"] == []
    assert response.json()["tool_names"] == []

    response = _patch(client, body={"tool_names": None})
    assert state["calls"][-1]["tool_names"] is None
    assert response.json()["tool_names"] is None


def test_patch_rejects_blank_tool_name(config_api):
    client, _state = config_api

    response = _patch(client, body={"tool_names": ["calculator", "   "]})

    assert response.status_code == 422


# —— ADR-036：目录字段端点（）——

def _fake_registry_update(calls):
    def fake(agent_id, **kwargs):
        calls.append({"agent_id": agent_id, **kwargs})
        return {"id": agent_id}
    return fake


def test_profile_patch_maps_tri_state(config_api, monkeypatch):
    """三态与覆盖端点同构：省略 = 不动（UNSET）、显式 null = 清空（None）、值 = 写入。"""

    client, _ = config_api
    calls: list = []
    monkeypatch.setattr(api_main, "update_agent_registry", _fake_registry_update(calls))

    response = client.patch(
        "/api/v1/config/agents/collector/profile",
        json={"name": "采集员", "description": None},
    )

    assert response.status_code == 200
    assert calls == [
        {
            "agent_id": "collector",
            "name": "采集员",
            # 显式 null 在可空列上 = 清空；省略的字段保持 UNSET（不动）。
            "description": None,
            "system_prompt": UNSET,
            "icon": UNSET,
            "enabled": UNSET,
        }
    ]


def test_profile_patch_rejects_null_name_and_enabled(config_api, monkeypatch):
    """ /  没有空值状态：显式 null 返回 422，不会静默变成「不动」。"""

    client, _ = config_api
    calls: list = []
    monkeypatch.setattr(api_main, "update_agent_registry", _fake_registry_update(calls))

    for field in ("name", "enabled"):
        response = client.patch(
            "/api/v1/config/agents/collector/profile", json={field: None}
        )
        assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"icon": "Search"},
        {"icon": "has space"},
        {"name": ""},
        {"system_prompt": "x" * 8001},
    ],
)
def test_profile_patch_shape_validation(config_api, body):
    """形状错误在 schema 层挡下（422），不进业务层。"""

    client, _ = config_api
    response = client.patch("/api/v1/config/agents/collector/profile", json=body)
    assert response.status_code == 422


def test_profile_patch_unknown_agent_404(config_api):
    client, _ = config_api
    response = client.patch(
        "/api/v1/config/agents/missing/profile", json={"name": "x"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_NOT_FOUND"


CUSTOM_ENTRY = {
    "id": "summarizer",
    "name": "摘要 Agent",
    "role": "summarizer",
    "builtin": False,
    "description": None,
    "enabled": True,
    "system_prompt": "你是摘要助手。",
    "icon": "summary",
}


def test_patch_accepts_registered_custom_agent(config_api, monkeypatch):
    """覆盖端点对目录里的自定义角色同样可写（ADR-036：角色是模块，不是常量）。"""

    client, _ = config_api
    monkeypatch.setattr(api_main, "list_agent_registry", lambda: [CUSTOM_ENTRY])

    response = _patch(client, agent_id="summarizer", body={"temperature": 0.6})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "summarizer"
    assert body["builtin"] is False
    assert body["temperature"] == 0.6
    # 目录字段原样透出，不在 API 层编默认值。
    assert body["system_prompt"] == "你是摘要助手。"
    assert body["icon"] == "summary"

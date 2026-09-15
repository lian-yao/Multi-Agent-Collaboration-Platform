"""注册表接口的 HTTP 契约：`/api/v1/config/providers|models|mcp|provider-presets`。

`doc/api.md` §5.9–§5.12、ADR-017。

分层意图：核心逻辑（校验、幂等、目录推导）的证据在
`tests/unit/test_model_registry.py` / `test_mcp_registry.py`；本文件只钉住**接口边界**：
路径与方法、状态码、契约错误码、出参形状，以及「凭据永不回显」。

因此这里把 `app.core.model_registry` / `app.core.mcp_registry` 的公开函数换成替身——
每条用例只声明「这一层返回什么 / 抛什么」，不重复实现注册表语义。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import app.api.main as api_main
from app.core import mcp_registry, model_registry
from app.core.model_discovery import ModelDiscoveryError

PROVIDER_VIEW = {
    "id": "gateway-main",
    "name": "自建网关",
    "preset_type": "openai-compatible",
    "api_type": "openai-compatible",
    "base_url": "http://localhost:3000/v1",
    "api_key_configured": True,
    "custom_headers": {},
    "additional_settings": {},
    "enabled": True,
    "model_count": 2,
    "created_at": None,
    "updated_at": None,
    "updated_by": None,
}

MODEL_VIEW = {
    "id": "gateway-main:gpt-4o-mini",
    "provider_id": "gateway-main",
    "model": "gpt-4o-mini",
    "name": "GPT-4o mini",
    "enabled": True,
    "reasoning_type": "none",
    "temperature": 0.2,
    "top_p": None,
    "max_context_tokens": 128000,
    "max_output_tokens": 4096,
    "custom_parameters": [],
    "modalities": ["text", "vision"],
    "created_at": None,
    "updated_at": None,
    "updated_by": None,
}

MCP_SERVER_VIEW = {
    "id": "filesystem",
    "name": "本地文件系统",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
    "env": {},
    "cwd": None,
    "url": None,
    "headers": {},
    "enabled": True,
    "tool_options": {},
    "tool_count": 1,
    "discovered_at": "2026-09-15T08:00:00+00:00",
    "server_info": {"name": "filesystem", "version": "1.0.0"},
    "created_at": None,
    "updated_at": None,
    "updated_by": None,
}


class Stub:
    """记录调用并返回预设结果的替身。

    `outcome` 可以是：返回值、一个 `Exception` 实例（抛出）、或一个可调用对象。
    """

    def __init__(self, name: str, outcome) -> None:
        self.name = name
        self.outcome = outcome
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if callable(self.outcome):
            return self.outcome(*args, **kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    @property
    def last_call(self) -> tuple[tuple, dict]:
        assert self.calls, f"{self.name} 未被调用"
        return self.calls[-1]


class RegistryApi:
    """把注册表模块函数换成替身，并按名字取用。"""

    MODEL_FUNCTIONS = (
        "list_providers",
        "create_provider",
        "get_provider",
        "update_provider",
        "delete_provider",
        "discover_provider_models",
        "list_models",
        "create_model",
        "batch_import_models",
        "update_model",
        "delete_model",
    )
    MCP_FUNCTIONS = (
        "list_servers",
        "create_server",
        "get_server",
        "update_server",
        "delete_server",
        "discover_server",
        "compact_tool_catalog",
    )

    def __init__(self, monkeypatch) -> None:
        self.stubs: dict[str, Stub] = {}
        for name in self.MODEL_FUNCTIONS:
            self._install(monkeypatch, model_registry, name)
        for name in self.MCP_FUNCTIONS:
            self._install(monkeypatch, mcp_registry, name)
        # 预设目录是静态常量，用真实实现（顺带验证 §5.12 的出参形状）。
        monkeypatch.setattr(
            model_registry, "provider_preset_catalog", model_registry.provider_preset_catalog
        )

    def _install(self, monkeypatch, module, name: str) -> None:
        stub = Stub(name, self._default_for(name))
        monkeypatch.setattr(module, name, stub)
        self.stubs[name] = stub

    @staticmethod
    def _default_for(name: str):
        if name in {"list_providers"}:
            return {"items": [PROVIDER_VIEW], "total": 1}
        if name == "get_provider":
            return {**PROVIDER_VIEW, "models": [MODEL_VIEW]}
        if name in {"create_provider", "update_provider"}:
            return PROVIDER_VIEW
        if name == "delete_provider":
            return None
        if name == "discover_provider_models":
            return {
                "provider_id": "gateway-main",
                "source": "remote",
                "source_url": "http://localhost:3000/v1/models",
                "items": [{"id": "gpt-4o-mini", "name": "gpt-4o-mini", "owned_by": "openai"}],
                "existing": [],
                "total": 1,
            }
        if name == "list_models":
            return {"items": [MODEL_VIEW], "total": 1}
        if name in {"create_model", "update_model"}:
            return MODEL_VIEW
        if name in {"delete_model", "delete_server"}:
            return None
        if name == "batch_import_models":
            return {
                "provider_id": "gateway-main",
                "created": ["gateway-main:gpt-4o-mini"],
                "skipped": [{"model": "gpt-4o", "reason": "already_exists"}],
                "total_requested": 2,
            }
        if name == "list_servers":
            return {"items": [MCP_SERVER_VIEW], "total": 1}
        if name in {"create_server", "update_server", "get_server"}:
            return MCP_SERVER_VIEW
        if name == "discover_server":
            return {
                "server_id": "filesystem",
                "server_info": {"name": "filesystem", "version": "1.0.0"},
                "tools": [{"name": "read_file", "description": "读取文件内容"}],
                "total": 1,
                "discovered_at": "2026-09-15T08:00:00+00:00",
            }
        if name == "compact_tool_catalog":
            return {
                "items": [
                    {
                        "server_id": "filesystem",
                        "server_name": "本地文件系统",
                        "enabled": True,
                        "name": "read_file",
                        "description": "读取文件内容",
                        "tool_enabled": True,
                        "available": True,
                    }
                ],
                "total": 1,
                "servers": [
                    {
                        "id": "filesystem",
                        "name": "本地文件系统",
                        "transport": "stdio",
                        "enabled": True,
                        "tool_count": 1,
                        "discovered_at": "2026-09-15T08:00:00+00:00",
                    }
                ],
            }
        raise AssertionError(f"未预设 {name}")

    def __getitem__(self, name: str) -> Stub:
        return self.stubs[name]


@pytest.fixture
def api(monkeypatch):
    return RegistryApi(monkeypatch), TestClient(api_main.app)


# --------------------------------------------------------------------------- #
# §5.12 预设目录
# --------------------------------------------------------------------------- #


def test_provider_presets_are_static_and_complete(api):
    _, client = api

    body = client.get("/api/v1/config/provider-presets").json()

    assert {item["preset_type"] for item in body["items"]} >= {
        "openai",
        "deepseek",
        "ollama",
        "amazon-bedrock",
    }
    assert body["categories"][0] == {"id": "all", "label": "全部"}


# --------------------------------------------------------------------------- #
# §5.9 Provider 注册表
# --------------------------------------------------------------------------- #


def test_list_providers_never_echoes_api_key(api):
    """出参只有 `api_key_configured`。

    故意让核心层「泄漏」一个 `api_key` 字段：响应模型必须把它吃掉——
    这样即使将来某处忘了脱敏，HTTP 边界也不会把凭据漏出去。
    """

    registry, client = api
    registry["list_providers"].outcome = {
        "items": [{**PROVIDER_VIEW, "api_key": "qc-secret"}],
        "total": 1,
    }

    response = client.get("/api/v1/config/providers")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert "api_key" not in body["items"][0]
    assert body["items"][0]["api_key_configured"] is True
    assert "qc-secret" not in response.text


def test_list_providers_can_filter_by_enabled(api):
    registry, client = api
    registry["list_providers"].outcome = {
        "items": [
            {**PROVIDER_VIEW, "id": "on", "enabled": True},
            {**PROVIDER_VIEW, "id": "off", "enabled": False},
        ],
        "total": 2,
    }

    body = client.get("/api/v1/config/providers", params={"enabled": "true"}).json()

    assert [item["id"] for item in body["items"]] == ["on"]
    assert body["total"] == 1


def test_create_provider_returns_201_and_passes_actor(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/providers",
        json={
            "id": "gateway-main",
            "name": "自建网关",
            "preset_type": "openai-compatible",
            "base_url": "http://localhost:3000/v1",
            "api_key": "qc-secret",
        },
        headers={"X-Request-ID": "req-9"},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "gateway-main"
    _, kwargs = registry["create_provider"].last_call
    assert kwargs["provider_id"] == "gateway-main"
    assert kwargs["api_key"] == "qc-secret"
    assert kwargs["actor"] == "req-9"


def test_create_provider_with_blank_base_url_uses_preset_default(api):
    """空 base_url 表示「用预设默认端点」，不是「显式设成空」——因此传 UNSET。"""

    registry, client = api

    client.post(
        "/api/v1/config/providers",
        json={"id": "gw", "name": "网关", "base_url": "", "api_key": ""},
    )

    _, kwargs = registry["create_provider"].last_call
    from app.core.checkpoint import UNSET

    assert kwargs["base_url"] is UNSET
    assert kwargs["api_key"] is UNSET


def test_create_provider_without_api_type_lets_core_derive_it(api):
    registry, client = api

    client.post("/api/v1/config/providers", json={"id": "gw", "name": "网关"})

    _, kwargs = registry["create_provider"].last_call
    from app.core.checkpoint import UNSET

    assert kwargs["api_type"] is UNSET


def test_duplicate_provider_is_409_validation_error(api):
    registry, client = api
    registry["create_provider"].outcome = model_registry.DuplicateEntryError(
        "Provider id 已存在：gw"
    )

    response = client.post("/api/v1/config/providers", json={"id": "gw", "name": "网关"})

    assert response.status_code == 409
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_invalid_provider_payload_is_422_before_reaching_registry(api):
    registry, client = api

    response = client.post("/api/v1/config/providers", json={"id": "", "name": ""})

    assert response.status_code == 422
    assert registry["create_provider"].calls == []


def test_get_provider_detail_includes_models(api):
    _, client = api

    body = client.get("/api/v1/config/providers/gateway-main").json()

    assert body["id"] == "gateway-main"
    assert [item["id"] for item in body["models"]] == ["gateway-main:gpt-4o-mini"]


def test_get_missing_provider_is_404(api):
    registry, client = api
    registry["get_provider"].outcome = model_registry.ProviderNotFoundError("missing")

    response = client.get("/api/v1/config/providers/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "PROVIDER_NOT_FOUND"


def test_patch_provider_forwards_only_submitted_fields(api):
    from app.core.checkpoint import UNSET

    registry, client = api

    response = client.patch(
        "/api/v1/config/providers/gateway-main", json={"name": "新名字"}
    )

    assert response.status_code == 200
    _, kwargs = registry["update_provider"].last_call
    assert kwargs["name"] == "新名字"
    assert kwargs["base_url"] is UNSET  # 未提交 ≠ 清空
    assert kwargs["enabled"] is UNSET


def test_patch_provider_explicit_null_clears(api):
    registry, client = api

    client.patch("/api/v1/config/providers/gateway-main", json={"api_key": None})

    _, kwargs = registry["update_provider"].last_call
    assert kwargs["api_key"] is None


def test_patch_provider_empty_body_is_422(api):
    registry, client = api

    response = client.patch("/api/v1/config/providers/gateway-main", json={})

    assert response.status_code == 422
    assert registry["update_provider"].calls == []


def test_delete_provider_returns_204(api):
    registry, client = api

    response = client.delete("/api/v1/config/providers/gateway-main")

    assert response.status_code == 204
    assert response.content == b""
    args, kwargs = registry["delete_provider"].last_call
    assert args == ("gateway-main",)
    assert kwargs["force"] is False


def test_delete_provider_in_use_is_409_without_force(api):
    registry, client = api
    registry["delete_provider"].outcome = model_registry.ProviderInUseError("gw", 2)

    response = client.delete("/api/v1/config/providers/gw")

    assert response.status_code == 409
    assert response.json()["code"] == "PROVIDER_IN_USE"


def test_delete_provider_force_is_forwarded(api):
    registry, client = api

    response = client.delete("/api/v1/config/providers/gw", params={"force": "true"})

    assert response.status_code == 204
    assert registry["delete_provider"].last_call[1]["force"] is True


def test_provider_registry_storage_failure_is_503(api):
    registry, client = api
    registry["list_providers"].outcome = SQLAlchemyError("database down")

    response = client.get("/api/v1/config/providers")

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# §5.10 模型注册表与批量引入
# --------------------------------------------------------------------------- #


def test_list_models_forwards_filters(api):
    registry, client = api

    body = client.get(
        "/api/v1/config/models",
        params={"provider_id": "gateway-main", "enabled": "false"},
    ).json()

    assert body["total"] == 1
    _, kwargs = registry["list_models"].last_call
    assert kwargs == {"provider_id": "gateway-main", "enabled": False}


def test_create_model_returns_201(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/models",
        json={
            "provider_id": "gateway-main",
            "model": "gpt-4o-mini",
            "max_output_tokens": 4096,
            "custom_parameters": [{"key": "thinking_budget", "value": "2048", "type": "number"}],
            "modalities": ["text", "vision"],
        },
    )

    assert response.status_code == 201
    _, kwargs = registry["create_model"].last_call
    assert kwargs["model_id"] is not None  # UNSET：由核心层派生
    assert kwargs["custom_parameters"] == [
        {"key": "thinking_budget", "value": "2048", "type": "number"}
    ]
    assert kwargs["modalities"] == ["text", "vision"]


def test_create_model_without_reasoning_type_defaults_to_none(api):
    registry, client = api

    client.post(
        "/api/v1/config/models", json={"provider_id": "gw", "model": "m"}
    )

    _, kwargs = registry["create_model"].last_call
    assert kwargs["reasoning_type"] == "none"


def test_create_model_unknown_provider_is_404(api):
    registry, client = api
    registry["create_model"].outcome = model_registry.ProviderNotFoundError("missing")

    response = client.post(
        "/api/v1/config/models", json={"provider_id": "missing", "model": "m"}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "PROVIDER_NOT_FOUND"


def test_create_model_duplicate_is_409(api):
    registry, client = api
    registry["create_model"].outcome = model_registry.DuplicateEntryError(
        "该 Provider 下已存在模型：gpt-4o-mini"
    )

    response = client.post(
        "/api/v1/config/models", json={"provider_id": "gw", "model": "gpt-4o-mini"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_create_model_invalid_temperature_is_422(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/models",
        json={"provider_id": "gw", "model": "m", "temperature": 3},
    )

    assert response.status_code == 422
    assert registry["create_model"].calls == []


def test_batch_import_returns_created_and_skipped(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/models/batch",
        json={
            "provider_id": "gateway-main",
            "models": ["gpt-4o-mini", "gpt-4o"],
            "defaults": {"temperature": 0.2, "modalities": ["text"]},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == ["gateway-main:gpt-4o-mini"]
    assert body["skipped"] == [{"model": "gpt-4o", "reason": "already_exists"}]
    assert body["total_requested"] == 2
    _, kwargs = registry["batch_import_models"].last_call
    assert kwargs["models"] == ["gpt-4o-mini", "gpt-4o"]
    assert kwargs["defaults"] == {"temperature": 0.2, "modalities": ["text"]}


def test_batch_import_without_defaults_passes_unset(api):
    from app.core.checkpoint import UNSET

    registry, client = api

    client.post(
        "/api/v1/config/models/batch",
        json={"provider_id": "gw", "models": ["m"]},
    )

    _, kwargs = registry["batch_import_models"].last_call
    assert kwargs["defaults"] is UNSET


def test_batch_import_over_200_items_is_422(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/models/batch",
        json={"provider_id": "gw", "models": [f"m{i}" for i in range(201)]},
    )

    assert response.status_code == 422
    assert registry["batch_import_models"].calls == []


def test_batch_import_rejects_empty_model_list(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/models/batch", json={"provider_id": "gw", "models": []}
    )

    assert response.status_code == 422
    assert registry["batch_import_models"].calls == []


def test_patch_model_toggles_and_clears(api):
    from app.core.checkpoint import UNSET

    registry, client = api

    response = client.patch(
        "/api/v1/config/models/gateway-main:gpt-4o-mini",
        json={"enabled": False, "temperature": None},
    )

    assert response.status_code == 200
    _, kwargs = registry["update_model"].last_call
    assert kwargs["enabled"] is False
    assert kwargs["temperature"] is None  # 显式 null = 清除
    assert kwargs["top_p"] is UNSET


def test_patch_model_null_reasoning_type_falls_back_to_none(api):
    registry, client = api

    client.patch("/api/v1/config/models/m", json={"reasoning_type": None})

    _, kwargs = registry["update_model"].last_call
    assert kwargs["reasoning_type"] == "none"


def test_patch_model_cannot_change_provider(api):
    """`provider_id` 不在 PATCH 契约里：成了额外字段会被 Pydantic 忽略而非静默改绑。"""

    registry, client = api

    client.patch("/api/v1/config/models/m", json={"provider_id": "other", "name": "n"})

    _, kwargs = registry["update_model"].last_call
    assert "provider_id" not in kwargs


def test_patch_missing_model_is_404(api):
    registry, client = api
    registry["update_model"].outcome = model_registry.ModelNotFoundError("missing")

    response = client.patch("/api/v1/config/models/missing", json={"name": "n"})

    assert response.status_code == 404
    assert response.json()["code"] == "MODEL_NOT_FOUND"


def test_delete_model_returns_204(api):
    registry, client = api

    response = client.delete("/api/v1/config/models/gateway-main:gpt-4o-mini")

    assert response.status_code == 204
    assert response.content == b""
    assert registry["delete_model"].calls


def test_patch_model_with_slashed_id_reaches_registry(api):
    """模型 id 含 `/`（如 gateway-main:BAAI/bge-m3）时路由仍要命中（§5.10）。

    id 里的 `/` 被客户端编码成 %2F 后，ASGI 层会先解码回 `/`，
    普通 `{model_id}` 单段匹配直接 404——路由必须用 `{model_id:path}`。
    这是「模型开关关不掉」的回归测试。
    """

    registry, client = api

    response = client.patch(
        "/api/v1/config/models/gateway-main:BAAI/bge-m3",
        json={"enabled": False},
    )

    assert response.status_code == 200
    args, kwargs = registry["update_model"].last_call
    assert args[0] == "gateway-main:BAAI/bge-m3"
    assert kwargs["enabled"] is False


def test_delete_model_with_slashed_id_reaches_registry(api):
    registry, client = api

    response = client.delete("/api/v1/config/models/gateway-main:BAAI/bge-m3")

    assert response.status_code == 204
    args, _ = registry["delete_model"].last_call
    assert args[0] == "gateway-main:BAAI/bge-m3"


def test_model_registry_validation_error_is_422(api):
    registry, client = api
    registry["create_model"].outcome = model_registry.ModelRegistryError("modalities 只能是 text / vision / pdf")

    response = client.post(
        "/api/v1/config/models", json={"provider_id": "gw", "model": "m"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# §5.10 远端发现
# --------------------------------------------------------------------------- #


def test_discover_models_returns_remote_catalog(api):
    registry, client = api

    body = client.get(
        "/api/v1/config/providers/gateway-main/models/discover"
    ).json()

    assert body["source"] == "remote"
    assert body["total"] == 1
    assert body["items"][0]["id"] == "gpt-4o-mini"
    assert registry["discover_provider_models"].calls


def test_discover_models_failure_is_502(api):
    registry, client = api
    registry["discover_provider_models"].outcome = ModelDiscoveryError(
        "远端返回 HTTP 401（http://localhost:3000）"
    )

    response = client.get("/api/v1/config/providers/gateway-main/models/discover")

    assert response.status_code == 502
    assert response.json()["code"] == "PROVIDER_DISCOVERY_FAILED"


def test_discover_models_missing_provider_is_404(api):
    registry, client = api
    registry["discover_provider_models"].outcome = model_registry.ProviderNotFoundError(
        "missing"
    )

    response = client.get("/api/v1/config/providers/missing/models/discover")

    assert response.status_code == 404
    assert response.json()["code"] == "PROVIDER_NOT_FOUND"


def test_discover_models_uses_injected_fetcher(api, monkeypatch):
    """协议侧钩子可注入：测试不必发真实出站请求。"""

    registry, client = api
    sentinel = object()
    seen: list[object] = []
    monkeypatch.setattr(api_main, "model_discovery_fetcher", sentinel)

    def record(provider_id, *, fetcher=None):
        seen.append(fetcher)
        return {
            "provider_id": provider_id,
            "source": "remote",
            "items": [],
            "existing": [],
            "total": 0,
        }

    registry["discover_provider_models"].outcome = record

    client.get("/api/v1/config/providers/gw/models/discover")

    assert seen == [sentinel]


# --------------------------------------------------------------------------- #
# §5.11 MCP Server 注册表与工具目录
# --------------------------------------------------------------------------- #


def test_list_mcp_servers(api):
    _, client = api

    body = client.get("/api/v1/config/mcp/servers").json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == "filesystem"
    assert item["transport"] == "stdio"
    assert item["tool_count"] == 1
    assert item["server_info"] == {"name": "filesystem", "version": "1.0.0"}


def test_create_mcp_server_returns_201(api):
    registry, client = api

    response = client.post(
        "/api/v1/config/mcp/servers",
        json={
            "id": "filesystem",
            "name": "本地文件系统",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
            "tool_options": {"read_file": {"disabled": False}},
        },
        headers={"X-Request-ID": "req-1"},
    )

    assert response.status_code == 201
    _, kwargs = registry["create_server"].last_call
    assert kwargs["transport"] == "stdio"
    assert kwargs["tool_options"] == {"read_file": {"disabled": False}}
    assert kwargs["actor"] == "req-1"


def test_create_mcp_server_drops_unset_tool_option_keys(api):
    """`{disabled: null}` 是「没填」，不能落库成 `disabled=None`。"""

    registry, client = api

    client.post(
        "/api/v1/config/mcp/servers",
        json={
            "id": "s",
            "name": "s",
            "transport": "stdio",
            "command": "npx",
            "tool_options": {"read_file": {"disabled": True}},
        },
    )

    _, kwargs = registry["create_server"].last_call
    assert kwargs["tool_options"] == {"read_file": {"disabled": True}}


def test_create_mcp_server_transport_mismatch_is_422(api):
    registry, client = api
    registry["create_server"].outcome = mcp_registry.McpRegistryError(
        "transport=stdio 时必须提供 command"
    )

    response = client.post(
        "/api/v1/config/mcp/servers",
        json={"id": "s", "name": "s", "transport": "stdio"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_patch_mcp_server(api):
    from app.core.checkpoint import UNSET

    registry, client = api

    response = client.patch(
        "/api/v1/config/mcp/servers/filesystem", json={"enabled": False}
    )

    assert response.status_code == 200
    _, kwargs = registry["update_server"].last_call
    assert kwargs["enabled"] is False
    assert kwargs["url"] is UNSET


def test_delete_mcp_server_returns_204(api):
    registry, client = api

    response = client.delete("/api/v1/config/mcp/servers/filesystem")

    assert response.status_code == 204
    args, _ = registry["delete_server"].last_call
    assert args == ("filesystem",)


def test_delete_missing_mcp_server_is_404(api):
    registry, client = api
    registry["delete_server"].outcome = mcp_registry.McpServerNotFoundError("missing")

    response = client.delete("/api/v1/config/mcp/servers/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "MCP_SERVER_NOT_FOUND"


def test_compact_tools_omit_schema(api):
    _, client = api

    response = client.get("/api/v1/config/mcp/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert set(body["items"][0]) == {
        "server_id",
        "server_name",
        "enabled",
        "name",
        "description",
        "tool_enabled",
        "available",
    }
    assert "input_schema" not in response.text
    assert [server["id"] for server in body["servers"]] == ["filesystem"]


def test_discover_mcp_server_returns_tools(api):
    registry, client = api

    body = client.post("/api/v1/config/mcp/servers/filesystem/discover").json()

    assert body["server_id"] == "filesystem"
    assert body["total"] == 1
    assert body["tools"] == [{"name": "read_file", "description": "读取文件内容"}]
    assert registry["discover_server"].calls


def test_discover_mcp_server_failure_is_502(api):
    registry, client = api
    registry["discover_server"].outcome = mcp_registry.McpDiscoveryError(
        "连接 MCP Server 失败：ConnectionRefusedError"
    )

    response = client.post("/api/v1/config/mcp/servers/filesystem/discover")

    assert response.status_code == 502
    assert response.json()["code"] == "MCP_DISCOVERY_FAILED"


def test_discover_mcp_server_missing_is_404(api):
    registry, client = api
    registry["discover_server"].outcome = mcp_registry.McpServerNotFoundError("missing")

    response = client.post("/api/v1/config/mcp/servers/missing/discover")

    assert response.status_code == 404
    assert response.json()["code"] == "MCP_SERVER_NOT_FOUND"


def test_discover_mcp_server_uses_injected_discoverer(api, monkeypatch):
    registry, client = api
    sentinel = object()
    seen: list[object] = []
    monkeypatch.setattr(api_main, "mcp_server_discoverer", sentinel)

    def record(server_id, *, discoverer=None):
        seen.append(discoverer)
        return {
            "server_id": server_id,
            "server_info": {},
            "tools": [],
            "total": 0,
            "discovered_at": None,
        }

    registry["discover_server"].outcome = record

    client.post("/api/v1/config/mcp/servers/filesystem/discover")

    assert seen == [sentinel]


def test_mcp_registry_storage_failure_is_503(api):
    registry, client = api
    registry["list_servers"].outcome = SQLAlchemyError("database down")

    response = client.get("/api/v1/config/mcp/servers")

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"

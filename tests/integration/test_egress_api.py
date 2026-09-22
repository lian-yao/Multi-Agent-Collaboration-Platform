"""出网策略的只读投影接口：`GET /api/v1/config/egress`（`doc/api.md` §5.21）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.security import egress as egress_module
from app.security.config import EgressSettings


def _client(monkeypatch, **settings) -> TestClient:
    resolved = {
        "mode": "public_only",
        "allow_hosts": "*.example.com,api.example.org",
        "deny_hosts": "blocked.example.com",
        "internal_hosts": "search-gateway,redis",
        "allowed_ports": "443,8800",
        "model_exempt": True,
        "max_redirects": 3,
    }
    resolved.update(settings)
    policy = egress_module.EgressPolicy(EgressSettings(_env_file=None, **resolved))
    policy.blocked["private_ip"] = 2
    monkeypatch.setattr(egress_module, "get_egress_policy", lambda: policy)
    return TestClient(api_main.app)


def test_egress_status_exposes_the_current_policy(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/api/v1/config/egress")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "public_only"
    assert body["allow_hosts"] == ["*.example.com", "api.example.org"]
    assert body["deny_hosts"] == ["blocked.example.com"]
    assert body["internal_hosts"] == ["redis", "search-gateway"]
    assert body["allowed_ports"] == [443, 8800]
    assert body["model_exempt"] is True
    assert body["max_redirects"] == 3
    assert body["blocked"] == {"private_ip": 2}


def test_invalid_configuration_is_reported_as_503(monkeypatch):
    """策略构造失败（配置写错）要在接口上看得见，而不是安静地用默认值跑。"""

    def broken():
        raise egress_module.EgressConfigError("非法的端口：'https'")

    monkeypatch.setattr(egress_module, "get_egress_policy", broken)
    client = TestClient(api_main.app)

    response = client.get("/api/v1/config/egress")

    assert response.status_code == 503
    assert response.json()["code"] == "VALIDATION_ERROR"

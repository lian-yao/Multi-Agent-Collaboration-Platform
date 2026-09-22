"""工作区接口的 HTTP 契约：`/api/v1/workspaces`（`doc/api.md` §5.19、ADR-033）。

分层意图与 `test_registry_api.py` 一致：路径与目录树语义的证据在
`tests/unit/test_workspace_service.py` / `test_workspace_paths.py`，这里只钉住
**接口边界**——路径与方法、状态码、契约错误码、出参形状。

因此把 `app.workspace.service` 的公开函数换成替身：每条用例只声明「这一层返回什么 /
抛什么」，不重复实现业务语义。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.workspace import service as workspace_service
from app.workspace import approvals as workspace_approvals
from app.workspace.approvals import ApprovalNotFoundError, ApprovalNotPendingError
from app.workspace.errors import (
    WorkspaceApprovalRequired,
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceRootUnavailable,
)

WORKSPACE_VIEW = {
    "id": "w-1",
    "session_id": "3f2b8f4e-1b6d-4c3a-9c6f-2c1b7f7a1a11",
    "path": "sessions/3f2b8f4e",
    "mode": "read_only",
    "name": None,
    "quota": {
        "max_file_bytes": 5242880,
        "max_total_bytes": 268435456,
        "max_entries": 2000,
    },
    "usage": {"available": True, "total_bytes": 12, "entries": 2, "truncated": False},
    "created_by": None,
    "created_at": datetime(2026, 9, 23, tzinfo=timezone.utc),
}

TREE_VIEW = {
    "workspace_id": "w-1",
    "path": "",
    "depth": 1,
    "entries": [
        {
            "name": "reports",
            "path": "reports",
            "kind": "dir",
            "outside": False,
            "size_bytes": None,
            "modified_at": None,
        },
        {
            "name": "escape",
            "path": "escape",
            "kind": "symlink",
            "outside": True,
            "size_bytes": None,
            "modified_at": None,
        },
    ],
    "truncated": False,
    "limit": 500,
}

SESSION_ID = "3f2b8f4e-1b6d-4c3a-9c6f-2c1b7f7a1a11"

APPROVAL_VIEW = {
    "id": "ap-1",
    "workspace_id": "w-1",
    "session_id": SESSION_ID,
    "run_id": None,
    "kind": "delete",
    "target": "reports/old.md",
    "reason": "删除工作区内的条目",
    "status": "pending",
    "payload": {"kind": "file"},
    "decided_by": None,
    "requested_at": datetime(2026, 9, 23, tzinfo=timezone.utc),
    "decided_at": None,
}


class Stub:
    """按名字记录调用并返回预设结果（与 `test_registry_api.py` 同形）。"""

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


class WorkspaceApi:
    """把 `app.workspace.service` 的函数换成替身，并按名字取用。"""

    FUNCTIONS = (
        "list_workspaces",
        "create_workspace",
        "update_workspace",
        "get_workspace",
        "workspace_tree",
        "delete_workspace",
    )

    def __init__(self, monkeypatch) -> None:
        self.stubs: dict[str, Stub] = {}
        for name in self.FUNCTIONS:
            stub = Stub(name, self._default_for(name))
            monkeypatch.setattr(workspace_service, name, stub)
            self.stubs[name] = stub
        # 会话存在性：默认全部存在，需要 404 的用例单独覆盖。
        monkeypatch.setattr(
            api_main.api_store,
            "get_session",
            lambda session_id: {"id": str(session_id)},
        )

    @staticmethod
    def _default_for(name: str):
        if name == "list_workspaces":
            return {"items": [WORKSPACE_VIEW], "total": 1}
        if name == "create_workspace":
            return WORKSPACE_VIEW
        if name == "update_workspace":
            return {**WORKSPACE_VIEW, "mode": "workspace_write", "updated_by": "ui"}
        if name == "get_workspace":
            return WORKSPACE_VIEW
        if name == "workspace_tree":
            return TREE_VIEW
        if name == "delete_workspace":
            return None
        raise AssertionError(f"未预设 {name}")

    def stub(self, name: str, outcome) -> Stub:
        stub = self.stubs[name]
        stub.outcome = outcome
        return stub

    def __getitem__(self, name: str) -> Stub:
        return self.stubs[name]


class ApprovalApi:
    """把 `app.workspace.approvals` 的两个入口换成替身。"""

    def __init__(self, monkeypatch) -> None:
        self.list_stub = Stub(
            "list_session_approvals",
            {"items": [APPROVAL_VIEW], "total": 1, "pending": 1},
        )
        self.decide_stub = Stub(
            "decide", {**APPROVAL_VIEW, "status": "approved", "decided_by": "req-1"}
        )
        monkeypatch.setattr(
            workspace_approvals, "list_session_approvals", self.list_stub
        )
        monkeypatch.setattr(workspace_approvals, "decide", self.decide_stub)


@pytest.fixture
def api(monkeypatch):
    return WorkspaceApi(monkeypatch), TestClient(api_main.app)


@pytest.fixture
def approval_api(monkeypatch):
    # 会话存在性：审批接口同样先做 404 校验。
    monkeypatch.setattr(
        api_main.api_store, "get_session", lambda session_id: {"id": str(session_id)}
    )
    return ApprovalApi(monkeypatch), TestClient(api_main.app)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def test_list_workspaces_returns_the_contract(api):
    stub, client = api

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["path"] == "sessions/3f2b8f4e"
    assert body["items"][0]["mode"] == "read_only"
    assert body["items"][0]["quota"]["max_entries"] == 2000
    assert stub["list_workspaces"].last_call[1] == {"session_id": None}


def test_list_workspaces_filters_by_session(api):
    stub, client = api

    response = client.get(f"/api/v1/workspaces?session_id={SESSION_ID}")

    assert response.status_code == 200
    assert stub["list_workspaces"].last_call[1] == {"session_id": SESSION_ID}


def test_list_workspaces_with_unknown_session_is_404(api, monkeypatch):
    _, client = api
    monkeypatch.setattr(api_main.api_store, "get_session", lambda session_id: None)

    response = client.get(f"/api/v1/workspaces?session_id={SESSION_ID}")

    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_get_workspace_returns_usage(api):
    _, client = api

    response = client.get("/api/v1/workspaces/w-1")

    assert response.status_code == 200
    assert response.json()["usage"]["entries"] == 2


def test_get_missing_workspace_is_404(api):
    stub, client = api
    stub.stub("get_workspace", WorkspaceNotFoundError("w-x"))

    response = client.get("/api/v1/workspaces/w-x")

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


def test_tree_returns_entries_and_marks_outside_symlinks(api):
    stub, client = api

    response = client.get("/api/v1/workspaces/w-1/tree?path=&depth=2")

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["kind"] for entry in entries] == ["dir", "symlink"]
    assert entries[1]["outside"] is True
    assert stub["workspace_tree"].last_call[0][0] == "w-1"
    assert stub["workspace_tree"].last_call[1] == {"path": "", "depth": 2}


def test_tree_rejects_a_depth_out_of_range(api):
    _, client = api

    response = client.get("/api/v1/workspaces/w-1/tree?depth=99")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


def test_create_workspace_returns_201(api):
    stub, client = api

    response = client.post(
        "/api/v1/workspaces",
        json={"session_id": SESSION_ID, "path": "project", "name": "项目"},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "w-1"
    kwargs = stub["create_workspace"].last_call[1]
    assert kwargs["session_id"] == SESSION_ID
    assert kwargs["path"] == "project"
    assert kwargs["mode"] == "read_only"


def test_create_workspace_with_unknown_session_is_404(api, monkeypatch):
    _, client = api
    monkeypatch.setattr(api_main.api_store, "get_session", lambda session_id: None)

    response = client.post("/api/v1/workspaces", json={"session_id": SESSION_ID})

    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_escaping_path_is_422(api):
    stub, client = api
    stub.stub("create_workspace", WorkspacePathError("路径越出工作区：../etc"))

    response = client.post("/api/v1/workspaces", json={"path": "../etc"})

    assert response.status_code == 422
    assert response.json()["code"] == "WORKSPACE_PATH_REJECTED"


def test_create_workspace_with_write_mode_is_201(api):
    """阶段 2 起写档位可用；创建时直接提档是合法的。"""

    stub, client = api
    stub.stub("create_workspace", {**WORKSPACE_VIEW, "mode": "workspace_write"})

    response = client.post("/api/v1/workspaces", json={"mode": "workspace_write"})

    assert response.status_code == 201
    assert response.json()["mode"] == "workspace_write"
    assert stub["create_workspace"].last_call[1]["mode"] == "workspace_write"


def test_patch_workspace_lifts_the_tier_and_records_the_actor(api):
    stub, client = api

    response = client.patch(
        "/api/v1/workspaces/w-1",
        json={"mode": "workspace_write"},
        headers={"X-Request-ID": "req-7"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_write"
    assert body["updated_by"] == "ui"
    kwargs = stub["update_workspace"].last_call[1]
    assert kwargs["mode"] == "workspace_write"
    assert kwargs["actor"] == "req-7"


def test_patch_workspace_with_an_unknown_mode_is_framework_422(api):
    """档位由 Pydantic 的 Literal 收口：非法值连服务层都到不了。"""

    stub, client = api

    response = client.patch("/api/v1/workspaces/w-1", json={"mode": "admin"})

    assert response.status_code == 422
    assert not stub["update_workspace"].calls


def test_patch_missing_workspace_is_404(api):
    stub, client = api
    stub.stub("update_workspace", WorkspaceNotFoundError("w-x"))

    response = client.patch("/api/v1/workspaces/w-x", json={"mode": "read_only"})

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


def test_duplicate_path_is_409(api):
    stub, client = api
    stub.stub("create_workspace", WorkspaceExistsError("该路径已登记：project"))

    response = client.post("/api/v1/workspaces", json={"path": "project"})

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_EXISTS"


def test_unavailable_root_is_503(api):
    stub, client = api
    stub.stub("create_workspace", WorkspaceRootUnavailable("工作区根不可用：/workspace"))

    response = client.post("/api/v1/workspaces", json={"path": "project"})

    assert response.status_code == 503
    assert response.json()["code"] == "WORKSPACE_ROOT_UNAVAILABLE"


def test_disabled_workspace_is_503(api):
    stub, client = api
    stub.stub("list_workspaces", WorkspaceDisabled("工作区功能已关闭"))

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 503
    assert response.json()["code"] == "WORKSPACE_DISABLED"


def test_quota_exceeded_is_409(api):
    stub, client = api
    stub.stub(
        "create_workspace",
        WorkspaceQuotaExceeded("超过目录总字节配额：当前 0 + 本次 11 > 上限 10 字节"),
    )

    response = client.post("/api/v1/workspaces", json={"path": "project"})

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_QUOTA_EXCEEDED"


def test_approval_required_is_409(api):
    stub, client = api
    stub.stub(
        "create_workspace",
        WorkspaceApprovalRequired("覆盖已有文件需要人工审批（阶段 3 提供）：a.txt"),
    )

    response = client.post("/api/v1/workspaces", json={"path": "project"})

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_APPROVAL_REQUIRED"


def test_unknown_workspace_error_is_422(api):
    stub, client = api
    stub.stub("create_workspace", WorkspaceError("mode 取值无效：admin"))

    response = client.post("/api/v1/workspaces", json={"mode": "read_only"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_delete_workspace_is_204(api):
    stub, client = api

    response = client.delete("/api/v1/workspaces/w-1")

    assert response.status_code == 204
    assert stub["delete_workspace"].last_call[0][0] == "w-1"


def test_delete_missing_workspace_is_404(api):
    stub, client = api
    stub.stub("delete_workspace", WorkspaceNotFoundError("w-x"))

    response = client.delete("/api/v1/workspaces/w-x")

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


# --------------------------------------------------------------------------- #
# §5.20 审批
# --------------------------------------------------------------------------- #


def test_list_approvals_returns_pending_count(approval_api):
    stub, client = approval_api

    response = client.get(f"/api/v1/sessions/{SESSION_ID}/approvals")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["pending"] == 1
    assert body["items"][0]["kind"] == "delete"
    assert stub.list_stub.last_call[1] == {"status": None}


def test_list_approvals_passes_the_status_filter(approval_api):
    stub, client = approval_api

    response = client.get(f"/api/v1/sessions/{SESSION_ID}/approvals?status=pending")

    assert response.status_code == 200
    assert stub.list_stub.last_call[1] == {"status": "pending"}


def test_list_approvals_with_unknown_session_is_404(approval_api, monkeypatch):
    _, client = approval_api
    monkeypatch.setattr(api_main.api_store, "get_session", lambda session_id: None)

    response = client.get(f"/api/v1/sessions/{SESSION_ID}/approvals")

    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_decide_approval_returns_the_decided_record(approval_api):
    stub, client = approval_api

    response = client.post(
        "/api/v1/approvals/ap-1/decision",
        json={"decision": "approved"},
        headers={"X-Request-ID": "req-1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert stub.decide_stub.last_call[1] == {"decision": "approved", "actor": "req-1"}


def test_decide_approval_rejects_an_unknown_decision(approval_api):
    stub, client = approval_api

    response = client.post("/api/v1/approvals/ap-1/decision", json={"decision": "maybe"})

    assert response.status_code == 422
    assert not stub.decide_stub.calls


def test_decide_missing_approval_is_404(approval_api):
    stub, client = approval_api
    stub.decide_stub.outcome = ApprovalNotFoundError("ap-x")

    response = client.post("/api/v1/approvals/ap-x/decision", json={"decision": "denied"})

    assert response.status_code == 404
    assert response.json()["code"] == "APPROVAL_NOT_FOUND"


def test_deciding_twice_is_409(approval_api):
    stub, client = approval_api
    stub.decide_stub.outcome = ApprovalNotPendingError("审批 ap-1 已经是 approved")

    response = client.post("/api/v1/approvals/ap-1/decision", json={"decision": "denied"})

    assert response.status_code == 409
    assert response.json()["code"] == "APPROVAL_NOT_PENDING"

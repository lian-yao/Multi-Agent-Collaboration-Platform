"""导入文件的端到端（进程内）：**真实 HTTP 请求 → 真实服务层 → 真实磁盘写入**。

`tests/integration/test_workspace_api.py` 把服务层换成了替身，钉的是接口契约；
`tests/unit/test_workspace_service.py` 直接调服务层，钉的是业务语义。
这一条补中间那段：走 FastAPI 的请求解析与错误映射，落到**真实文件系统**上，
证明「浏览器传上来的路径 + 内容」确实变成工作区里的文件——这正是「选择文件夹」按钮的
服务端一侧。存储用替身（不连 PostgreSQL），根用 `tmp_path`（不碰真实磁盘别处）。
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.core import checkpoint
from app.workspace import service as workspace_service
from app.workspace.config import WorkspaceSettings


class _Store:
    """只实现工作区读写的最小替身（导入不需要审批）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def install(self, monkeypatch) -> "_Store":
        for name in (
            "list_workspaces",
            "get_workspace",
            "find_workspace_by_path",
            "create_workspace",
            "delete_workspace",
        ):
            monkeypatch.setattr(checkpoint, name, getattr(self, name))
        return self

    def list_workspaces(self, *, session_id=None) -> list[dict]:
        rows = list(self.rows.values())
        if session_id is not None:
            rows = [row for row in rows if row.get("session_id") == str(session_id)]
        return [dict(row) for row in rows]

    def get_workspace(self, workspace_id) -> dict | None:
        row = self.rows.get(str(workspace_id))
        return dict(row) if row else None

    def find_workspace_by_path(self, path: str) -> dict | None:
        for row in self.rows.values():
            if row["path"] == path:
                return dict(row)
        return None

    def create_workspace(self, *, workspace_id, path, mode, **fields) -> dict:
        now = datetime.now(timezone.utc)
        row = {
            "id": str(workspace_id),
            "session_id": fields.get("session_id"),
            "path": path,
            "mode": mode,
            "name": fields.get("name"),
            "quota": dict(fields.get("quota") or {}),
            "created_by": fields.get("created_by"),
            "updated_by": None,
            "created_at": now,
            "updated_at": now,
        }
        self.rows[row["id"]] = row
        return dict(row)

    def delete_workspace(self, workspace_id) -> bool:
        return self.rows.pop(str(workspace_id), None) is not None


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch) -> Path:
    _Store().install(monkeypatch)
    settings = WorkspaceSettings(_env_file=None, root=str(tmp_path))
    # 服务层读的是模块级函数；换掉它就不必依赖环境变量与 lru_cache 的清理顺序。
    monkeypatch.setattr(
        workspace_service, "get_workspace_settings", lambda: settings
    )
    return tmp_path


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_import_endpoint_writes_real_files_under_the_workspace(workspace_root: Path):
    client = TestClient(api_main.app)
    created = client.post(
        "/api/v1/workspaces", json={"path": "project", "mode": "read_only"}
    )
    assert created.status_code == 201
    workspace_id = created.json()["id"]

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/files",
        json={
            "files": [
                {"path": "docs/a.md", "content_base64": _b64("# 从浏览器选上来的")},
                {"path": "data/blob.bin", "content_base64": base64.b64encode(b"\x00\x01").decode("ascii")},
            ]
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert (payload["imported"], payload["skipped"], payload["failed"]) == (2, 0, 0)
    assert (workspace_root / "project" / "docs" / "a.md").read_text(
        encoding="utf-8"
    ) == "# 从浏览器选上来的"
    assert (workspace_root / "project" / "data" / "blob.bin").read_bytes() == b"\x00\x01"
    # 目录树随即能看到它们（界面导入后就是这样刷新出来的）
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?depth=2").json()
    names = [entry["name"] for entry in tree["entries"]]
    assert names == ["data", "docs"]


def test_import_endpoint_rejects_escaping_paths_without_writing(workspace_root: Path):
    client = TestClient(api_main.app)
    workspace_id = client.post(
        "/api/v1/workspaces", json={"path": "project"}
    ).json()["id"]

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/files",
        json={"files": [{"path": "../escape.md", "content_base64": _b64("x")}]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "WORKSPACE_PATH_REJECTED"
    assert not (workspace_root / "escape.md").exists()


def test_import_endpoint_reports_quota_as_409(workspace_root: Path, monkeypatch):
    settings = WorkspaceSettings(_env_file=None, root=str(workspace_root), max_total_bytes=4)
    monkeypatch.setattr(workspace_service, "get_workspace_settings", lambda: settings)
    client = TestClient(api_main.app)
    workspace_id = client.post(
        "/api/v1/workspaces", json={"path": "project"}
    ).json()["id"]

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/files",
        json={"files": [{"path": "a.md", "content_base64": _b64("12345")}]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_QUOTA_EXCEEDED"
    assert list((workspace_root / "project").iterdir()) == []

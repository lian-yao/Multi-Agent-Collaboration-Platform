"""会话协作工作流列表契约（`doc/api.md` §5.18）。

这个列表存在的唯一理由是「对话编号」：用户要说「对话 3 那次是怎么跑的」，
编号就必须**跟着对话本身固定**。所以排序是语义要求而非偏好——一旦倒序或按更新时间排，
旧对话的编号会随着新对话往后挪，编号就失去了指代能力。
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.api.main as api_main
from app.api.store import InMemoryApiStore


@pytest.fixture
def client(monkeypatch):
    store = InMemoryApiStore()
    monkeypatch.setattr(api_main, "api_store", store)
    return TestClient(api_main.app), store


def _add_workflow(store, session_id, workflow_id, *, minutes, status="completed"):
    """按指定时间落一个 Workflow；`created_at` 显式给值，避免相邻创建撞同一微秒。"""

    store.create_workflow(workflow_id, session_id=session_id, agent_run_id=f"r-{workflow_id}")
    created = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    row = store.workflows[workflow_id]
    row["created_at"] = created
    row["updated_at"] = created
    row["status"] = status
    return row


def test_unknown_session_is_404(client):
    http, _ = client
    response = http.get("/api/v1/sessions/nope/workflows")
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_empty_session_returns_empty_list(client):
    http, store = client
    session = store.create_session("u1")

    body = http.get(f"/api/v1/sessions/{session['id']}/workflows").json()
    assert body["items"] == []
    assert body["total"] == 0


def test_workflows_are_listed_in_creation_order(client):
    """乱序落库也要按创建时间升序回来——这正是「对话编号」的依据。"""

    http, store = client
    session = store.create_session("u1")
    session_id = session["id"]
    _add_workflow(store, session_id, "w-third", minutes=20)
    _add_workflow(store, session_id, "w-first", minutes=0)
    _add_workflow(store, session_id, "w-second", minutes=10)

    body = http.get(f"/api/v1/sessions/{session_id}/workflows").json()

    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == ["w-first", "w-second", "w-third"]


def test_list_is_scoped_to_its_own_session(client):
    http, store = client
    mine = store.create_session("u1")["id"]
    other = store.create_session("u2")["id"]
    _add_workflow(store, mine, "w-mine", minutes=0)
    _add_workflow(store, other, "w-other", minutes=1)

    body = http.get(f"/api/v1/sessions/{mine}/workflows").json()

    assert [item["id"] for item in body["items"]] == ["w-mine"]


def test_running_workflow_exposes_step_and_checkpoint(client):
    """画布要画进度，所以未完成的 Workflow 必须带上 `current_step` 与 checkpoint。"""

    http, store = client
    session_id = store.create_session("u1")["id"]
    row = _add_workflow(store, session_id, "w-live", minutes=0, status="running")
    row["current_step"] = "analyze"
    row["checkpoint"] = {
        "status": "running",
        "current_step": "analyze",
        "completed_steps": ["collect"],
    }

    item = http.get(f"/api/v1/sessions/{session_id}/workflows").json()["items"][0]

    assert item["status"] == "running"
    assert item["current_step"] == "analyze"
    assert item["checkpoint"]["completed_steps"] == ["collect"]
    assert item["agent_run_id"] == "r-w-live"

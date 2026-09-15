from fastapi.testclient import TestClient

import app.api.main as api_main
from app.api.store import InMemoryApiStore
from app.api.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class FakeWorkflowService:
    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.scheduled_tasks: list[object] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []

    def schedule(self, task) -> str:
        self.scheduled.append(task.workflow_id)
        self.scheduled_tasks.append(task)
        return task.workflow_id

    def pause(self, workflow_id: str) -> None:
        self.paused.append(workflow_id)

    def resume(self, workflow_id: str) -> None:
        self.resumed.append(workflow_id)


def test_session_message_and_workflow_lifecycle(monkeypatch) -> None:
    store = InMemoryApiStore()
    workflow_service = FakeWorkflowService()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: workflow_service)
    client = TestClient(app)

    created = client.post("/api/v1/sessions", json={"user_id": "demo-user"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    accepted = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "执行三步流水线"},
    )
    assert accepted.status_code == 202
    accepted_payload = accepted.json()
    assert accepted_payload["status"] == "pending"
    workflow_id = accepted_payload["workflow_id"]
    assert workflow_service.scheduled == [workflow_id]
    assert workflow_service.scheduled_tasks[0].message_id == accepted_payload["message_id"]

    workflow = client.get(f"/api/v1/workflows/{workflow_id}")
    assert workflow.status_code == 200
    assert workflow.json()["status"] == "running"

    messages = client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert messages.json()["total"] == 1
    assert messages.json()["items"][0]["content"] == "执行三步流水线"
    assert messages.json()["items"][0]["status"] == "running"

    paused = client.post(f"/api/v1/sessions/{session_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["session"]["status"] == "paused"
    assert paused.json()["workflow"]["status"] == "paused"
    assert workflow_service.paused == [workflow_id]

    rejected = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "不能提交"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "SESSION_PAUSED"

    resumed = client.post(f"/api/v1/sessions/{session_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["session"]["status"] == "active"
    assert resumed.json()["workflow"]["status"] == "running"
    assert workflow_service.resumed == [workflow_id]


def test_missing_session_uses_contract_error(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "api_store", InMemoryApiStore())
    client = TestClient(app)

    response = client.get("/api/v1/sessions/not-found")

    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_list_sessions_returns_summaries(monkeypatch) -> None:
    """`GET /api/v1/sessions` 返回带摘要的历史会话分页列表（`doc/api.md` §5.13）。"""
    store = InMemoryApiStore()
    workflow_service = FakeWorkflowService()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: workflow_service)
    client = TestClient(app)

    # 建两个会话：一个发消息（产生 workflow），一个只创建不发消息（无 workflow）。
    first = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    first_id = first["id"]
    client.post(
        f"/api/v1/sessions/{first_id}/messages",
        json={"content": "帮我分析这份数据"},
    )

    second = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    second_id = second["id"]

    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    payload = response.json()

    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert len(payload["items"]) == 2

    # 按 updated_at 倒序：后建的 second 在前。
    titles = {item["id"]: item["title"] for item in payload["items"]}
    assert titles[first_id] == "帮我分析这份数据"
    assert titles[second_id] == "（暂无消息）"

    # 有 workflow 的会话带 latest_workflow_status 与 latest_workflow_id。
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[first_id]["latest_workflow_status"] == "running"
    assert by_id[first_id]["latest_workflow_id"] is not None
    assert by_id[second_id]["latest_workflow_status"] is None
    assert by_id[second_id]["latest_workflow_id"] is None


def test_list_sessions_pagination(monkeypatch) -> None:
    store = InMemoryApiStore()
    monkeypatch.setattr(api_main, "api_store", store)
    client = TestClient(app)

    for _ in range(3):
        client.post("/api/v1/sessions", json={"user_id": "demo-user"})

    page_one = client.get("/api/v1/sessions?page=1&page_size=2")
    assert page_one.status_code == 200
    assert page_one.json()["total"] == 3
    assert len(page_one.json()["items"]) == 2

    page_two = client.get("/api/v1/sessions?page=2&page_size=2")
    assert page_two.status_code == 200
    assert len(page_two.json()["items"]) == 1


def test_delete_session_removes_session_and_children(monkeypatch) -> None:
    """`DELETE /api/v1/sessions/{id}` 删除会话及其消息/运行记录（`doc/api.md` §5.14）。"""
    store = InMemoryApiStore()
    workflow_service = FakeWorkflowService()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: workflow_service)
    client = TestClient(app)

    first = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    first_id = first["id"]
    client.post(f"/api/v1/sessions/{first_id}/messages", json={"content": "要删掉我"})

    # 删除前：列表 1 条、有消息。
    assert client.get("/api/v1/sessions").json()["total"] == 1
    assert len(client.get(f"/api/v1/sessions/{first_id}/messages").json()["items"]) == 1

    # 删除成功返回 204，无正文。
    deleted = client.delete(f"/api/v1/sessions/{first_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    # 删除后：列表为空，取会话 404，取消息 404。
    assert client.get("/api/v1/sessions").json()["total"] == 0
    assert client.get(f"/api/v1/sessions/{first_id}").status_code == 404
    assert client.get(f"/api/v1/sessions/{first_id}/messages").status_code == 404


def test_delete_missing_session_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "api_store", InMemoryApiStore())
    client = TestClient(app)

    response = client.delete("/api/v1/sessions/not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_agent_team_matches_d5_d6_pipeline(monkeypatch) -> None:
    # 覆盖表隔离：本用例只断言团队结构，不依赖数据库中的覆盖行。
    monkeypatch.setattr(api_main, "agent_config_reader", dict)
    client = TestClient(app)

    response = client.get("/api/v1/agents")

    assert response.status_code == 200
    assert [item["role"] for item in response.json()["items"]] == [
        "collector",
        "analyst",
        "reporter",
    ]

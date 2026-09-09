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


def test_agent_team_matches_d5_d6_pipeline() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/agents")

    assert response.status_code == 200
    assert [item["role"] for item in response.json()["items"]] == [
        "collector",
        "analyst",
        "reporter",
    ]

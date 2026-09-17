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

    # 两个都发过消息的会话：摘要 =「首条用户消息 + 最新 workflow 终态」。
    first = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    first_id = first["id"]
    client.post(
        f"/api/v1/sessions/{first_id}/messages",
        json={"content": "帮我分析这份数据"},
    )

    second = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    second_id = second["id"]
    client.post(
        f"/api/v1/sessions/{second_id}/messages",
        json={"content": "再帮我画一张趋势图"},
    )

    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    payload = response.json()

    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert len(payload["items"]) == 2

    # 按 updated_at 倒序：后建的 second 在前。
    assert [item["id"] for item in payload["items"]] == [second_id, first_id]

    titles = {item["id"]: item["title"] for item in payload["items"]}
    assert titles[first_id] == "帮我分析这份数据"
    assert titles[second_id] == "再帮我画一张趋势图"

    # 有 workflow 的会话带 latest_workflow_status 与 latest_workflow_id。
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[first_id]["latest_workflow_status"] == "running"
    assert by_id[first_id]["latest_workflow_id"] is not None


def test_list_sessions_hides_sessions_without_messages(monkeypatch) -> None:
    """没有消息的会话不出现在历史列表里，`total` 同条件过滤（`doc/api.md` §5.13）。

    空会话只可能来自绕过前端直接 `POST /sessions`（前端草稿态不落库）；接口层要兜住它，
    否则列表会被无意义的空行堆满、`total` 与分页也会跟着错位。
    """
    store = InMemoryApiStore()
    monkeypatch.setattr(api_main, "api_store", store)
    client = TestClient(app)

    empty = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    assert client.get("/api/v1/sessions").json()["total"] == 0

    real = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
    client.post(
        f"/api/v1/sessions/{real['id']}/messages",
        json={"content": "有内容才该被记录"},
    )

    payload = client.get("/api/v1/sessions").json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [real["id"]]
    assert empty["id"] not in {item["id"] for item in payload["items"]}

    # 被过滤只是不出现在列表里，`GET /sessions/{id}` 仍按 §4.2 返回它（删除入口要用）。
    assert client.get(f"/api/v1/sessions/{empty['id']}").status_code == 200


def test_list_sessions_pagination(monkeypatch) -> None:
    store = InMemoryApiStore()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: FakeWorkflowService())
    client = TestClient(app)

    # 每个会话都得先发一条消息：空会话会被 §5.13 的服务端过滤挡住，不进列表。
    for index in range(3):
        created = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()
        client.post(
            f"/api/v1/sessions/{created['id']}/messages",
            json={"content": f"第 {index + 1} 条消息"},
        )

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


def test_create_agent_registry_entry(monkeypatch) -> None:
    """`POST /api/v1/config/agents` 登记自定义角色；内置 id 返回 409。"""

    registry: dict[str, dict] = {}

    def fake_list() -> list[dict]:
        return list(registry.values())

    def fake_create(**kwargs) -> dict:
        entry = {
            "id": kwargs["agent_id"],
            "name": kwargs["name"],
            "role": kwargs["role"],
            "description": kwargs.get("description"),
            "system_prompt": kwargs.get("system_prompt"),
            "builtin": False,
            "enabled": kwargs.get("enabled", True),
            "created_at": None,
            "updated_at": None,
        }
        registry[kwargs["agent_id"]] = entry
        return entry

    monkeypatch.setattr(api_main, "agent_config_reader", dict)
    monkeypatch.setattr(api_main, "list_agent_registry", fake_list)
    monkeypatch.setattr(api_main, "create_agent_registry", fake_create)
    client = TestClient(app)

    ok = client.post(
        "/api/v1/config/agents",
        json={"id": "summarizer", "name": "摘要 Agent", "role": "summarizer"},
    )
    assert ok.status_code == 201
    assert ok.json()["id"] == "summarizer"
    assert ok.json()["builtin"] is False
    assert ok.json()["name"] == "摘要 Agent"

    conflict = client.post(
        "/api/v1/config/agents",
        json={"id": "collector", "name": "x", "role": "x"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "VALIDATION_ERROR"


def test_delete_agent_registry_entry(monkeypatch) -> None:
    """`DELETE /api/v1/config/agents/{id}`：内置角色 409，不存在 404，自定义删除 204。"""

    monkeypatch.setattr(api_main, "agent_config_reader", dict)
    monkeypatch.setattr(api_main, "list_agent_registry", lambda: [])

    def fake_delete(agent_id: str) -> bool:
        return agent_id == "summarizer"

    monkeypatch.setattr(api_main, "delete_agent_registry", fake_delete)
    client = TestClient(app)

    builtin = client.delete("/api/v1/config/agents/collector")
    assert builtin.status_code == 409
    assert builtin.json()["code"] == "AGENT_BUILTIN"

    missing = client.delete("/api/v1/config/agents/ghost")
    assert missing.status_code == 404
    assert missing.json()["code"] == "AGENT_NOT_FOUND"

    removed = client.delete("/api/v1/config/agents/summarizer")
    assert removed.status_code == 204


def test_send_message_passes_orchestration_mode(monkeypatch) -> None:
    """单次执行的编排模式覆盖必须原样送到调度器（ADR-019、`doc/api.md` §4.4）。"""

    store = InMemoryApiStore()
    workflow_service = FakeWorkflowService()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: workflow_service)
    client = TestClient(app)

    session_id = client.post("/api/v1/sessions", json={"user_id": "demo-user"}).json()["id"]

    default_call = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "默认模式"},
    )
    dynamic_call = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "动态模式", "orchestration_mode": "dynamic"},
    )
    bogus_call = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "非法模式", "orchestration_mode": "autonomous"},
    )

    assert default_call.status_code == 202
    assert dynamic_call.status_code == 202
    # 未支持的取值由契约拒绝，而不是悄悄退回 static——否则「我选了动态」会静默失效。
    assert bogus_call.status_code == 422
    assert [
        task.orchestration_mode for task in workflow_service.scheduled_tasks
    ] == [None, "dynamic"]


class _AlwaysAvailableSandbox:
    def available(self) -> bool:
        return True


def test_sandbox_status_reports_denied_backend(monkeypatch) -> None:
    """沙箱状态是只读诊断：说清当前能不能用、为什么不能用。"""

    from app.sandbox import SandboxSettings

    monkeypatch.setattr(
        api_main,
        "get_sandbox_settings",
        lambda: SandboxSettings(backend="denied", timeout_seconds=7),
    )
    client = TestClient(app)

    response = client.get("/api/v1/config/sandbox")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "denied"
    assert payload["available"] is False
    assert "denied" in payload["reason"]
    assert payload["limits"]["timeout_seconds"] == 7
    # 运行期没有写入口：这些参数属部署期安全边界。
    assert client.put("/api/v1/config/sandbox", json={"timeout_seconds": 999}).status_code == 405


def test_sandbox_status_reports_available_docker_backend(monkeypatch) -> None:
    from app.sandbox import SandboxSettings

    monkeypatch.setattr(
        api_main,
        "get_sandbox_settings",
        lambda: SandboxSettings(backend="docker", image="python:3.12-slim"),
    )
    monkeypatch.setattr(api_main, "build_sandbox", lambda settings: _AlwaysAvailableSandbox())
    client = TestClient(app)

    payload = client.get("/api/v1/config/sandbox").json()

    assert payload["available"] is True
    assert payload["reason"] is None
    assert payload["image"] == "python:3.12-slim"


def test_sandbox_status_explains_missing_docker_socket(monkeypatch) -> None:
    from app.sandbox import SandboxSettings

    class _ProbeFails:
        def available(self) -> bool:
            raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(
        api_main, "get_sandbox_settings", lambda: SandboxSettings(backend="docker")
    )
    monkeypatch.setattr(api_main, "build_sandbox", lambda settings: _ProbeFails())
    client = TestClient(app)

    payload = client.get("/api/v1/config/sandbox").json()

    assert payload["available"] is False
    assert "FileNotFoundError" in payload["reason"]

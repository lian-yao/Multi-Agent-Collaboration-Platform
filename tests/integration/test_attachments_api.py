"""附件接口契约（ADR-021、`doc/api.md` §5.16）。

单独一个文件而不是并进 `test_api.py`：附件有一条**跨模块**链路（上传 → 挂到消息 →
回读消息），用例需要自己的上传辅助函数；塞进已有文件会让那份文件同时承担
会话生命周期与附件契约两件事。
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

import app.api.main as api_main
from app.api.main import app
from app.api.store import InMemoryApiStore


class FakeWorkflowService:
    def __init__(self) -> None:
        self.scheduled_tasks: list[object] = []

    def schedule(self, task) -> str:
        self.scheduled_tasks.append(task)
        return task.workflow_id


def _setup(monkeypatch) -> tuple[TestClient, InMemoryApiStore, FakeWorkflowService]:
    store = InMemoryApiStore()
    service = FakeWorkflowService()
    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: service)
    return TestClient(app), store, service


def _upload(client: TestClient, name: str, raw: bytes, mime: str = ""):
    return client.post(
        "/api/v1/attachments",
        json={
            "name": name,
            "mime": mime,
            "data_base64": base64.b64encode(raw).decode("ascii"),
        },
    )


def test_upload_attachment_and_attach_to_message(monkeypatch) -> None:
    client, _, service = _setup(monkeypatch)

    uploaded = _upload(client, "notes.md", "核心结论：先做度量。".encode())
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["kind"] == "text"
    assert body["status"] == "ready"
    assert body["message_id"] is None
    # 上传不依赖会话存在：草稿态下还没有会话 id（`doc/api.md` §4.2）。
    assert body["session_id"] is None

    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    accepted = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "看看这份笔记", "attachment_ids": [body["id"]]},
    )
    assert accepted.status_code == 202
    payload = accepted.json()
    assert payload["unattached_attachment_ids"] == []
    assert [item["id"] for item in payload["attachments"]] == [body["id"]]
    # 只传 id：附件内容可能很大，进不了工作流输入（Dapr gRPC 默认 4 MB）。
    assert service.scheduled_tasks[0].attachment_ids == [body["id"]]

    messages = client.get(f"/api/v1/sessions/{session_id}/messages").json()
    assert [item["id"] for item in messages["items"][0]["attachments"]] == [body["id"]]


def test_message_can_carry_attachment_without_text(monkeypatch) -> None:
    """只传图不打字是常见用法；约束是 content 与 attachment_ids 不能同时为空。"""

    client, _, _ = _setup(monkeypatch)
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    shot = _upload(client, "shot.png", b"\x89PNG\r\n\x1a\n\x00\x00").json()

    accepted = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "", "attachment_ids": [shot["id"]]},
    )
    assert accepted.status_code == 202

    empty = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "   ", "attachment_ids": []},
    )
    assert empty.status_code == 422


def test_attachment_rejections_are_explicit(monkeypatch) -> None:
    client, _, _ = _setup(monkeypatch)

    # 不支持的格式在上传时就被拒，而不是收下来之后永远用不上。
    unsupported = _upload(client, "bundle.rar", b"Rar!\x1a\x07")
    assert unsupported.status_code == 400
    assert unsupported.json()["code"] == "ATTACHMENT_TYPE_UNSUPPORTED"

    empty = _upload(client, "empty.txt", b"")
    assert empty.status_code == 400
    assert empty.json()["code"] == "ATTACHMENT_EMPTY"

    broken = client.post(
        "/api/v1/attachments",
        json={"name": "a.txt", "mime": "", "data_base64": "!!!not base64!!!"},
    )
    assert broken.status_code == 400
    assert broken.json()["code"] == "ATTACHMENT_INVALID_BASE64"


def test_upload_accepts_data_url_prefix(monkeypatch) -> None:
    """前端 FileReader 给的是 data URL，服务端剥前缀，避免两边各写一份转换。"""

    client, _, _ = _setup(monkeypatch)

    created = client.post(
        "/api/v1/attachments",
        json={
            "name": "a.txt",
            "mime": "text/plain",
            "data_base64": "data:text/plain;base64," + base64.b64encode(b"hi").decode(),
        },
    )
    assert created.status_code == 201


def test_attachment_can_only_be_linked_once(monkeypatch) -> None:
    """附件 id 不是权限：已经被别的消息挂走的附件不能再挂一次。"""

    client, _, _ = _setup(monkeypatch)
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    attachment_id = _upload(client, "a.txt", b"hello").json()["id"]

    first = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "第一次", "attachment_ids": [attachment_id]},
    )
    second = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "第二次", "attachment_ids": [attachment_id]},
    )

    assert first.json()["unattached_attachment_ids"] == []
    # 消息本身仍发成功（部分失败），缺的附件单独回给调用方——报成 4xx 会让前端
    # 把已经发出去的消息当成没发出去。
    assert second.status_code == 202
    assert second.json()["unattached_attachment_ids"] == [attachment_id]
    assert second.json()["attachments"] == []


def test_attachment_count_is_capped_by_contract(monkeypatch) -> None:
    client, _, _ = _setup(monkeypatch)
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]

    over = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "太多附件", "attachment_ids": [f"id-{n}" for n in range(5)]},
    )
    assert over.status_code == 422


def test_attachment_delete_rules(monkeypatch) -> None:
    client, _, _ = _setup(monkeypatch)

    pending = _upload(client, "temp.txt", b"tmp").json()["id"]
    assert client.delete(f"/api/v1/attachments/{pending}").status_code == 204
    assert client.delete(f"/api/v1/attachments/{pending}").status_code == 404

    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    sent = _upload(client, "keep.txt", b"keep").json()["id"]
    client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "发出去", "attachment_ids": [sent]},
    )
    # 已随消息发出的附件不能单独删：历史消息里的附件引用会变成空洞。
    conflict = client.delete(f"/api/v1/attachments/{sent}")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ATTACHMENT_ALREADY_SENT"


def test_attachment_content_download(monkeypatch) -> None:
    client, _, _ = _setup(monkeypatch)

    raw = b"\x89PNG\r\n\x1a\npixels"
    image_id = _upload(client, "图 片.png", raw, "image/png").json()["id"]

    downloaded = client.get(f"/api/v1/attachments/{image_id}/content")
    assert downloaded.status_code == 200
    assert downloaded.content == raw
    assert downloaded.headers["content-type"].startswith("image/png")
    # 文件名含中文时用 RFC 5987，否则响应头按 latin-1 编码会直接抛错。
    assert "filename*=UTF-8" in downloaded.headers["content-disposition"]

    # 解析失败的附件没有可回的内容，回 404 而不是空 200。
    failed = _upload(client, "scan.pdf", b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n").json()
    assert failed["status"] == "failed"
    assert client.get(f"/api/v1/attachments/{failed['id']}/content").status_code == 404


def test_deleting_session_removes_its_attachments(monkeypatch) -> None:
    client, store, _ = _setup(monkeypatch)
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    attachment_id = _upload(client, "a.txt", b"hello").json()["id"]
    client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "带着附件", "attachment_ids": [attachment_id]},
    )
    assert store.attachments  # 已挂上

    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 204

    # 附件与消息同样随会话级联清理，不留孤儿行（SQL 侧由外键 ON DELETE CASCADE 保证）。
    assert store.attachments == {}

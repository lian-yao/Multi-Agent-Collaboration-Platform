"""角色 C D3-4：会话与记忆数据结构的单元测试。

验证 Schema 字段、序列化往返、key 命名与读写接口契约，
不依赖任何外部服务（对齐 tests/unit 无需外部服务的前提）。
"""

import json
from datetime import datetime, timezone

from app.memory import (
    ConversationMemory,
    LongTermMemory,
    MemoryEntry,
    MessageRole,
    MessageStatus,
    SessionMessage,
    agent_memory_key,
    deserialize_memory_entry,
    deserialize_message,
    memory_entry_value,
    serialize_memory_entry,
    serialize_message,
    session_messages_key,
)


def test_message_roles_match_doc():
    assert {role.value for role in MessageRole} == {
        "user",
        "assistant",
        "system",
        "tool",
    }


def test_message_statuses_match_doc():
    assert {status.value for status in MessageStatus} == {
        "queued",
        "running",
        "completed",
        "failed",
    }


def test_session_message_requires_session_role_content():
    try:
        SessionMessage()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic.ValidationError
        assert "session_id" in str(exc)
    else:
        raise AssertionError("缺少必填字段时应报错")


def test_session_message_defaults_status_completed():
    message = SessionMessage(
        session_id="s1", role=MessageRole.USER, content="你好"
    )

    assert message.status is MessageStatus.COMPLETED
    assert message.id is None
    assert message.agent_run_id is None
    assert message.created_at.tzinfo is not None


def test_message_json_roundtrip_preserves_fields():
    message = SessionMessage(
        id="m-1",
        session_id="s1",
        agent_run_id="run-1",
        role=MessageRole.ASSISTANT,
        content="回复",
        status=MessageStatus.COMPLETED,
        created_at=datetime(2026, 9, 7, 8, 0, 0, tzinfo=timezone.utc),
    )

    restored = deserialize_message(serialize_message(message))

    assert restored == message
    assert restored.content == "回复"


def test_message_roundtrip_accepts_bytes():
    message = SessionMessage(
        session_id="s1", role=MessageRole.USER, content="字节输入"
    )

    restored = deserialize_message(serialize_message(message).encode("utf-8"))

    assert restored == message


def test_memory_entry_requires_key_content_agent():
    try:
        MemoryEntry()  # type: ignore[call-arg]
    except Exception as exc:
        assert "key" in str(exc)
    else:
        raise AssertionError("缺少必填字段时应报错")


def test_memory_entry_value_excludes_key():
    entry = MemoryEntry(
        key="prefers-zh",
        content="使用中文回复",
        agent_id="a1",
        updated_at=datetime(2026, 9, 7, 8, 0, 0, tzinfo=timezone.utc),
    )

    value = json.loads(memory_entry_value(entry))

    assert value == {
        "content": "使用中文回复",
        "agent_id": "a1",
        "updated_at": "2026-09-07T08:00:00Z",
    }
    assert "key" not in value


def test_memory_entry_json_roundtrip_preserves_key():
    entry = MemoryEntry(
        key="prefers-zh",
        content="使用中文回复",
        agent_id="a1",
        updated_at=datetime(2026, 9, 7, 8, 0, 0, tzinfo=timezone.utc),
    )

    restored = deserialize_memory_entry(serialize_memory_entry(entry))

    assert restored == entry
    assert restored.key == "prefers-zh"


def test_session_messages_key_matches_doc():
    assert session_messages_key("abc-123") == "session:abc-123:messages"


def test_agent_memory_key_matches_doc():
    assert agent_memory_key("collector") == "agent:collector:memory"


def test_in_memory_store_satisfies_conversation_protocol():
    """内存 dict 实现满足 ConversationMemory 契约（鸭子类型，不引外部服务）。"""

    class InMemoryConversation:
        def __init__(self) -> None:
            self._messages: list[SessionMessage] = []

        def append_message(
            self, session_id: str, message: SessionMessage
        ) -> None:
            self._messages.append(message)

        def list_messages(
            self, session_id: str, *, limit: int | None = None
        ) -> list[SessionMessage]:
            messages = list(self._messages)
            return messages[-limit:] if limit is not None else messages

    store = InMemoryConversation()
    assert isinstance(store, ConversationMemory)

    store.append_message(
        "s1", SessionMessage(session_id="s1", role=MessageRole.USER, content="a")
    )
    store.append_message(
        "s1",
        SessionMessage(session_id="s1", role=MessageRole.ASSISTANT, content="b"),
    )

    assert [m.content for m in store.list_messages("s1", limit=1)] == ["b"]


def test_in_memory_store_satisfies_long_term_memory_protocol():
    """内存 dict 实现满足 LongTermMemory 契约。"""

    class InMemoryLongTerm:
        def __init__(self) -> None:
            self._entries: dict[str, dict[str, MemoryEntry]] = {}

        def save_entry(self, agent_id: str, entry: MemoryEntry) -> None:
            self._entries.setdefault(agent_id, {})[entry.key] = entry

        def get_entry(self, agent_id: str, key: str) -> MemoryEntry | None:
            return self._entries.get(agent_id, {}).get(key)

        def list_entries(self, agent_id: str) -> list[MemoryEntry]:
            return list(self._entries.get(agent_id, {}).values())

    store = InMemoryLongTerm()
    assert isinstance(store, LongTermMemory)

    store.save_entry(
        "a1", MemoryEntry(key="k1", content="v1", agent_id="a1")
    )
    store.save_entry(
        "a1",
        MemoryEntry(key="k1", content="v1b", agent_id="a1"),
    )

    assert store.get_entry("a1", "k1") is not None
    assert store.get_entry("a1", "k1").content == "v1b"  # type: ignore[union-attr]
    assert store.get_entry("a1", "missing") is None
    assert len(store.list_entries("a1")) == 1
    assert store.list_entries("a2") == []

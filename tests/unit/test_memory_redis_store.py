"""成员 C D3-4：记忆的 Redis 实现（会话记忆 List / 长期记忆 Hash）。

对齐事实源 `doc/15 AI Native多智能体协作平台.md` 模块 4「智能体记忆管理」与
ADR-005 的数据形状：

- 会话记忆 `session:{id}:messages`——Redis List、TTL 7 天、读最近 N 条；
- 长期记忆 `agent:{id}:memory`——Redis Hash、field = 记忆项 key、无 TTL。

按 `doc/testing.md` §1 的替身约定，这里用内存 Redis 替身，不连真实 Redis；
真实 Redis 上的验收见 `tests/e2e/test_live_e2e.py` 所在的 compose 环境。

降级语义是本文件的重点：Redis 只服务会话上下文、可丢失
（`doc/data-model.md` §5），因此读写失败必须退化为「空 / 无操作」而不是抛异常——
一条缓存写不进去不该让整次协作失败。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.memory import (
    ConversationMemory,
    LongTermMemory,
    MemoryEntry,
    MessageRole,
    RedisConversationMemory,
    RedisLongTermMemory,
    SessionMessage,
    agent_memory_key,
    build_conversation_memory,
    build_long_term_memory,
    memory_entry_from_value,
    memory_entry_value,
    session_messages_key,
)


# --------------------------------------------------------------------------- #
# 内存 Redis 替身
# --------------------------------------------------------------------------- #


class _FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self._ops: list[tuple[Any, ...]] = []

    def rpush(self, key: str, value: str) -> "_FakePipeline":
        self._ops.append(("rpush", key, value))
        return self

    def ltrim(self, key: str, start: int, stop: int) -> "_FakePipeline":
        self._ops.append(("ltrim", key, start, stop))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self._ops.append(("expire", key, seconds))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op in self._ops:
            results.append(self._client.apply(op))
        self._ops.clear()
        return results


class FakeRedis:
    """只实现记忆读写用到的那几个命令，语义与 Redis 对齐（含负下标 LTRIM）。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}
        self.fail_on: set[str] = set()

    # -- 命令 ------------------------------------------------------------- #

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        self._guard("lrange")
        return self._slice(self.lists.get(key, []), start, stop)

    def hset(self, key: str, field: str, value: str) -> int:
        self._guard("hset")
        bucket = self.hashes.setdefault(key, {})
        created = field not in bucket
        bucket[field] = value
        return 1 if created else 0

    def hget(self, key: str, field: str) -> str | None:
        self._guard("hget")
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        self._guard("hgetall")
        return dict(self.hashes.get(key, {}))

    def apply(self, op: tuple[Any, ...]) -> Any:
        name = op[0]
        self._guard(name)
        if name == "rpush":
            _, key, value = op
            self.lists.setdefault(key, []).append(value)
            return len(self.lists[key])
        if name == "ltrim":
            _, key, start, stop = op
            self.lists[key] = self._slice(self.lists.get(key, []), start, stop)
            return "OK"
        _, key, seconds = op
        self.expires[key] = int(seconds)
        return True

    # -- 辅助 ------------------------------------------------------------- #

    def _guard(self, command: str) -> None:
        if command in self.fail_on:
            raise ConnectionError(f"redis 不可用（{command}）")

    @staticmethod
    def _slice(values: list[str], start: int, stop: int) -> list[str]:
        size = len(values)
        begin = start + size if start < 0 else start
        end = stop + size if stop < 0 else stop
        begin = max(0, begin)
        if end < 0 or begin > end:
            return []
        return list(values[begin : end + 1])


def _message(content: str, *, role: MessageRole = MessageRole.USER) -> SessionMessage:
    return SessionMessage(session_id="s1", role=role, content=content)


def _entry(key: str, content: str) -> MemoryEntry:
    return MemoryEntry(key=key, content=content, agent_id="a1")


# --------------------------------------------------------------------------- #
# 会话记忆
# --------------------------------------------------------------------------- #


def test_conversation_memory_satisfies_the_declared_protocol():
    memory = RedisConversationMemory(FakeRedis())

    assert isinstance(memory, ConversationMemory)


def test_append_writes_the_contract_key_with_ttl():
    client = FakeRedis()
    RedisConversationMemory(client).append_message("s1", _message("第一条"))

    key = session_messages_key("s1")
    assert key == "session:s1:messages"
    assert len(client.lists[key]) == 1
    assert client.expires[key] == 7 * 24 * 3600


def test_list_messages_returns_chronological_order():
    memory = RedisConversationMemory(FakeRedis())
    for index in range(3):
        memory.append_message("s1", _message(f"第 {index} 条"))

    contents = [message.content for message in memory.list_messages("s1")]

    assert contents == ["第 0 条", "第 1 条", "第 2 条"]


def test_list_messages_limit_reads_the_most_recent():
    """`limit` 是「读最近 N 条」而不是「读最前 N 条」（`app/memory/store.py` 契约）。"""

    memory = RedisConversationMemory(FakeRedis())
    for index in range(5):
        memory.append_message("s1", _message(f"第 {index} 条"))

    contents = [message.content for message in memory.list_messages("s1", limit=2)]

    assert contents == ["第 3 条", "第 4 条"]


def test_append_trims_the_list_to_the_configured_bound():
    """Redis List 没有天然边界，超出上限时必须裁掉最旧的。"""

    client = FakeRedis()
    memory = RedisConversationMemory(client, max_messages=3)
    for index in range(5):
        memory.append_message("s1", _message(f"第 {index} 条"))

    assert [message.content for message in memory.list_messages("s1")] == [
        "第 2 条",
        "第 3 条",
        "第 4 条",
    ]
    assert len(client.lists[session_messages_key("s1")]) == 3


def test_list_messages_skips_a_corrupt_entry_without_losing_history():
    """单条损坏只跳过它——不能因为一条脏数据把整段上下文清空。"""

    client = FakeRedis()
    memory = RedisConversationMemory(client)
    memory.append_message("s1", _message("好的"))
    client.lists[session_messages_key("s1")].insert(1, "{不是合法 JSON")
    memory.append_message("s1", _message("仍在"))

    assert [message.content for message in memory.list_messages("s1")] == ["好的", "仍在"]


def test_read_failure_degrades_to_empty_context():
    client = FakeRedis()
    client.fail_on.add("lrange")

    assert RedisConversationMemory(client).list_messages("s1") == []


def test_append_failure_does_not_raise():
    """会话记忆是缓存，写不进去只记警告：不能让一次协作因为缓存失败而中断。"""

    client = FakeRedis()
    client.fail_on.add("rpush")

    RedisConversationMemory(client).append_message("s1", _message("写入失败"))

    assert client.lists.get(session_messages_key("s1")) is None


# --------------------------------------------------------------------------- #
# 长期记忆
# --------------------------------------------------------------------------- #


def test_long_term_memory_satisfies_the_declared_protocol():
    assert isinstance(RedisLongTermMemory(FakeRedis()), LongTermMemory)


def test_save_and_get_entry_round_trip():
    client = FakeRedis()
    memory = RedisLongTermMemory(client)
    memory.save_entry("a1", _entry("偏好", "回答尽量简短"))

    restored = memory.get_entry("a1", "偏好")

    assert restored is not None
    assert restored.key == "偏好"
    assert restored.content == "回答尽量简短"
    assert restored.agent_id == "a1"
    assert agent_memory_key("a1") == "agent:a1:memory"


def test_save_entry_overwrites_the_same_key():
    memory = RedisLongTermMemory(FakeRedis())
    memory.save_entry("a1", _entry("偏好", "旧值"))
    memory.save_entry("a1", _entry("偏好", "新值"))

    entries = memory.list_entries("a1")

    assert [entry.content for entry in entries] == ["新值"]


def test_list_entries_is_sorted_by_key():
    memory = RedisLongTermMemory(FakeRedis())
    for key in ("c", "a", "b"):
        memory.save_entry("a1", _entry(key, f"{key} 的内容"))

    assert [entry.key for entry in memory.list_entries("a1")] == ["a", "b", "c"]


def test_missing_entry_returns_none():
    assert RedisLongTermMemory(FakeRedis()).get_entry("a1", "不存在") is None


def test_long_term_read_failure_degrades():
    client = FakeRedis()
    client.fail_on.update({"hget", "hgetall"})
    memory = RedisLongTermMemory(client)

    assert memory.get_entry("a1", "偏好") is None
    assert memory.list_entries("a1") == []


def test_long_term_save_failure_does_not_raise():
    client = FakeRedis()
    client.fail_on.add("hset")

    RedisLongTermMemory(client).save_entry("a1", _entry("偏好", "写不进去"))

    assert client.hashes == {}


def test_list_entries_skips_a_corrupt_value():
    client = FakeRedis()
    memory = RedisLongTermMemory(client)
    memory.save_entry("a1", _entry("好的", "内容"))
    client.hashes[agent_memory_key("a1")]["坏的"] = "[]"

    assert [entry.key for entry in memory.list_entries("a1")] == ["好的"]


# --------------------------------------------------------------------------- #
# Hash value 编解码
# --------------------------------------------------------------------------- #


def test_memory_entry_value_round_trips_through_the_hash_field():
    entry = MemoryEntry(
        key="偏好",
        content="回答尽量简短",
        agent_id="a1",
        updated_at=datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc),
    )

    restored = memory_entry_from_value(entry.key, memory_entry_value(entry))

    assert restored == entry


def test_hash_field_wins_over_a_stale_key_inside_the_value():
    """value 里若残留了 key，一律以 Hash 的 field 为准，避免读到旧 key。"""

    payload = (
        '{"key": "旧的", "content": "内容", "agent_id": "a1",'
        ' "updated_at": "2026-09-16T08:00:00Z"}'
    )

    assert memory_entry_from_value("新的", payload).key == "新的"


def test_builders_use_the_injected_client():
    """工厂函数必须能用注入的客户端构造，从而在单元测试里完全不连 Redis。"""

    client = FakeRedis()

    conversation = build_conversation_memory(client, max_messages=2)
    long_term = build_long_term_memory(client)
    conversation.append_message("s1", _message("注入的客户端"))
    long_term.save_entry("a1", _entry("偏好", "内容"))

    assert [item.content for item in conversation.list_messages("s1")] == ["注入的客户端"]
    assert long_term.get_entry("a1", "偏好") is not None

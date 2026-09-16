"""记忆的 Redis 实现：会话记忆 List + 长期记忆 Hash（成员 C D3-4，契约见 ADR-005）。

数据形状由 `app/memory/schemas.py` 定义，本模块只负责读写与降级：

- **会话记忆**：`session:{id}:messages`，Redis List（最新在尾部），TTL 7 天；
  写入时按 `max_messages` 裁剪，避免列表无界增长；
- **长期记忆**：`agent:{id}:memory`，Redis Hash，field=记忆项 key，无 TTL。

**降级策略**：按 `doc/data-model.md` §5，Redis 只服务会话上下文读取、可丢失，
PostgreSQL 的 `messages` 表才是最终事实源。所以 Redis 不可用时读写都退化为
「空 / 无操作」并记一条警告，**不向流水线抛异常**——记忆缺失不该让一次协作失败。
这与 `app/core/provider_config.py` 对 Redis 镜像的处理保持一致。

**边界**：本模块只提供读写实现，不决定「历史消息怎么进提示词」。
注入点（构造 `WorkflowTask` 与拼装模型 messages 的位置）在编排/工作流层，
需要与那两处的归属方一起接；`list_messages` 就是给它们用的读取口。
"""

from __future__ import annotations

from typing import Any

from app.core.storage import get_storage_settings
from app.memory.schemas import (
    MemoryEntry,
    SessionMessage,
    agent_memory_key,
    deserialize_message,
    memory_entry_from_value,
    memory_entry_value,
    serialize_message,
    session_messages_key,
)
from app.observability.logging import get_logger, log_event

logger = get_logger("memory.redis")

DEFAULT_CONVERSATION_TTL_SECONDS = 7 * 24 * 3600
"""会话记忆 TTL：7 天（`doc/data-model.md` §4）。"""

DEFAULT_MAX_MESSAGES = 200
"""单个会话缓存的最近消息条数上限。

Redis List 没有天然边界，只写不裁的话一次长会话会无限增长；
按「最近 N 条」裁剪与 `list_messages(limit=...)`（读最近 N 条）语义一致。
"""


class RedisConversationMemory:
    """会话上下文（短期记忆）：Redis List 实现。"""

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int = DEFAULT_CONVERSATION_TTL_SECONDS,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self._client = client
        self._ttl = max(1, int(ttl_seconds))
        self._max_messages = max(1, int(max_messages))

    def append_message(self, session_id: str, message: SessionMessage) -> None:
        """追加一条消息；列表裁到最近 `max_messages` 条并续期 TTL。"""

        key = session_messages_key(session_id)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(key, serialize_message(message))
            pipe.ltrim(key, -self._max_messages, -1)
            pipe.expire(key, self._ttl)
            pipe.execute()
        except Exception as exc:
            _warn("memory.conversation_append_failed", session_id=session_id, error=exc)

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[SessionMessage]:
        """按时间正序读取最近的消息；Redis 不可用时返回空列表。"""

        count = self._max_messages if limit is None else int(limit)
        count = max(1, min(count, self._max_messages))
        try:
            raw = self._client.lrange(session_messages_key(session_id), -count, -1)
        except Exception as exc:
            _warn("memory.conversation_read_failed", session_id=session_id, error=exc)
            return []
        return _decode_messages(raw, session_id=session_id)


class RedisLongTermMemory:
    """长期记忆：Redis Hash 实现（field = 记忆项 key）。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def save_entry(self, agent_id: str, entry: MemoryEntry) -> None:
        """保存一条长期记忆，同 key 覆盖。"""

        try:
            self._client.hset(
                agent_memory_key(agent_id), entry.key, memory_entry_value(entry)
            )
        except Exception as exc:
            _warn("memory.long_term_save_failed", agent_id=agent_id, error=exc)

    def get_entry(self, agent_id: str, key: str) -> MemoryEntry | None:
        try:
            raw = self._client.hget(agent_memory_key(agent_id), key)
        except Exception as exc:
            _warn("memory.long_term_read_failed", agent_id=agent_id, error=exc)
            return None
        if raw is None:
            return None
        try:
            return memory_entry_from_value(key, raw)
        except Exception as exc:
            _warn("memory.long_term_entry_corrupt", agent_id=agent_id, key=key, error=exc)
            return None

    def list_entries(self, agent_id: str) -> list[MemoryEntry]:
        """列出全部长期记忆，按 key 排序；无法解析的条目跳过并记警告。"""

        try:
            raw = self._client.hgetall(agent_memory_key(agent_id)) or {}
        except Exception as exc:
            _warn("memory.long_term_read_failed", agent_id=agent_id, error=exc)
            return []

        entries: list[MemoryEntry] = []
        for field, value in raw.items():
            key = field.decode("utf-8") if isinstance(field, bytes) else str(field)
            try:
                entries.append(memory_entry_from_value(key, value))
            except Exception as exc:
                _warn("memory.long_term_entry_corrupt", agent_id=agent_id, key=key, error=exc)
        entries.sort(key=lambda entry: entry.key)
        return entries


def build_redis_client(url: str | None = None) -> Any:
    """按 `REDIS_URL`（默认 `StorageSettings.redis_url`）建立客户端。

    连接是惰性的：这里只构造对象，不在调用点建连，因此可以在模块级安全调用。
    """

    from redis import Redis

    return Redis.from_url(
        url or get_storage_settings().redis_url, decode_responses=True
    )


def build_conversation_memory(
    client: Any | None = None,
    *,
    ttl_seconds: int = DEFAULT_CONVERSATION_TTL_SECONDS,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> RedisConversationMemory:
    """构造会话记忆；`client` 供测试注入内存替身。"""

    return RedisConversationMemory(
        client if client is not None else build_redis_client(),
        ttl_seconds=ttl_seconds,
        max_messages=max_messages,
    )


def build_long_term_memory(client: Any | None = None) -> RedisLongTermMemory:
    """构造长期记忆；`client` 供测试注入内存替身。"""

    return RedisLongTermMemory(client if client is not None else build_redis_client())


def _decode_messages(raw: Any, *, session_id: str) -> list[SessionMessage]:
    """逐条还原；单条损坏只跳过它，不把整段历史清空。"""

    messages: list[SessionMessage] = []
    for payload in raw or []:
        try:
            messages.append(deserialize_message(payload))
        except Exception as exc:
            _warn("memory.conversation_entry_corrupt", session_id=session_id, error=exc)
    return messages


def _warn(event: str, **fields: Any) -> None:
    error = fields.pop("error", None)
    log_event(
        logger,
        event,
        error=None if error is None else f"{type(error).__name__}: {error}",
        **fields,
    )


__all__ = [
    "DEFAULT_CONVERSATION_TTL_SECONDS",
    "DEFAULT_MAX_MESSAGES",
    "RedisConversationMemory",
    "RedisLongTermMemory",
    "build_conversation_memory",
    "build_long_term_memory",
    "build_redis_client",
]

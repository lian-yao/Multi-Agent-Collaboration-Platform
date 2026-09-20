"""会话与长期记忆：数据结构定义、读写接口契约与 Redis 实现。"""

from app.memory.redis_store import (
    DEFAULT_CONVERSATION_TTL_SECONDS,
    DEFAULT_MAX_MESSAGES,
    RedisConversationMemory,
    RedisLongTermMemory,
    build_conversation_memory,
    build_long_term_memory,
    build_redis_client,
)
from app.memory.runtime import conversation_memory, set_conversation_memory_factory
from app.memory.schemas import (
    MemoryEntry,
    MessageRole,
    MessageStatus,
    SessionMessage,
    agent_memory_key,
    deserialize_memory_entry,
    deserialize_message,
    memory_entry_from_value,
    memory_entry_value,
    serialize_memory_entry,
    serialize_message,
    session_messages_key,
)
from app.memory.store import ConversationMemory, LongTermMemory

__all__ = [
    "ConversationMemory",
    "DEFAULT_CONVERSATION_TTL_SECONDS",
    "DEFAULT_MAX_MESSAGES",
    "LongTermMemory",
    "MemoryEntry",
    "MessageRole",
    "MessageStatus",
    "RedisConversationMemory",
    "RedisLongTermMemory",
    "SessionMessage",
    "agent_memory_key",
    "build_conversation_memory",
    "build_long_term_memory",
    "build_redis_client",
    "conversation_memory",
    "deserialize_memory_entry",
    "deserialize_message",
    "memory_entry_from_value",
    "memory_entry_value",
    "serialize_memory_entry",
    "serialize_message",
    "session_messages_key",
    "set_conversation_memory_factory",
]

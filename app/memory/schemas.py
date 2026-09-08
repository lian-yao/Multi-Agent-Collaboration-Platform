"""会话与记忆数据结构定义。

对齐 doc/data-model.md 与 doc/api.md 的字段契约：
- 会话记忆（短期）：`session:{id}:messages`，Redis List（JSON 消息），TTL 7 天；
  可丢失，PostgreSQL 为最终事实源（data-model §4/§5）。
- 长期记忆：`agent:{id}:memory`，Redis Hash，field=记忆项 key，value=JSON
  {content, agent_id, updated_at}；无 TTL，跨会话，写前先落审计（data-model §4）。
- 本期不引入向量检索（设计文档标记为可选，见 ADR-005）。

本模块只提供数据结构、序列化与 key 命名，不产生任何外部 IO；
真实读写由实现 ConversationMemory / LongTermMemory 契约的存储层完成。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    """消息角色，取值对齐 doc/api.md §2 与 doc/data-model.md §3。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MessageStatus(StrEnum):
    """消息处理状态，取值对齐 doc/data-model.md §3 与 doc/api.md §2。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionMessage(BaseModel):
    """会话中的一条消息，作为 `session:{id}:messages` 列表里的 JSON 条目。

    字段契约见 data-model §3 messages 表与 api.md §2 Message：
    - id / agent_run_id 允许为空（消息可由编排层自动产生）；
    - status 默认 completed，与 Redis 缓存只读语义一致。
    """

    session_id: str = Field(min_length=1)
    role: MessageRole
    content: str
    id: str | None = None
    agent_run_id: str | None = None
    status: MessageStatus = MessageStatus.COMPLETED
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MemoryEntry(BaseModel):
    """一条长期记忆项，存储于 `agent:{id}:memory` Hash。

    Hash 的 field 为记忆项 key，value 为不含 key 的 JSON（见 memory_entry_value），
    {content, agent_id, updated_at} 三字段契约见 ADR-005 与 data-model §4 细化。
    """

    key: str = Field(min_length=1)
    content: str
    agent_id: str = Field(min_length=1)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def session_messages_key(session_id: str) -> str:
    """返回会话消息缓存的 Redis key：`session:{id}:messages`。"""
    return f"session:{session_id}:messages"


def agent_memory_key(agent_id: str) -> str:
    """返回长期记忆的 Redis key：`agent:{id}:memory`。"""
    return f"agent:{agent_id}:memory"


def serialize_message(message: SessionMessage) -> str:
    """将会话消息序列化为可写入 Redis List 的 JSON 字符串。"""
    return message.model_dump_json()


def deserialize_message(payload: str | bytes) -> SessionMessage:
    """从 Redis List 条目还原会话消息。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return SessionMessage.model_validate_json(payload)


def memory_entry_value(entry: MemoryEntry) -> str:
    """返回长期记忆 Hash 的 value（不含 key 字段的 JSON）。"""
    data = entry.model_dump(mode="json", exclude={"key"})
    return json.dumps(data, ensure_ascii=False)


def serialize_memory_entry(entry: MemoryEntry) -> str:
    """返回含 key 的完整记忆项 JSON（用于展示/审计，不入 Hash 结构）。"""
    return entry.model_dump_json()


def deserialize_memory_entry(payload: str | bytes) -> MemoryEntry:
    """从 JSON 还原完整记忆项。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return MemoryEntry.model_validate_json(payload)

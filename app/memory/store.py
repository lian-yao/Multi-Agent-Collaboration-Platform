"""记忆读写接口契约。

仅声明会话记忆（短期）与长期记忆的读写协议与一致性语义，
不包含任何实现或外部 IO。真实存储实现由存储层完成（见 ADR-005）：
- 会话记忆落 Redis List `session:{id}:messages`（TTL 7 天）；
  按 data-model §5，Redis 只服务会话上下文读取，可丢失，
  PostgreSQL messages 表为最终事实源。
- 长期记忆落 Redis Hash `agent:{id}:memory`；写前先落审计（data-model §4）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.memory.schemas import MemoryEntry, SessionMessage


@runtime_checkable
class ConversationMemory(Protocol):
    """会话上下文（短期记忆）读写契约。"""

    def append_message(self, session_id: str, message: SessionMessage) -> None:
        """把一条消息追加到会话消息缓存末尾。"""
        ...

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[SessionMessage]:
        """读取会话消息缓存，按时间正序；limit 限制条数（读最近 N 条）。"""
        ...


@runtime_checkable
class LongTermMemory(Protocol):
    """长期记忆读写契约（跨会话偏好与知识）。"""

    def save_entry(self, agent_id: str, entry: MemoryEntry) -> None:
        """保存一条长期记忆；key 相同则覆盖（写前由调用方保证已落审计）。"""
        ...

    def get_entry(self, agent_id: str, key: str) -> MemoryEntry | None:
        """按 key 读取一条长期记忆，不存在返回 None。"""
        ...

    def list_entries(self, agent_id: str) -> list[MemoryEntry]:
        """列出某 Agent 的全部长期记忆项。"""
        ...

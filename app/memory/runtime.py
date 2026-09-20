"""记忆的运行期入口：给消费方一个可替换的 `ConversationMemory` 实例。

ADR-005 把「历史消息怎么进提示词」留给编排/工作流层，本模块只回答「谁提供会话记忆
实例」，与 `app/core/provider_config.py::set_redis_factory`、
`app/orchestration/tools.py::set_tool_registry_factory` 是同一模式：

- 默认按 `REDIS_URL` 构造 Redis 实现（`build_conversation_memory`）；
- 测试用 `set_conversation_memory_factory` 注入内存替身，不依赖真实 Redis，
  也不会把用例数据写进开发环境的 Redis；
- Redis 不可用时由实现层（`app/memory/redis_store.py`）降级为「空 / 无操作」，
  因此消费方不需要 try/except，记忆缺失不该让一次协作失败（`doc/data-model.md` §5）。

接线口径（写点/读点）见 `doc/decisions/019-memory-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from app.memory.redis_store import build_conversation_memory
from app.memory.store import ConversationMemory

_CONVERSATION_MEMORY_FACTORY: Callable[[], ConversationMemory] | None = None


@lru_cache(maxsize=1)
def _redis_conversation_memory() -> ConversationMemory:
    """默认实现按进程缓存：每个阶段活动都会取一次，避免反复重建 Redis 连接池。"""

    return build_conversation_memory()


def set_conversation_memory_factory(
    factory: Callable[[], ConversationMemory] | None,
) -> None:
    """覆盖会话记忆的构造方式（主要供测试注入内存替身）；传 None 恢复默认。"""

    global _CONVERSATION_MEMORY_FACTORY
    _CONVERSATION_MEMORY_FACTORY = factory


def conversation_memory() -> ConversationMemory:
    """返回会话记忆实例：测试替身优先，否则按 Redis 配置构造。"""

    factory = _CONVERSATION_MEMORY_FACTORY
    return factory() if factory is not None else _redis_conversation_memory()


__all__ = ["conversation_memory", "set_conversation_memory_factory"]

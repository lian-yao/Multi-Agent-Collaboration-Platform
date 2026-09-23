"""长期记忆的**异步**读取（ADR-036）：后台预取 + 进程内缓存，**永不阻塞主流程**。

为什么不在阶段里同步读：阶段活动在关键路径上，一次 Redis 抖动就能把整次执行拖慢，
而长期记忆是**增强**而不是必需——有则更好，没有也能跑。所以这里的语义只有两条：

1. `prefetch()` 在后台线程里把记忆读进缓存（带 TTL），同一 id 只允许一个在途请求；
2. `preference_block()` **只读缓存**：没就绪、过期、读失败一律返回空串，不等待、不抛错。

读失败要显式落在日志里（`memory.long_term_prefetch_failed`），但不进调用方的控制流——
「拿不到记忆」不该让一次协作失败，这与 ADR-005 对记忆层的降级口径一致。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

from app.memory import MemoryEntry
from app.memory.redis_store import build_long_term_memory
from app.observability.logging import get_logger, log_event
from app.orchestration.context import long_term_block

logger = get_logger("orchestration.long_term")

DEFAULT_TTL_SECONDS = 300.0
"""缓存有效期。取 5 分钟：偏好不会秒级变化，而执行本身通常几秒到几分钟。"""

_CACHE: dict[str, tuple[float, str]] = {}
_INFLIGHT: set[str] = set()
_LOCK = threading.Lock()

_factory: Any = build_long_term_memory
"""记忆实现的取用口。默认按 `REDIS_URL` 构造；测试用 `set_memory_factory` 注入替身
（与 `app/memory/runtime.py` 的会话记忆同一模式——那一层不动，避免同时改两个模块）。"""


def set_memory_factory(factory: Any) -> None:
    global _factory
    _factory = factory if factory is not None else build_long_term_memory


def prefetch(
    ids: Iterable[str],
    *,
    ttl: float = DEFAULT_TTL_SECONDS,
    wait: bool = False,
) -> None:
    """在**后台线程**里把若干 id 的长期记忆读进缓存。默认立即返回，不阻塞调用方。

    `wait=True` 只给测试与显式预热用：它会在当前线程里同步读一遍，语义与后台读一致。
    """

    pending = [str(value) for value in ids if value]
    if not pending:
        return
    now = time.monotonic()
    with _LOCK:
        fresh = [key for key in pending if _is_fresh(key, now, ttl)]
        if fresh:
            # 缓存还热就不重复读：受理消息会为每个角色各预取一次，没必要次次打 Redis。
            return
        todo = [key for key in pending if key not in _INFLIGHT]
        if not todo:
            return
        _INFLIGHT.update(todo)
    if wait:
        _load(todo, ttl=ttl)
        return
    threading.Thread(
        target=_load, args=(todo,), kwargs={"ttl": ttl}, name="long-term-prefetch", daemon=True
    ).start()


def preference_block(agent_id: str | None) -> str:
    """取该 id 的长期记忆渲染块；**只读缓存**，未就绪/过期/失败一律返回空串。"""

    if not agent_id:
        return ""
    with _LOCK:
        entry = _CACHE.get(str(agent_id))
    if entry is None:
        return ""
    stored_at, block = entry
    if time.monotonic() - stored_at > DEFAULT_TTL_SECONDS:
        return ""
    return block


def clear_cache() -> None:
    """清空缓存（测试与"改了记忆想立刻生效"的场景用）。"""

    with _LOCK:
        _CACHE.clear()


def _is_fresh(key: str, now: float, ttl: float) -> bool:
    entry = _CACHE.get(key)
    return entry is not None and now - entry[0] <= ttl


def _load(ids: Sequence[str], *, ttl: float) -> None:
    """真正读 Redis 的地方；每个 id 独立成败，单个失败不影响其它 id。"""

    memory = _factory()
    for agent_id in ids:
        try:
            entries = memory.list_entries(agent_id)
        except Exception as exc:  # pragma: no cover - 实现层已吞异常，这里兜底
            log_event(
                logger,
                "memory.long_term_prefetch_failed",
                agent_id=agent_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            entries = None
        try:
            if entries is None:
                continue
            block = long_term_block(entries)
            with _LOCK:
                _CACHE[agent_id] = (time.monotonic(), block)
        finally:
            with _LOCK:
                _INFLIGHT.discard(agent_id)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "clear_cache",
    "preference_block",
    "prefetch",
    "set_memory_factory",
]

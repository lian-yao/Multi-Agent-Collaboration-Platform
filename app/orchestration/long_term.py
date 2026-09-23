"""长期记忆的**异步**读取（ADR-036）：后台预取 + 进程内缓存，**永不阻塞主流程**。

为什么不在阶段里同步读：阶段活动在关键路径上，一次 Redis 抖动就能把整次执行拖慢，
而长期记忆是**增强**而不是必需——有则更好，没有也能跑。所以这里的语义只有两条：

1. `prefetch()` 在后台线程里把记忆读进缓存（带 TTL），同一 id 只允许一个在途请求；
2. `preference_block()` **只读缓存**：没就绪、过期、读失败一律返回空串，不等待、不抛错。

读失败要显式落在日志里（`memory.long_term_prefetch_failed`），但不进调用方的控制流——
「拿不到记忆」不该让一次协作失败，这与 ADR-005 对记忆层的降级口径一致。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from app.memory import MemoryEntry
from app.memory.redis_store import build_long_term_memory
from app.observability.logging import get_logger, log_event
from app.orchestration.context import long_term_block

logger = get_logger("orchestration.long_term")

DEFAULT_TTL_SECONDS = 300.0
"""缓存有效期。取 5 分钟：偏好不会秒级变化，而执行本身通常几秒到几分钟。"""

USER_MEMORY_ID = "user"
"""**使用者**长期记忆的保留 id（ADR-036 §3）。

本项目没有登录、默认只有一名使用者，所以长期记忆按使用者存**一份**，所有角色与规划节点
都读它；键格式仍是 `agent:{id}:memory`（ADR-005 不改），只是把 id 固定成这个保留值。
将来接多用户时改成 `user:{user_id}` 即可，读取端不用动——它本来就只认一个 id。
"""

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


def remember(key: str, content: str, *, memory_id: str = USER_MEMORY_ID) -> None:
    """写入一条长期记忆并**就地刷新缓存**——紧接着的那次执行要立刻看到它。"""

    entry = MemoryEntry(
        key=key,
        content=content,
        agent_id=memory_id,
        updated_at=datetime.now(timezone.utc),
    )
    _factory().save_entry(memory_id, entry)
    _reload(memory_id)


def forget(key: str, *, memory_id: str = USER_MEMORY_ID) -> bool:
    """删除一条长期记忆并刷新缓存；返回是否真的删掉了（ADR-036 §5）。

    长期记忆无 TTL，删除入口是必须的。返回布尔值让**界面**那条路能如实回答"这条还在不在"
    （删不存在 → 404），而不是假装成功；`忘记：` 指令那条路不看返回值，它只是尽力而为。
    """

    removed = _factory().delete_entry(memory_id, key)
    _reload(memory_id)
    return bool(removed)


def forget_all(*, memory_id: str = USER_MEMORY_ID) -> None:
    """清空该 id 的全部长期记忆并刷新缓存。"""

    for entry in _factory().list_entries(memory_id):
        _factory().delete_entry(memory_id, entry.key)
    _reload(memory_id)


def list_entries(*, memory_id: str = USER_MEMORY_ID) -> list[MemoryEntry]:
    """只读列出（审计入口用；ADR-036 §5）。"""

    return list(_factory().list_entries(memory_id))


def parse_directives(text: str) -> list[tuple[str, str, str]]:
    """解析消息里的记忆指令，返回 `(动作, 名称, 内容)` 三元组。

    确定性规则，**没有模型参与**（ADR-036 §4）。逐行看，只认行首：

    - `记住：<内容>` / `记住:<内容>` / `请记住：<内容>` → `("save", 自动名, 内容)`
    - `记住 <名称>：<内容>` → `("save", 名称, 内容)`
    - `忘记：<名称>` / `忘记 <名称>` → `("forget", 名称, "")`
    - `忘记全部：` / `忘记全部` → `("forget_all", "", "")`

    没给名称时用内容派生一个稳定短名（同一句话重复说会覆盖同一条，而不是越记越多）。
    """

    directives: list[tuple[str, str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("忘记全部"):
            directives.append(("forget_all", "", ""))
            continue
        if line.startswith("忘记"):
            name = _split_name(line[len("忘记") :])
            if name:
                directives.append(("forget", name, ""))
            continue
        body = None
        for prefix in ("请记住", "记住"):
            if line.startswith(prefix):
                body = line[len(prefix) :]
                break
        if body is None:
            continue
        name, content = _split_save(body)
        if content:
            directives.append(("save", name or _auto_key(content), content))
    return directives


def apply_directives(
    text: str, *, memory_id: str = USER_MEMORY_ID
) -> list[tuple[str, str, str]]:
    """执行消息里的记忆指令；返回实际执行了哪些，便于日志与回执。"""

    applied: list[tuple[str, str, str]] = []
    for action, name, content in parse_directives(text):
        if action == "save":
            remember(name, content, memory_id=memory_id)
        elif action == "forget":
            forget(name, memory_id=memory_id)
        elif action == "forget_all":
            forget_all(memory_id=memory_id)
        applied.append((action, name, content))
    return applied


def _split_save(body: str) -> tuple[str, str]:
    """把 `记住` 之后的部分拆成 `(名称, 内容)`；没写名称时名称为空串。"""

    for separator in ("：", ":"):
        if separator in body:
            head, _, tail = body.partition(separator)
            return head.strip(), tail.strip()
    # 没有分隔符：整段当内容（例如「记住 回答尽量简短」也算一条）
    return "", body.strip()


def _split_name(body: str) -> str:
    """`忘记 <名称>` / `忘记：<名称>` 里的名称。"""

    text = body.lstrip("：:").strip()
    return text


def _auto_key(content: str) -> str:
    """自动名：内容前 12 字 + 内容摘要（同一句话重复说覆盖同一条）。"""

    head = content.strip().replace("\n", " ")[:12]
    digest = hashlib.sha1(content.strip().encode("utf-8")).hexdigest()[:4]
    return f"{head}·{digest}"


def _reload(memory_id: str) -> None:
    """写完立刻刷新缓存：下一次取用要看到最新内容，而不是等 TTL 到期。"""

    block = long_term_block(_factory().list_entries(memory_id))
    with _LOCK:
        _CACHE[memory_id] = (time.monotonic(), block)


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

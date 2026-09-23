"""长期记忆的**异步读取**（ADR-036）。

这一层只有两条语义值得钉死：

1. **不阻塞**：prefetch() 立刻返回，慢的 Redis 不该拖住消息受理；
2. **拿不到就照常跑**：缓存没就绪、读失败、条目损坏——一律空串，不抛错、不等待。

注入走 `long_term.set_memory_factory`（本模块自己的口），记忆层实现本身不动。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from app.agents.roles import RoleId
from app.memory import MemoryEntry
from app.orchestration import long_term
from app.orchestration.context import long_term_block
from app.orchestration.dynamic_graph import PlanStep, step_input
from app.orchestration.pipeline_graph import _role_input


def _entry(key: str, content: str, agent_id: str = "collector") -> MemoryEntry:
    return MemoryEntry(
        key=key,
        content=content,
        agent_id=agent_id,
        updated_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )


class FakeLongTermMemory:
    """可控替身：可以指定返回、抛错，或在读取时阻塞。"""

    def __init__(
        self,
        entries: dict[str, list[MemoryEntry]] | None = None,
        *,
        error: Exception | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.entries = entries or {}
        self.error = error
        self.gate = gate
        self.calls: list[str] = []

    def list_entries(self, agent_id: str) -> list[MemoryEntry]:
        self.calls.append(agent_id)
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return list(self.entries.get(agent_id, []))

    def save_entry(self, agent_id: str, entry: MemoryEntry) -> None:
        self.calls.append(agent_id)
        bucket = self.entries.setdefault(agent_id, [])
        for index, existing in enumerate(bucket):
            if existing.key == entry.key:
                bucket[index] = entry
                return
        bucket.append(entry)

    def delete_entry(self, agent_id: str, key: str) -> bool:
        self.calls.append(agent_id)
        bucket = self.entries.get(agent_id, [])
        remaining = [entry for entry in bucket if entry.key != key]
        self.entries[agent_id] = remaining
        return len(remaining) != len(bucket)


@pytest.fixture(autouse=True)
def _clean_cache():
    long_term.clear_cache()
    yield
    long_term.clear_cache()
    long_term.set_memory_factory(None)


def test_prefetch_returns_immediately_even_when_the_store_is_slow():
    gate = threading.Event()
    memory = FakeLongTermMemory(
        {"collector": [_entry("偏好.回答长度", "尽量简短")]}, gate=gate
    )
    long_term.set_memory_factory(lambda: memory)

    started = time.monotonic()
    long_term.prefetch(["collector"])  # 读仍阻塞在 gate 上
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"预取阻塞了调用方（{elapsed:.3f}s）——它必须在后台线程里跑"
    assert long_term.preference_block("collector") == "", "缓存没就绪时只能给空串"

    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if long_term.preference_block("collector"):
            break
        time.sleep(0.02)
    assert "偏好.回答长度: 尽量简短" in long_term.preference_block("collector")


def test_prefetch_wait_reads_synchronously_for_warmup_and_tests():
    memory = FakeLongTermMemory({"analyst": [_entry("背景", "在做论文复现")]})
    long_term.set_memory_factory(lambda: memory)

    long_term.prefetch(["analyst"], wait=True)

    assert "背景: 在做论文复现" in long_term.preference_block("analyst")
    assert memory.calls == ["analyst"]


def test_cold_cache_is_empty_and_does_not_touch_the_store():
    memory = FakeLongTermMemory({"collector": [_entry("偏好", "简短")]})
    long_term.set_memory_factory(lambda: memory)

    assert long_term.preference_block("collector") == ""
    assert memory.calls == [], "没预取过就不该去读——读取只碰缓存"


def test_read_failure_degrades_to_empty_without_raising():
    memory = FakeLongTermMemory(error=RuntimeError("redis down"))
    long_term.set_memory_factory(lambda: memory)

    long_term.prefetch(["collector"], wait=True)

    assert long_term.preference_block("collector") == ""


def test_prefetch_skips_a_fresh_cache_instead_of_re_reading():
    memory = FakeLongTermMemory({"collector": [_entry("偏好", "简短")]})
    long_term.set_memory_factory(lambda: memory)

    long_term.prefetch(["collector"], wait=True)
    long_term.prefetch(["collector"], wait=True)

    assert memory.calls == ["collector"], "缓存还热就不该重复打 Redis"


def test_rendering_skips_blank_entries_and_returns_empty_for_nothing():
    assert long_term_block(()) == ""
    assert long_term_block([_entry("空", "   ")]) == ""
    block = long_term_block([_entry("偏好.语言", "中文"), _entry("空", "")])
    assert block.startswith("【长期记忆（跨会话，1 条）】")
    assert "偏好.语言: 中文" in block


def test_role_input_puts_preferences_before_history_and_task():
    from app.memory import MessageRole, SessionMessage

    history = (
        SessionMessage(session_id="s-1", role=MessageRole.USER, content="上一轮的话"),
    )

    text = _role_input(
        RoleId.COLLECTOR,
        "本轮任务",
        None,
        history,
        "【长期记忆（跨会话，1 条）】\n偏好.回答长度: 尽量简短\n",
    )

    assert text.index("长期记忆") < text.index("会话历史") < text.index("本轮任务")


def test_step_input_puts_preferences_before_task_and_duty():
    step = PlanStep(id="s1", role=RoleId.COLLECTOR, instruction="收集", depends_on=[])

    text = step_input("任务", step, {}, (), "【长期记忆（跨会话，1 条）】\n偏好: 简短\n")

    assert text.index("长期记忆") < text.index("用户任务")
    assert text.index("长期记忆") < text.index("你这一步的职责")


# ---------------------------------------------------------------------------------------
# 写入与删除：只认显式指令（ADR-036 §4）
# ---------------------------------------------------------------------------------------


def test_parse_directives_recognizes_save_and_forget_lines():
    text = "\n".join(
        [
            "帮我看看这个方案",  # 无关行不该被当成指令
            "记住：回答尽量简短",
            "记住 称呼：叫我张三",
            "请记住 时区：Asia/Shanghai",
            "忘记：称呼",
            "忘记全部：",
        ]
    )

    directives = long_term.parse_directives(text)

    assert [action for action, _, _ in directives] == [
        "save",
        "save",
        "save",
        "forget",
        "forget_all",
    ]
    assert directives[0][2] == "回答尽量简短"
    assert directives[0][1].startswith("回答尽量简短"), "没给名称时用内容派生稳定名"
    assert directives[1][1:] == ("称呼", "叫我张三")
    assert directives[2][1:] == ("时区", "Asia/Shanghai")
    assert directives[3][1] == "称呼"


def test_parse_directives_ignores_mentions_in_the_middle_of_a_sentence():
    directives = long_term.parse_directives("请先说明为什么需要记住：这句话不是指令")

    assert directives == [], "只认行首，避免正文里提到「记住」就被当成指令"


def test_remember_writes_and_refreshes_the_cache_immediately():
    memory = FakeLongTermMemory()
    long_term.set_memory_factory(lambda: memory)

    long_term.remember("回答长度", "尽量简短")

    # 就地刷新：紧接着的那次执行要立刻看到它，而不是等 TTL 到期
    assert "回答长度: 尽量简短" in long_term.preference_block(long_term.USER_MEMORY_ID)
    assert [entry.key for entry in memory.entries[long_term.USER_MEMORY_ID]] == ["回答长度"]


def test_apply_directives_executes_save_then_forget():
    memory = FakeLongTermMemory()
    long_term.set_memory_factory(lambda: memory)

    applied = long_term.apply_directives("记住 称呼：叫我张三\n忘记：称呼")

    assert [action for action, _, _ in applied] == ["save", "forget"]
    assert memory.entries[long_term.USER_MEMORY_ID] == []
    assert long_term.preference_block(long_term.USER_MEMORY_ID) == ""


def test_forget_all_clears_every_entry():
    memory = FakeLongTermMemory()
    long_term.set_memory_factory(lambda: memory)
    long_term.remember("a", "1")
    long_term.remember("b", "2")

    long_term.apply_directives("忘记全部：")

    assert memory.entries[long_term.USER_MEMORY_ID] == []

"""`tests/unit` 共享的测试替身（成员 C D7-8）。

可观测性用例统一把进程级采集器换成内存 sink：默认的 `PostgresMetricSink` 会连接
`doc/api.md` §5.5 的 `metrics` 表（该表由成员 B 建），单元测试既不依赖真实
PostgreSQL，也不该在 buffer 满时触发一次建连。

Provider 配置用例同理：模块级 Redis 客户端换成内存替身，单元测试不连真实 Redis
（`app/core/provider_config.py`，ADR-014）。

**数据库 DSN 的自足化**：`resolve_provider_settings` / `resolve_agent_settings`
的合并顺序是 Redis → PostgreSQL → 环境回退，Redis 那一段由 `memory_redis` 替身兜住，
PostgreSQL 那一段却没有。「读不到就回退环境配置」在应用层是 fail-soft 的，但
**传输层的连不上不是读失败而是阻塞**：DSN 指向一个不可达的 PostgreSQL 时，建连会
一直等在 `select` 上，用例不报错也不结束，整个 `tests/unit` 因此挂住（表现为
pytest 长时间无输出，后面的用例根本不会执行）。所以这里在导入应用模块**之前**把
DSN 钉成一个自足的内存 SQLite：查询会因「表不存在」快速失败，正好落回应用层
既定的「配置库里没有覆盖行 → 用环境默认值」分支，语义与无覆盖行一致。
要让这组用例对着真实 PostgreSQL 跑，设 `MACP_UNIT_DATABASE_URL`。

MCP 注册表快照同理：`app/mcp/registry.py` 的条目快照默认会去读 `mcp_server_registry`
表，这里固定为空（ADR-026）。它属于「编排层不读数据库」那条边界，单测不应因此连 PG。
"""

from __future__ import annotations

import os

REGRESSION_DATABASE_URL = "sqlite+pysqlite:///:memory:"
"""单元回归的默认数据库：进程内 SQLite，不建连、不落盘。"""

# 必须在导入 `app.*` 之前设置：`app/core/storage.py` 的引擎与 session 工厂是
# 模块级 lru_cache，一旦用错 DSN 建过就换不回来了。
#
# 显式给出的 DSN 优先（`MACP_UNIT_DATABASE_URL`，其次运行环境里已有的 `DATABASE_URL`）：
# 一次 pytest 进程会加载多个 conftest，若这里无条件覆盖，`DATABASE_URL=真实库 pytest tests`
# 会被静默改写成 SQLite，集成用例就再也连不上目标库了。
_explicit_dsn = os.getenv("MACP_UNIT_DATABASE_URL") or os.getenv("DATABASE_URL")
os.environ["DATABASE_URL"] = _explicit_dsn or REGRESSION_DATABASE_URL

# 以下导入一律晚于 DSN 设定（`app.core.storage` 的引擎是模块级缓存，必须先定 DSN）。
import pytest  # noqa: E402

from app.core.provider_config import set_redis_factory
from app.memory import SessionMessage
from app.memory.runtime import set_conversation_memory_factory
from app.observability.metrics import (
    MetricSample,
    MetricsCollector,
    MetricSink,
    get_metrics_collector,
    set_metrics_collector,
)


class MemoryMetricSink:
    """把采样留在内存里，供用例断言。"""

    def __init__(self) -> None:
        self.written: list[MetricSample] = []

    def write(self, samples) -> int:
        self.written.extend(samples)
        return len(samples)

    def names(self) -> list[str]:
        return [sample.metric_name for sample in self.written]

    def samples(self, metric_name: str) -> list[MetricSample]:
        return [s for s in self.written if s.metric_name == metric_name]


@pytest.fixture
def memory_metrics() -> tuple[MetricsCollector, MemoryMetricSink]:
    """安装内存采集器，并在用例结束后恢复原采集器。

    返回 `(collector, sink)`；断言采样前记得调用 `collector.flush()`。
    """

    sink: MetricSink = MemoryMetricSink()
    collector = MetricsCollector(sink=sink, enabled=True)
    previous = get_metrics_collector()
    set_metrics_collector(collector)
    yield collector, sink
    set_metrics_collector(previous)


class MemoryRedis:
    """最小 Redis 替身：只实现 Provider 配置镜像用到的 get/set。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class MemoryConversationMemory:
    """会话记忆替身：进程内 List，语义与 Redis 实现一致（按时间正序、可丢失）。

    F-06 接线后 API 与 Workflow 活动都会读写会话记忆，用例既不该依赖真实 Redis，
    也不该把测试数据写进开发环境（与 `memory_redis` 同一动机）。
    """

    def __init__(self) -> None:
        self.data: dict[str, list[SessionMessage]] = {}

    def append_message(self, session_id: str, message: SessionMessage) -> None:
        self.data.setdefault(session_id, []).append(message)

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[SessionMessage]:
        messages = list(self.data.get(session_id, []))
        if limit is None:
            return messages
        count = max(1, int(limit))
        return messages[-count:]


@pytest.fixture(autouse=True)
def memory_redis() -> MemoryRedis:
    """把 Provider 配置的 Redis 镜像换成内存替身，用例之间互不影响。"""

    redis = MemoryRedis()
    set_redis_factory(lambda: redis)
    yield redis
    set_redis_factory(None)


@pytest.fixture(autouse=True)
def memory_conversation() -> MemoryConversationMemory:
    """把会话记忆换成内存替身，用例之间互不影响。"""

    memory = MemoryConversationMemory()
    set_conversation_memory_factory(lambda: memory)
    yield memory
    set_conversation_memory_factory(None)


@pytest.fixture(autouse=True)
def memory_mcp_registry(monkeypatch) -> None:
    """编排层的 MCP 注册表快照固定为空（不给它机会去读真实 PostgreSQL）。

    需要真实条目的用例（`tests/unit/test_registry_servers.py`）自己在用例里把它
    重置为 `None` 并替换表的读取，从而仍然覆盖「加载」这条路径。
    """

    from app.mcp import registry as mcp_registry

    monkeypatch.setattr(mcp_registry, "_registered_servers_cache", [])

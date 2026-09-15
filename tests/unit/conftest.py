"""`tests/unit` 共享的测试替身（成员 C D7-8）。

可观测性用例统一把进程级采集器换成内存 sink：默认的 `PostgresMetricSink` 会连接
`doc/api.md` §5.5 的 `metrics` 表（该表由成员 B 建），单元测试既不依赖真实
PostgreSQL，也不该在 buffer 满时触发一次建连。

Provider 配置用例同理：模块级 Redis 客户端换成内存替身，单元测试不连真实 Redis
（`app/core/provider_config.py`，ADR-014）。
"""

from __future__ import annotations

import pytest

from app.core.provider_config import set_redis_factory
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


@pytest.fixture(autouse=True)
def memory_redis() -> MemoryRedis:
    """把 Provider 配置的 Redis 镜像换成内存替身，用例之间互不影响。"""

    redis = MemoryRedis()
    set_redis_factory(lambda: redis)
    yield redis
    set_redis_factory(None)

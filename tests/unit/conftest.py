"""`tests/unit` 共享的测试替身（成员 C D7-8）。

可观测性用例统一把进程级采集器换成内存 sink：默认的 `PostgresMetricSink` 会连接
`doc/api.md` §5.5 的 `metrics` 表（该表由成员 B 建），单元测试既不依赖真实
PostgreSQL，也不该在 buffer 满时触发一次建连。
"""

from __future__ import annotations

import pytest

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

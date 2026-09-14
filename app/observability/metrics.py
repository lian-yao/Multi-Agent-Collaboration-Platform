"""指标采集（成员 C D7-8）。

对齐事实源：

- `doc/15 AI Native多智能体协作平台.md` 模块 5「指标收集」：通过 Prometheus 导出
  任务完成率、平均执行时间、模型调用 Token 消耗、工具调用成功率；
- `doc/api.md` §5.5：C 负责**采集、去重**与 `labels.workflow_id`（可选 agent_id/model）
  关联，采样写入 `doc/data-model.md` §3 的 `metrics` 表，D 只负责读取与呈现；
  表由 B 建，因此本模块只在表已存在时写入，绝不建表。

两条导出通道：

1. **Prometheus**：进程内计数器/直方图，`render_prometheus_metrics()` 输出文本格式；
2. **`metrics` 表采样**：`MetricsCollector` 缓冲并写入数据库，供 Web 控制台按
   Workflow 过滤展示。

去重语义：同一执行身份（如 `stage:{workflow_id}:{stage}`、`tool:{call_id}`）只记一次。
Dapr 活动重放会重新执行同一阶段，去重键保证不会重复累计采样值。
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Protocol

from pydantic import BaseModel, Field
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import MetaData, Table, create_engine, inspect, make_url
from sqlalchemy.engine import Engine

from app.core.storage import get_storage_settings
from app.observability.config import (
    ObservabilitySettings,
    get_observability_settings,
)
from app.observability.context import current_labels
from app.observability.logging import get_logger, log_event

logger = get_logger("observability.metrics")

METRICS_TABLE = "metrics"

# metrics 表写入的连接超时。观测是旁路，数据库不可用时必须快速失败，
# 绝不能把流水线的终态活动挂住。注意 libpq 对**每个解析出的地址**各计时一次，
# 因此 localhost 同时解析到 ::1 与 127.0.0.1 时实际等待约为两倍。
DEFAULT_SINK_TIMEOUT_SECONDS = 1.0

# --- 指标名（写入 metrics.metric_name；Token 命名遵循 doc/api.md §5.5 交接约定）---
METRIC_INPUT_TOKENS = "input_tokens"
METRIC_OUTPUT_TOKENS = "output_tokens"
METRIC_TOTAL_TOKENS = "total_tokens"
METRIC_TOOL_CALLS = "tool_calls"
METRIC_TOOL_CALL_FAILURES = "tool_call_failures"
METRIC_TOOL_CALL_DURATION_MS = "tool_call_duration_ms"
METRIC_STAGE_RUNS = "stage_runs"
METRIC_STAGE_DURATION_MS = "stage_duration_ms"
METRIC_WORKFLOW_RUNS = "workflow_runs"
METRIC_WORKFLOW_DURATION_MS = "workflow_duration_ms"

LABEL_WORKFLOW_ID = "workflow_id"

# --- Prometheus 导出（与 metrics 表采样同名同标签，便于交叉核对）---
PROMETHEUS_REGISTRY = CollectorRegistry()

PROM_TOOL_CALLS = Counter(
    "macp_tool_calls_total",
    "工具调用次数（按工具与状态）",
    ["tool_name", "status"],
    registry=PROMETHEUS_REGISTRY,
)
PROM_TOOL_DURATION = Histogram(
    "macp_tool_call_duration_seconds",
    "工具调用耗时",
    ["tool_name"],
    registry=PROMETHEUS_REGISTRY,
)
PROM_STAGE_DURATION = Histogram(
    "macp_stage_duration_seconds",
    "流水线阶段耗时",
    ["stage", "status"],
    registry=PROMETHEUS_REGISTRY,
)
PROM_LLM_TOKENS = Counter(
    "macp_llm_tokens_total",
    "模型调用 Token 消耗（按方向）",
    ["model", "kind"],
    registry=PROMETHEUS_REGISTRY,
)
PROM_WORKFLOW_RUNS = Counter(
    "macp_workflow_runs_total",
    "Workflow 终态次数（completed/failed 之比即任务完成率）",
    ["status"],
    registry=PROMETHEUS_REGISTRY,
)
PROM_METRICS_BUFFER = Gauge(
    "macp_metrics_buffer_samples",
    "待写入 metrics 表的采样条数",
    registry=PROMETHEUS_REGISTRY,
)


class MetricSample(BaseModel):
    """一条指标采样，字段对齐 `doc/data-model.md` §3 metrics 表。"""

    metric_name: str = Field(min_length=1, max_length=100)
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MetricSink(Protocol):
    """采样落库接口；返回实际写入条数。"""

    def write(self, samples: Sequence[MetricSample]) -> int: ...


class NullMetricSink:
    """不落库（`OBS_METRICS_SINK=none` 或测试使用）。"""

    def write(self, samples: Sequence[MetricSample]) -> int:
        return 0


class PostgresMetricSink:
    """把采样插入 `metrics` 表。

    表由成员 B 建（`doc/api.md` §5.5）；表不存在时记一次日志并跳过，
    不建表、不改结构，也**不把错误吞成"零条记录"**（`doc/api.md` §5）。

    观测写入必须无条件让位于业务：连接带超时，失败一次后本进程内不再重试
    （`disabled`），把降级状态显式记进日志——否则数据库不可用时会拖慢甚至卡住
    流水线终态活动。
    """

    def __init__(
        self,
        engine_factory: Callable[[], Engine] | None = None,
        *,
        dsn: str | None = None,
        timeout_seconds: float = DEFAULT_SINK_TIMEOUT_SECONDS,
    ) -> None:
        self._engine_factory = engine_factory or (
            lambda: _sink_engine(dsn or get_storage_settings().database_url, timeout_seconds)
        )
        self._missing_logged = False
        self._disabled = False

    @property
    def disabled(self) -> bool:
        return self._disabled

    def write(self, samples: Sequence[MetricSample]) -> int:
        if not samples or self._disabled:
            return 0
        try:
            return self._write(samples)
        except Exception as exc:
            self._disabled = True
            log_event(
                logger,
                "metrics.sink_disabled",
                level=logging.WARNING,
                error=f"{type(exc).__name__}: {exc}",
                hint="本次进程内不再尝试写入 metrics 表，Prometheus 指标不受影响",
            )
            return 0

    def _write(self, samples: Sequence[MetricSample]) -> int:
        engine: Engine = self._engine_factory()
        with engine.begin() as connection:
            if not inspect(connection).has_table(METRICS_TABLE):
                if not self._missing_logged:
                    self._missing_logged = True
                    log_event(
                        logger,
                        "metrics.table_missing",
                        level=logging.WARNING,
                        table=METRICS_TABLE,
                        hint="metrics 表由成员 B 建（doc/api.md §5.5）",
                    )
                return 0
            table = Table(METRICS_TABLE, MetaData(), autoload_with=connection)
            connection.execute(
                table.insert(),
                [
                    {
                        "metric_name": sample.metric_name,
                        "value": sample.value,
                        "labels": dict(sample.labels),
                        "recorded_at": sample.recorded_at,
                    }
                    for sample in samples
                ],
            )
        return len(samples)


class MetricsCollector:
    """采样缓冲、去重与落库。"""

    def __init__(
        self,
        sink: MetricSink | None = None,
        *,
        settings: ObservabilitySettings | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._settings = settings or get_observability_settings()
        self._sink = sink if sink is not None else _default_sink(self._settings)
        self._enabled = (
            self._settings.metrics_enabled if enabled is None else enabled
        )
        self._pending: list[MetricSample] = []
        self._seen: OrderedDict[str, None] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        metric_name: str,
        value: float,
        labels: dict[str, str] | None = None,
        *,
        dedupe_key: str | None = None,
    ) -> MetricSample | None:
        """记录一条采样；命中 `dedupe_key` 时丢弃并返回 None。"""

        if not self._enabled:
            return None
        if dedupe_key is not None and not self._mark(dedupe_key):
            log_event(
                logger,
                "metrics.duplicate_dropped",
                metric_name=metric_name,
                dedupe_key=dedupe_key,
            )
            return None
        sample = MetricSample(
            metric_name=metric_name,
            value=float(value),
            labels=self._labels(labels),
        )
        self._pending.append(sample)
        PROM_METRICS_BUFFER.set(len(self._pending))
        if len(self._pending) >= self._settings.metrics_flush_size:
            self.flush()
        return sample

    def flush(self) -> int:
        """把缓冲区写入 sink；失败时保留缓冲并记日志，不抛给业务链路。"""

        if not self._pending:
            return 0
        batch = list(self._pending)
        try:
            written = self._sink.write(batch)
        except Exception as exc:
            log_event(
                logger,
                "metrics.flush_failed",
                level=logging.ERROR,
                count=len(batch),
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0
        del self._pending[: len(batch)]
        PROM_METRICS_BUFFER.set(len(self._pending))
        if written:
            log_event(logger, "metrics.flushed", count=written)
        return written

    def pending(self) -> tuple[MetricSample, ...]:
        return tuple(self._pending)

    def reset(self) -> None:
        self._pending.clear()
        self._seen.clear()
        PROM_METRICS_BUFFER.set(0)

    def _mark(self, key: str) -> bool:
        """登记去重键；返回 True 表示首次出现（应记录）。"""

        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self._settings.metrics_dedupe_limit:
            self._seen.popitem(last=False)
        return True

    @staticmethod
    def _labels(labels: dict[str, str] | None) -> dict[str, str]:
        merged = current_labels()
        for key, value in (labels or {}).items():
            if value is not None and str(value) != "":
                merged[key] = str(value)
        return merged


def render_prometheus_metrics() -> bytes:
    """输出 Prometheus 文本格式（供 scrape 端点使用）。"""

    return generate_latest(PROMETHEUS_REGISTRY)


def record_tool_call(
    tool_name: str,
    status: str,
    duration_ms: float,
    *,
    call_id: str | None = None,
    workflow_id: str | None = None,
    collector: MetricsCollector | None = None,
) -> None:
    """记录一次工具调用：成功率（status 标签）+ 耗时（平均执行时间的来源之一）。"""

    target = collector or get_metrics_collector()
    labels = {"tool_name": tool_name, "status": status}
    PROM_TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()
    PROM_TOOL_DURATION.labels(tool_name=tool_name).observe(duration_ms / 1000)
    key = f"tool:{call_id}" if call_id else None
    target.record(METRIC_TOOL_CALLS, 1, labels, dedupe_key=key)
    target.record(METRIC_TOOL_CALL_DURATION_MS, duration_ms, _with(labels, workflow_id))
    if status == "failed":
        failure_key = f"tool_failure:{call_id}" if call_id else None
        target.record(
            METRIC_TOOL_CALL_FAILURES, 1, _with(labels, workflow_id), dedupe_key=failure_key
        )


def record_stage(
    stage: str,
    role: str,
    status: str,
    duration_ms: float,
    *,
    workflow_id: str | None = None,
    tool_calls: int = 0,
    collector: MetricsCollector | None = None,
) -> None:
    """记录一个流水线阶段的执行结果与耗时。"""

    target = collector or get_metrics_collector()
    labels = {"stage": stage, "role": role, "status": status}
    PROM_STAGE_DURATION.labels(stage=stage, status=status).observe(duration_ms / 1000)
    identity = f"{workflow_id or 'unknown'}:{stage}"
    target.record(
        METRIC_STAGE_RUNS,
        1,
        _with(labels, workflow_id, tool_calls=tool_calls),
        dedupe_key=f"stage:{identity}",
    )
    target.record(
        METRIC_STAGE_DURATION_MS,
        duration_ms,
        _with(labels, workflow_id),
        dedupe_key=f"stage_duration:{identity}",
    )


def record_llm_usage(
    usage: dict[str, int],
    *,
    model: str | None = None,
    workflow_id: str | None = None,
    stage: str | None = None,
    collector: MetricsCollector | None = None,
) -> None:
    """记录一次模型调用的 Token 消耗（input/output/total，命名按 `doc/api.md` §5.5）。"""

    target = collector or get_metrics_collector()
    resolved_model = model or current_labels().get("model") or "unknown"
    identity = f"{workflow_id or 'unknown'}:{stage or 'unknown'}"
    for metric_name, kind in (
        (METRIC_INPUT_TOKENS, "input"),
        (METRIC_OUTPUT_TOKENS, "output"),
        (METRIC_TOTAL_TOKENS, "total"),
    ):
        value = usage.get(kind)
        if value is None:
            continue
        PROM_LLM_TOKENS.labels(model=resolved_model, kind=kind).inc(value)
        target.record(
            metric_name,
            value,
            _with({"model": resolved_model, "stage": stage}, workflow_id),
            dedupe_key=f"tokens:{identity}:{kind}",
        )


def record_workflow_terminal(
    status: str,
    *,
    workflow_id: str | None = None,
    duration_ms: float | None = None,
    collector: MetricsCollector | None = None,
) -> None:
    """记录一次 Workflow 终态；`completed/total` 即任务完成率。"""

    target = collector or get_metrics_collector()
    PROM_WORKFLOW_RUNS.labels(status=status).inc()
    identity = f"{workflow_id or uuid.uuid4()}:{status}"
    target.record(
        METRIC_WORKFLOW_RUNS,
        1,
        _with({"status": status}, workflow_id),
        dedupe_key=f"workflow:{identity}",
    )
    if duration_ms is not None:
        target.record(
            METRIC_WORKFLOW_DURATION_MS,
            duration_ms,
            _with({"status": status}, workflow_id),
            dedupe_key=f"workflow_duration:{identity}",
        )


def usage_from_message(message: Any) -> dict[str, int] | None:
    """从 LangChain 消息中抽取 Token 用量，缺失时返回 None。"""

    metadata = getattr(message, "usage_metadata", None)
    if not isinstance(metadata, dict):
        response_metadata = getattr(message, "response_metadata", None)
        raw = response_metadata.get("usage") if isinstance(response_metadata, dict) else None
        return _normalize_usage(raw)
    return _normalize_usage(metadata)


def flush_metrics() -> int:
    return get_metrics_collector().flush()


def _normalize_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    aliases = {
        "input": ("input_tokens", "prompt_tokens"),
        "output": ("output_tokens", "completion_tokens"),
        "total": ("total_tokens",),
    }
    usage: dict[str, int] = {}
    for kind, keys in aliases.items():
        for key in keys:
            value = raw.get(key)
            if isinstance(value, int):
                usage[kind] = value
                break
    if "total" not in usage and {"input", "output"} <= usage.keys():
        usage["total"] = usage["input"] + usage["output"]
    return usage or None


def _with(labels: dict[str, str], workflow_id: str | None, **extra: Any) -> dict[str, str]:
    merged = dict(labels)
    if workflow_id:
        merged[LABEL_WORKFLOW_ID] = workflow_id
    merged.update({key: str(value) for key, value in extra.items() if value is not None})
    return merged


def _default_sink(settings: ObservabilitySettings) -> MetricSink:
    if settings.metrics_sink == "none":
        return NullMetricSink()
    return PostgresMetricSink()


@lru_cache(maxsize=8)
def _sink_engine(dsn: str, timeout_seconds: float) -> Engine:
    """为 metrics 写入单独建引擎，并带上连接超时。

    不复用业务引擎：观测连接不该占用业务连接池，也不该继承业务侧无超时的策略。
    """

    connect_args: dict[str, Any] = {}
    backend = make_url(dsn).get_backend_name()
    if backend == "postgresql":
        # libpq 的 connect_timeout 覆盖整个建连过程（含启动包握手），单位秒。
        connect_args["connect_timeout"] = max(1, int(timeout_seconds))
    elif backend == "sqlite":
        connect_args["timeout"] = timeout_seconds
    return create_engine(dsn, connect_args=connect_args)


_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """进程级默认采集器（惰性创建）。"""

    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


def set_metrics_collector(collector: MetricsCollector | None) -> None:
    """替换默认采集器（测试与自定义部署使用）。"""

    global _collector
    _collector = collector

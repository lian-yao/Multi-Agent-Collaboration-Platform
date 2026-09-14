"""可观测性层：追踪、指标与行为日志（见 `doc/architecture.md` 模块划分）。"""

from app.observability.callbacks import ObservabilityCallbackHandler
from app.observability.config import (
    ObservabilitySettings,
    get_observability_settings,
)
from app.observability.context import (
    ObservationContext,
    current_context,
    current_labels,
    observe,
)
from app.observability.instrumentation import observed_stage
from app.observability.logging import (
    configure_logging,
    get_logger,
    log_event,
)
from app.observability.metrics import (
    METRIC_INPUT_TOKENS,
    METRIC_OUTPUT_TOKENS,
    METRIC_STAGE_DURATION_MS,
    METRIC_TOTAL_TOKENS,
    METRIC_WORKFLOW_RUNS,
    MetricSample,
    MetricsCollector,
    NullMetricSink,
    PostgresMetricSink,
    flush_metrics,
    get_metrics_collector,
    record_llm_usage,
    record_stage,
    record_tool_call,
    record_workflow_terminal,
    render_prometheus_metrics,
    set_metrics_collector,
    usage_from_message,
)
from app.observability.tracing import (
    configure_tracing,
    current_trace_id,
    get_tracer,
    is_tracing_enabled,
    record_exception,
    shutdown_tracing,
    span,
)

__all__ = [
    "METRIC_INPUT_TOKENS",
    "METRIC_OUTPUT_TOKENS",
    "METRIC_STAGE_DURATION_MS",
    "METRIC_TOTAL_TOKENS",
    "METRIC_WORKFLOW_RUNS",
    "MetricSample",
    "MetricsCollector",
    "NullMetricSink",
    "ObservationContext",
    "ObservabilityCallbackHandler",
    "ObservabilitySettings",
    "PostgresMetricSink",
    "configure_logging",
    "configure_tracing",
    "current_context",
    "current_labels",
    "current_trace_id",
    "flush_metrics",
    "get_logger",
    "get_metrics_collector",
    "get_observability_settings",
    "get_tracer",
    "is_tracing_enabled",
    "log_event",
    "observe",
    "observed_stage",
    "record_exception",
    "record_llm_usage",
    "record_stage",
    "record_tool_call",
    "record_workflow_terminal",
    "render_prometheus_metrics",
    "set_metrics_collector",
    "shutdown_tracing",
    "span",
    "usage_from_message",
]

"""OpenTelemetry 追踪接入（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 5「分布式追踪」——
为 Agent 调用链路生成 Span，集成 Jaeger 展示 Agent 推理与工具调用过程；
`doc/architecture.md` 可观测性层。

设计要点：

- 未启用（`OBS_TRACING_ENABLED=false`）或 SDK 未安装时全部退化为**空操作**，
  不改变业务行为、不产生异常；
- Span 统一由 `span()` 上下文管理器创建，属性写入失败不影响主流程；
- `configure_tracing` 幂等，重复调用只配置一次；测试可用 `force=True` 换用内存导出器。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

from app.observability.config import (
    ObservabilitySettings,
    get_observability_settings,
)
from app.observability.logging import get_logger, log_event

logger = get_logger("observability.tracing")

_provider: TracerProvider | None = None


def configure_tracing(
    settings: ObservabilitySettings | None = None,
    *,
    span_exporter: SpanExporter | None = None,
    force: bool = False,
) -> bool:
    """初始化追踪导出；返回是否真正启用了导出。

    `span_exporter` 用于测试注入内存导出器（`InMemorySpanExporter`）；
    未给出时使用 OTLP HTTP 导出到 `OBS_TRACING_ENDPOINT`（默认 Jaeger 4318）。
    """

    global _provider
    resolved = settings or get_observability_settings()
    if not resolved.tracing_enabled:
        log_event(logger, "tracing.disabled")
        return False
    if _provider is not None and not force:
        return True

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: resolved.service_name}),
        sampler=_build_sampler(resolved.tracing_sample_ratio),
    )
    exporter = span_exporter or _build_otlp_exporter(resolved)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _provider = provider
    _install_global_provider(provider)
    log_event(
        logger,
        "tracing.configured",
        service=resolved.service_name,
        exporter=type(exporter).__name__,
    )
    return True


def get_tracer(component: str = "app") -> trace.Tracer:
    """返回组件级 Tracer；未配置时返回空操作 Tracer。"""

    if _provider is not None:
        return _provider.get_tracer(f"macp.{component}")
    return trace.get_tracer(f"macp.{component}")


def is_tracing_enabled() -> bool:
    return _provider is not None


@contextmanager
def span(name: str, /, **attributes: Any) -> Iterator[trace.Span]:
    """创建一个 Span 并写入属性；属性值为 None 时跳过。"""

    tracer = get_tracer(name.split(".", 1)[0])
    with tracer.start_as_current_span(name) as current:
        _set_attributes(current, attributes)
        yield current


def record_exception(current: trace.Span, exc: BaseException) -> None:
    """把异常写入 Span 并标记为错误。"""

    try:
        current.record_exception(exc)
        current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
    except Exception:  # 追踪失败不能影响主流程
        pass


def current_trace_id() -> str | None:
    """返回当前 Span 的 trace id（16 进制），供日志与指标关联。"""

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def shutdown_tracing() -> None:
    """Flush 并关闭 provider（进程退出或测试清理时调用）。"""

    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def _build_sampler(ratio: float):
    """采样率 1.0 走恒定采样，其余按 trace id 比例采样并保持父子一致。"""

    if ratio >= 1:
        return ALWAYS_ON
    return ParentBased(TraceIdRatioBased(max(ratio, 0.0)))


def _build_otlp_exporter(settings: ObservabilitySettings) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=settings.tracing_endpoint,
        timeout=settings.tracing_export_timeout_seconds,
    )


def _install_global_provider(provider: TracerProvider) -> None:
    """设置全局 provider；已被其他组件占用时忽略（本模块自带 provider 仍生效）。"""

    try:
        trace.set_tracer_provider(provider)
    except Exception:  # 全局 provider 只能设置一次
        pass


def _set_attributes(current: trace.Span, attributes: dict[str, Any]) -> None:
    if not current.is_recording():
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            current.set_attribute(key, value)
        except Exception:  # 非法属性类型不应影响业务
            continue

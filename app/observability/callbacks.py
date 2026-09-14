"""LangChain 回调：模型调用 Span 与 Token 消耗（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 5——追踪 Agent 调用链路，
统计「模型调用Token消耗」；`doc/api.md` §5.5 的 Token 名称交接约定。

挂载点：`app/orchestration/llm.py::build_chat_model` 在构造模型时附带本处理器
（模型接入由成员 C 负责）。处理器只读取回调参数，不改变模型行为；
回调内部异常一律吞掉，观测失败不能影响业务链路。
"""

from __future__ import annotations

import time
import logging
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.observability.context import current_labels
from app.observability.logging import get_logger, log_event
from app.observability.metrics import (
    MetricsCollector,
    get_metrics_collector,
    record_llm_usage,
    usage_from_message,
)
from app.observability.tracing import get_tracer, record_exception

logger = get_logger("observability.callbacks")


class ObservabilityCallbackHandler(BaseCallbackHandler):
    """为每次模型调用开 Span，并在结束时记录 Token 用量与耗时。"""

    def __init__(self, collector: MetricsCollector | None = None) -> None:
        self._collector = collector
        self._spans: dict[uuid.UUID, Any] = {}
        self._started: dict[uuid.UUID, float] = {}

    # --- LLM 调用 ---

    def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001
        self._start(kwargs.get("run_id"), serialized, len(messages or []))

    def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: ANN001
        self._start(kwargs.get("run_id"), serialized, len(prompts or []), calls=1)

    def on_llm_end(self, response: LLMResult, **kwargs):  # noqa: ANN001
        run_id = kwargs.get("run_id")
        try:
            self._end_success(run_id, response)
        except Exception as exc:  # 观测失败不影响业务
            log_event(
                logger,
                "llm.observe_failed",
                level=logging.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

    def on_llm_error(self, error: BaseException, **kwargs):  # noqa: ANN001
        run_id = kwargs.get("run_id")
        if not isinstance(run_id, uuid.UUID):
            return
        self._started.pop(run_id, None)
        span = self._spans.pop(run_id, None)
        if span is not None:
            record_exception(span, error)
            span.end()

    # --- 内部 ---

    def _start(self, run_id: Any, serialized: Any, message_count: int, calls: int = 0) -> None:
        try:
            if not isinstance(run_id, uuid.UUID):
                return
            labels = current_labels()
            model = _model_name(serialized)
            span = get_tracer("llm").start_span("llm.chat")
            span.set_attribute("llm.model", model or "unknown")
            span.set_attribute("llm.message_count", message_count)
            for key, value in labels.items():
                span.set_attribute(f"macp.{key}", value)
            self._spans[run_id] = span
            self._started[run_id] = time.perf_counter()
            log_event(
                logger,
                "llm.start",
                model=model,
                messages=message_count,
                **({"calls": calls} if calls else {}),
            )
        except Exception:  # 观测失败不影响业务
            pass

    def _end_success(self, run_id: Any, response: LLMResult) -> None:
        labels = current_labels()
        usage = _usage_from_result(response)
        model = _model_from_result(response) or labels.get("model")
        span = self._spans.pop(run_id, None) if isinstance(run_id, uuid.UUID) else None
        started = self._started.pop(run_id, None) if isinstance(run_id, uuid.UUID) else None
        duration_ms = round((time.perf_counter() - started) * 1000, 1) if started else None

        if usage:
            record_llm_usage(
                usage,
                model=model,
                workflow_id=labels.get("workflow_id"),
                stage=labels.get("stage"),
                collector=self._collector or get_metrics_collector(),
            )
        if span is not None:
            if usage:
                for key, value in usage.items():
                    span.set_attribute(f"llm.usage.{key}", value)
            if duration_ms is not None:
                span.set_attribute("llm.duration_ms", duration_ms)
            span.end()
        log_event(
            logger,
            "llm.finish",
            model=model,
            duration_ms=duration_ms,
            input_tokens=(usage or {}).get("input"),
            output_tokens=(usage or {}).get("output"),
            total_tokens=(usage or {}).get("total"),
        )


def _model_name(serialized: Any) -> str | None:
    if not isinstance(serialized, dict):
        return None
    kwargs = serialized.get("kwargs")
    if isinstance(kwargs, dict):
        for key in ("model", "model_name"):
            value = kwargs.get(key)
            if isinstance(value, str) and value:
                return value
    name = serialized.get("name")
    return name if isinstance(name, str) else None


def _model_from_result(response: LLMResult) -> str | None:
    output = getattr(response, "llm_output", None)
    if not isinstance(output, dict):
        return None
    for key in ("model_name", "model"):
        value = output.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _usage_from_result(response: LLMResult) -> dict[str, int] | None:
    """从模型调用结果中取 Token 用量。

    `LLMResult.generations` 是「每次候选一组」的嵌套列表，而聊天模型实际回传的
    `ChatResult` 用的是**扁平**列表，两种形状都要认，否则 Token 指标会一直是空的。
    """

    for entry in getattr(response, "generations", None) or []:
        candidates = entry if isinstance(entry, (list, tuple)) else [entry]
        for generation in candidates:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage = usage_from_message(message)
            if usage:
                return usage
    return None

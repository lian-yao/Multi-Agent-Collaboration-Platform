"""LangChain 回调：模型调用 Span 与 Token 消耗（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 5——追踪 Agent 调用链路，
统计「模型调用Token消耗」；`doc/api.md` §5.5 的 Token 名称交接约定。

挂载点：`app/orchestration/llm.py::build_chat_model` 在构造模型时附带本处理器
（模型接入由成员 C 负责）。处理器只读取回调参数，不改变模型行为；
回调内部异常一律吞掉，观测失败不能影响业务链路。

模型标识来源见 `_model_name()`：LangChain 1.x 的回调 `serialized` 不再带 `kwargs`，
只能拿到集成类名（`ChatOllama`），会让 Token 指标失去按模型的归因能力
（缺口 F-03，见 ADR-016），因此改从回调的 `metadata["ls_model_name"]` 取真实模型名。
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
        # 模型名只在开始时拿得到：ChatOllama 的 LLMResult.llm_output 是 None，
        # 结束回调取不到模型名（F-03），所以开始时就按 run_id 记下来。
        self._models: dict[uuid.UUID, str] = {}

    # --- LLM 调用 ---

    def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001
        self._start(
            kwargs.get("run_id"), serialized, len(messages or []), callback_kwargs=kwargs
        )

    def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: ANN001
        self._start(
            kwargs.get("run_id"),
            serialized,
            len(prompts or []),
            calls=1,
            callback_kwargs=kwargs,
        )

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
        self._models.pop(run_id, None)
        span = self._spans.pop(run_id, None)
        if span is not None:
            record_exception(span, error)
            span.end()

    # --- 内部 ---

    def _start(
        self,
        run_id: Any,
        serialized: Any,
        message_count: int,
        calls: int = 0,
        callback_kwargs: dict[str, Any] | None = None,
    ) -> None:
        try:
            if not isinstance(run_id, uuid.UUID):
                return
            labels = current_labels()
            model = _model_name(serialized, callback_kwargs or {})
            if model:
                self._models[run_id] = model
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
        # 结果里没有模型名（ChatOllama 的 llm_output 是 None）时回落到开始时记下的名字，
        # 再退化到阶段标签；都没有才留空，不编造。
        remembered = (
            self._models.pop(run_id, None) if isinstance(run_id, uuid.UUID) else None
        )
        model = _model_from_result(response) or remembered or labels.get("model")
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


def _model_name(serialized: Any, callback_kwargs: dict[str, Any] | None = None) -> str | None:
    """取模型标识，按可靠性从高到低（F-03）。

    LangChain 1.x 起回调的 `serialized` 是 `{"type": "not_implemented", "name": "ChatOllama"}`，
    **没有 `kwargs`**，实测只能拿到集成类名，用它做「按模型归因」会失真
    （甚至把 Ollama 与 OpenAI 的 Token 记成同一个模型）。可用来源按序：
    1. `metadata["ls_model_name"]`——LangChain 为每次调用自动注入的真实模型名（实测可用）；
    2. `invocation_params["model"|"model_name"]`——部分集成把模型名放进调用参数；
    3. `serialized["kwargs"]["model"|"model_name"]`——旧版回调形状，保留兼容；
    4. `serialized["name"]`——只有集成类名时的兜底，语义上弱于前三者。
    """

    callback_kwargs = callback_kwargs or {}

    metadata = callback_kwargs.get("metadata")
    if isinstance(metadata, dict):
        for key in ("ls_model_name", "model", "model_name"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value

    invocation_params = callback_kwargs.get("invocation_params")
    if isinstance(invocation_params, dict):
        for key in ("model", "model_name"):
            value = invocation_params.get(key)
            if isinstance(value, str) and value:
                return value

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

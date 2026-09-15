"""成员 C D7-8：可观测数据输出（`doc/testing.md` §2.1 I-07 的单元级证据）。

对齐 `doc/15 AI Native多智能体协作平台.md` 模块 5 与 `doc/api.md` §5.5：

- **追踪**：阶段与工具调用产生 Span，属性带 `workflow_id`/`stage`/`role`，异常写入 Span；
- **指标**：任务完成率、阶段耗时、Token 消耗、工具成功率按 `labels.workflow_id`
  关联，可导成 Prometheus 文本；同一执行身份重放时按去重键只记一次；
- **采样落库**：`PostgresMetricSink` 只写成员 B 建好的 `metrics` 表，绝不建表，
  且数据库不可用时快速失败并降级，不拖住业务链路。

Span 断言用 `InMemorySpanExporter` 替代 OTLP 导出，因此不依赖 Jaeger；
Prometheus 断言只看文本格式的暴露结果，不启动 exporter 端口（`doc/testing.md` §1）。
"""

from __future__ import annotations

import logging
import uuid

import pytest
import sqlalchemy as sa
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.observability import (
    MetricSample,
    MetricsCollector,
    ObservationContext,
    ObservabilityCallbackHandler,
    ObservabilitySettings,
    PostgresMetricSink,
    configure_tracing,
    current_trace_id,
    is_tracing_enabled,
    observe,
    observed_stage,
    record_llm_usage,
    record_stage,
    record_tool_call,
    record_workflow_terminal,
    render_prometheus_metrics,
    shutdown_tracing,
    span,
    usage_from_message,
)

EXPECTED_PROMETHEUS_METRICS = [
    "macp_tool_calls_total",
    "macp_tool_call_duration_seconds",
    "macp_stage_duration_seconds",
    "macp_llm_tokens_total",
    "macp_workflow_runs_total",
]


class _StubMessage:
    """只带 `usage_metadata` 的消息替身，用来构造提供方给出的各种用量形状。"""

    def __init__(self, usage_metadata) -> None:
        self.usage_metadata = usage_metadata


@pytest.fixture
def span_exporter():
    """把追踪指向内存导出器；用例里调用 `shutdown_tracing()` 即完成 flush。"""

    shutdown_tracing()
    exporter = InMemorySpanExporter()
    configure_tracing(
        ObservabilitySettings(tracing_enabled=True),
        span_exporter=exporter,
        force=True,
    )
    try:
        yield exporter
    finally:
        shutdown_tracing()


# --- 追踪 -----------------------------------------------------------------


def test_span_is_exported_with_attributes(span_exporter):
    with span("stage.run", workflow_id="w1", stage="collect"):
        tracer_id = current_trace_id()

    shutdown_tracing()
    [exported] = span_exporter.get_finished_spans()

    assert exported.name == "stage.run"
    assert exported.attributes["workflow_id"] == "w1"
    assert exported.attributes["stage"] == "collect"
    assert tracer_id is not None and len(tracer_id) == 32


def test_span_skips_none_attributes(span_exporter):
    with span("tool.call", tool_name="calculator", call_id=None):
        pass

    shutdown_tracing()
    [exported] = span_exporter.get_finished_spans()

    assert exported.attributes["tool_name"] == "calculator"
    assert "call_id" not in exported.attributes


def test_span_marks_exceptions(span_exporter):
    with pytest.raises(ValueError):
        with span("stage.run", stage="collect"):
            raise ValueError("上游结果缺失")

    shutdown_tracing()
    [exported] = span_exporter.get_finished_spans()

    assert exported.status.status_code.name == "ERROR"
    assert any(event.name == "exception" for event in exported.events)


def test_tracing_disabled_is_a_noop():
    shutdown_tracing()
    enabled = configure_tracing(ObservabilitySettings(tracing_enabled=False))

    assert enabled is False
    assert is_tracing_enabled() is False
    # 没有本模块的 provider 时，开 Span 依然安全（落到全局 provider 或 no-op），
    # 不抛异常、也不影响业务；`current_trace_id()` 此时可能取到上游 provider 的 id。
    with span("stage.run", stage="collect") as current:
        assert current is not None


# --- 标签上下文 ------------------------------------------------------------


def test_observe_sets_labels_and_restores_them():
    with observe(workflow_id="w1", stage="collect", role="collector") as current:
        assert isinstance(current, ObservationContext)
        assert current.labels()["workflow_id"] == "w1"

    with observe() as outside:
        assert "workflow_id" not in outside.labels()


def test_labels_flow_into_recorded_samples(memory_metrics):
    collector, sink = memory_metrics

    with observe(workflow_id="w1", stage="collect", agent_id="a1"):
        record_tool_call("calculator", "succeeded", 12.0, call_id="c1")
    collector.flush()

    [sample] = sink.samples("tool_calls")
    assert sample.labels["workflow_id"] == "w1"
    assert sample.labels["stage"] == "collect"
    assert sample.labels["agent_id"] == "a1"
    assert sample.labels["tool_name"] == "calculator"


# --- 阶段指标 -------------------------------------------------------------


def test_observed_stage_records_success_duration_and_span(span_exporter, memory_metrics):
    collector, sink = memory_metrics

    with observed_stage(workflow_id="w1", stage="collect", role="collector"):
        pass
    collector.flush()
    shutdown_tracing()

    [runs] = sink.samples("stage_runs")
    assert runs.value == 1
    assert runs.labels["stage"] == "collect"
    assert runs.labels["role"] == "collector"
    assert runs.labels["status"] == "succeeded"
    assert runs.labels["workflow_id"] == "w1"
    [duration] = sink.samples("stage_duration_ms")
    assert duration.value >= 0
    assert duration.labels["workflow_id"] == "w1"

    [exported] = span_exporter.get_finished_spans()
    assert exported.name == "stage.run"
    assert exported.attributes["role"] == "collector"


def test_observed_stage_records_failure_and_reraises(memory_metrics):
    collector, sink = memory_metrics

    with pytest.raises(RuntimeError):
        with observed_stage(workflow_id="w1", stage="analyze", role="analyst"):
            raise RuntimeError("模型不可用")
    collector.flush()

    [runs] = sink.samples("stage_runs")
    assert runs.labels["status"] == "failed"
    assert runs.labels["stage"] == "analyze"


def test_record_stage_counts_a_tool_call_surface(memory_metrics):
    collector, sink = memory_metrics

    record_stage("report", "reporter", "succeeded", 42.0, workflow_id="w1", tool_calls=2)
    collector.flush()

    [runs] = sink.samples("stage_runs")
    assert runs.labels["tool_calls"] == "2"


# --- 去重（Dapr 活动重放） --------------------------------------------------


def test_replayed_tool_call_is_recorded_once(memory_metrics):
    collector, sink = memory_metrics

    record_tool_call("calculator", "succeeded", 5.0, call_id="stable")
    record_tool_call("calculator", "succeeded", 5.0, call_id="stable")
    collector.flush()

    assert len(sink.samples("tool_calls")) == 1


def test_failed_call_is_counted_as_a_failure(memory_metrics):
    """失败调用不能被算成成功：`tool_calls.status` 与失败计数都要能看出失败。"""

    collector, sink = memory_metrics

    record_tool_call("calculator", "failed", 5.0, call_id="fail-1")
    record_tool_call("calculator", "succeeded", 5.0, call_id="ok-1")
    collector.flush()

    statuses = sorted(s.labels["status"] for s in sink.samples("tool_calls"))
    assert statuses == ["failed", "succeeded"]
    [failures] = sink.samples("tool_call_failures")
    assert failures.labels["tool_name"] == "calculator"


def test_replayed_stage_is_recorded_once(memory_metrics):
    collector, sink = memory_metrics

    record_stage("collect", "collector", "succeeded", 10.0, workflow_id="w1")
    record_stage("collect", "collector", "succeeded", 10.0, workflow_id="w1")
    collector.flush()

    assert len(sink.samples("stage_runs")) == 1
    assert len(sink.samples("stage_duration_ms")) == 1


def test_workflow_terminal_records_status_for_completion_ratio(memory_metrics):
    collector, sink = memory_metrics

    record_workflow_terminal("completed", workflow_id="w1")
    record_workflow_terminal("failed", workflow_id="w2")
    collector.flush()

    statuses = sorted(s.labels["status"] for s in sink.samples("workflow_runs"))
    assert statuses == ["completed", "failed"]


# --- Token 消耗 -----------------------------------------------------------


def test_record_llm_usage_uses_the_api_token_names(memory_metrics):
    """`doc/api.md` §5.5 交接约定：input_tokens / output_tokens / total_tokens。"""

    collector, sink = memory_metrics

    record_llm_usage(
        {"input": 10, "output": 5, "total": 15},
        model="qwen2.5",
        workflow_id="w1",
        stage="collect",
    )
    collector.flush()

    assert sorted(sink.names()) == ["input_tokens", "output_tokens", "total_tokens"]
    [tokens] = sink.samples("input_tokens")
    assert tokens.value == 10
    assert tokens.labels["model"] == "qwen2.5"
    assert tokens.labels["workflow_id"] == "w1"


def test_usage_from_message_normalizes_provider_metadata():
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    )

    assert usage_from_message(message) == {"input": 3, "output": 4, "total": 7}


def test_usage_from_message_derives_total_when_absent():
    """部分提供方只给输入/输出两个数，total 由本模块补齐。

    这里用替身而不是 `AIMessage`：LangChain 的 `usage_metadata` 按类型定义要求
    `total_tokens` 必填，构造不出「缺 total」的消息。
    """

    assert usage_from_message(_StubMessage({"input_tokens": 3, "output_tokens": 4})) == {
        "input": 3,
        "output": 4,
        "total": 7,
    }


def test_usage_from_message_accepts_openai_style_keys():
    assert usage_from_message(
        _StubMessage({"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})
    ) == {"input": 8, "output": 2, "total": 10}


def test_usage_from_message_returns_none_without_metadata():
    assert usage_from_message(AIMessage(content="hi")) is None
    assert usage_from_message(_StubMessage(None)) is None


def test_callback_handler_records_tokens_for_a_model_call(memory_metrics):
    collector, sink = memory_metrics
    handler = ObservabilityCallbackHandler(collector=collector)
    result = ChatResult(
        generations=[
            ChatGeneration(
                message=AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                )
            )
        ],
        llm_output={"model_name": "test-model"},
    )

    handler.on_llm_end(result, run_id=uuid.uuid4())
    collector.flush()

    [tokens] = sink.samples("total_tokens")
    assert tokens.value == 10
    assert tokens.labels["model"] == "test-model"


# 实测的 ChatOllama 回调载荷（2026-09-15，langchain-core 1.6.1 / langchain-ollama）：
# `serialized` 只有集成类名、**没有 `kwargs`**，真实模型名只在回调 `metadata` 里；
# `LLMResult.llm_output` 为 None，结束回调本身取不到模型名。
CHATOLLAMA_SERIALIZED = {
    "lc": 1,
    "type": "not_implemented",
    "id": ["langchain_ollama", "chat_models", "ChatOllama"],
    "repr": "ChatOllama(model='qwen2.5-coder:7b', temperature=0.2)",
    "name": "ChatOllama",
}
CHATOLLAMA_METADATA = {
    "ls_provider": "ollama",
    "ls_model_name": "qwen2.5-coder:7b",
    "ls_model_type": "chat",
    "ls_temperature": 0.2,
    "ls_integration": "langchain_chat_model",
}
CHATOLLAMA_INVOCATION_PARAMS = {"_type": "chat-ollama", "stop": None}


def _chat_result(total: int = 7, llm_output: dict | None = None) -> ChatResult:
    return ChatResult(
        generations=[
            ChatGeneration(
                message=AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": total - 2,
                        "output_tokens": 2,
                        "total_tokens": total,
                    },
                )
            )
        ],
        llm_output=llm_output,
    )


def test_callback_handler_uses_real_model_name_not_integration_class(
    memory_metrics, caplog: pytest.LogCaptureFixture
) -> None:
    """F-03 回归：Token 要按真实模型归因，而不是记成集成类名 `ChatOllama`（ADR-015）。

    模型名只在开始回调拿得到（结果里没有），所以开始时按 `run_id` 记下、结束时用。
    只有连 `metadata` 都没有时才退化为集成类名——那是能力下限，不是模型名。
    """

    collector, sink = memory_metrics
    handler = ObservabilityCallbackHandler(collector=collector)
    run_id = uuid.uuid4()
    caplog.set_level(logging.INFO, logger="macp.observability.callbacks")

    handler.on_chat_model_start(
        CHATOLLAMA_SERIALIZED,
        [[HumanMessage(content="hi")]],
        run_id=run_id,
        metadata=CHATOLLAMA_METADATA,
        invocation_params=CHATOLLAMA_INVOCATION_PARAMS,
    )
    handler.on_llm_end(_chat_result(), run_id=run_id)
    collector.flush()

    assert "event=llm.finish" in caplog.messages[-1]
    assert "model=qwen2.5-coder:7b" in caplog.messages[-1]
    [tokens] = sink.samples("total_tokens")
    assert tokens.labels["model"] == "qwen2.5-coder:7b"


def test_callback_handler_falls_back_to_legacy_serialized_kwargs(memory_metrics) -> None:
    """旧版回调形状（`serialized["kwargs"]["model"]`）仍要能取到模型名。"""

    collector, sink = memory_metrics
    handler = ObservabilityCallbackHandler(collector=collector)
    run_id = uuid.uuid4()

    handler.on_chat_model_start(
        {"name": "ChatOpenAI", "kwargs": {"model": "gpt-legacy", "temperature": 0.2}},
        [[HumanMessage(content="hi")]],
        run_id=run_id,
    )
    handler.on_llm_end(_chat_result(total=3), run_id=run_id)
    collector.flush()

    [tokens] = sink.samples("total_tokens")
    assert tokens.labels["model"] == "gpt-legacy"


def test_callback_handler_reads_nested_llm_result_generations(memory_metrics):
    """`LLMResult` 的 generations 是嵌套列表，`ChatResult` 是扁平的，两种都要认。"""

    collector, sink = memory_metrics
    handler = ObservabilityCallbackHandler(collector=collector)
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="ok",
                        usage_metadata={
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "total_tokens": 3,
                        },
                    )
                )
            ]
        ]
    )

    handler.on_llm_end(result, run_id=uuid.uuid4())
    collector.flush()

    [tokens] = sink.samples("total_tokens")
    assert tokens.value == 3


# --- Prometheus 导出 -------------------------------------------------------


def test_prometheus_text_exposes_the_required_metrics(memory_metrics):
    collector, _ = memory_metrics
    record_tool_call("calculator", "succeeded", 8.0, call_id="c1")
    record_stage("collect", "collector", "succeeded", 12.0, workflow_id="w1")
    record_workflow_terminal("completed", workflow_id="w1")
    record_llm_usage({"input": 1, "output": 2, "total": 3}, model="qwen2.5")
    collector.flush()

    rendered = render_prometheus_metrics().decode("utf-8")

    for metric in EXPECTED_PROMETHEUS_METRICS:
        assert metric in rendered, metric
    assert 'tool_name="calculator"' in rendered
    assert 'status="completed"' in rendered
    assert "# TYPE macp_tool_calls_total counter" in rendered


# --- 采样落库 -------------------------------------------------------------


@pytest.fixture
def metrics_table_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE metrics ("
            " metric_name TEXT NOT NULL,"
            " value REAL NOT NULL,"
            " labels JSON NOT NULL,"
            " recorded_at TIMESTAMP NOT NULL)"
        )
    yield engine
    engine.dispose()


def test_postgres_sink_writes_samples_into_the_existing_table(metrics_table_engine):
    sink = PostgresMetricSink(engine_factory=lambda: metrics_table_engine)
    written = sink.write(
        [
            MetricSample(
                metric_name="tool_calls",
                value=1.0,
                labels={"workflow_id": "w1", "tool_name": "calculator"},
            )
        ]
    )

    assert written == 1

    with metrics_table_engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT metric_name, value, labels FROM metrics")
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "tool_calls"
    assert rows[0][1] == 1.0
    assert "calculator" in str(rows[0][2])


def test_postgres_sink_never_creates_the_metrics_table():
    """建表是成员 B 的职责（`doc/api.md` §5.5）：表不存在就跳过，不建表。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sink = PostgresMetricSink(engine_factory=lambda: engine)

    assert sink.write([MetricSample(metric_name="tool_calls", value=1.0)]) == 0
    assert inspect(engine).has_table("metrics") is False
    engine.dispose()


def test_postgres_sink_degrades_after_the_first_failure():
    """数据库不可用时只失败一次：不重试，也不把异常抛回业务链路。"""

    attempts: list[int] = []

    def failing_engine():
        attempts.append(1)
        raise RuntimeError("connection refused")

    sink = PostgresMetricSink(engine_factory=failing_engine)
    sample = MetricSample(metric_name="tool_calls", value=1.0)

    assert sink.write([sample]) == 0
    assert sink.disabled is True
    assert sink.write([sample]) == 0
    assert len(attempts) == 1


def test_collector_flush_keeps_samples_when_the_sink_fails():
    class ExplodingSink:
        def write(self, samples):
            raise RuntimeError("metrics 表不可写")

    collector = MetricsCollector(sink=ExplodingSink())

    collector.record("tool_calls", 1.0)
    assert collector.flush() == 0
    assert len(collector.pending()) == 1


def test_collector_auto_flushes_at_the_configured_size():
    class CountingSink:
        def __init__(self) -> None:
            self.batches: list[int] = []

        def write(self, samples) -> int:
            self.batches.append(len(samples))
            return len(samples)

    sink = CountingSink()
    collector = MetricsCollector(
        sink=sink, settings=ObservabilitySettings(metrics_flush_size=3)
    )

    for index in range(3):
        collector.record("tool_calls", float(index))

    assert sink.batches == [3]
    assert collector.pending() == ()


def test_collector_disabled_records_nothing():
    class CountingSink:
        def __init__(self) -> None:
            self.written = 0

        def write(self, samples) -> int:
            self.written += len(samples)
            return len(samples)

    sink = CountingSink()
    collector = MetricsCollector(sink=sink, enabled=False)

    assert collector.record("tool_calls", 1.0) is None
    assert collector.flush() == 0
    assert sink.written == 0

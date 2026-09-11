"""成员 A D7-8 增量：应用行为日志（结构化事件）与流水线事件覆盖。

对齐 `doc/architecture.md` 可观测性层（追踪、指标、行为日志）与
`doc/15 AI Native多智能体协作平台.md` 模块 5「行为追踪」：
Agent 的阶段执行与每次工具调用都要留下可 grep 的结构化日志，
供 `docker compose logs backend` 直接观察与事后审计。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.observability.logging import (
    LOG_FORMAT,
    configure_logging,
    get_logger,
    log_event,
)
from app.orchestration.pipeline import PipelineStage, new_pipeline_state, start
from app.orchestration.pipeline_graph import run_role_stage
from app.orchestration.tools import ToolCall, ToolCaller, ToolSpec


class _ScriptedModel(BaseChatModel):
    """按序返回固定回复的假模型，不请求外部服务。"""

    replies: list[str]
    calls: list[Any] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-observability-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


class _EchoRegistry:
    """内存版注册表：原样回显入参，用于验证工具调用日志。"""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = list(specs)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, request: ToolCall) -> Any:
        return {"echo": request.arguments}


SEARCH_SPEC = ToolSpec(name="web_search", description="检索公开资料")


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """configure_logging 会改全局 logger 级别，用例结束后恢复，避免串扰。"""

    app_logger = logging.getLogger("macp")
    level = app_logger.level
    yield
    app_logger.setLevel(level)


def test_log_event_renders_greppable_key_value_pairs(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="macp.test")

    log_event(get_logger("test"), "stage.finish", stage="collect", chars=430)

    assert "event=stage.finish" in caplog.messages[-1]
    assert "stage=collect" in caplog.messages[-1]
    assert "chars=430" in caplog.messages[-1]


def test_log_event_quotes_values_containing_spaces(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="macp.test")

    log_event(get_logger("test"), "stage.failed", error="ValueError: bad input")

    assert 'error="ValueError: bad input"' in caplog.messages[-1]


def test_log_event_omits_none_fields(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="macp.test")

    log_event(get_logger("test"), "stage.start", workflow_id=None, stage="collect")

    assert "workflow_id" not in caplog.messages[-1]


def test_get_logger_uses_macp_prefix():
    assert get_logger("orchestration.pipeline").name == "macp.orchestration.pipeline"


def test_configure_logging_sets_application_level():
    configure_logging("DEBUG")

    assert logging.getLogger("macp").level == logging.DEBUG


def test_configure_logging_passes_format_to_basic_config(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

    configure_logging("WARNING")

    assert captured["format"] == LOG_FORMAT
    assert captured["level"] == logging.WARNING


def test_configure_logging_rejects_unknown_level():
    with pytest.raises(ValueError, match="未知日志级别"):
        configure_logging("chatty")


def test_role_stage_logs_start_and_finish_events(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    model = _ScriptedModel(replies=["要点一"])

    run_role_stage(
        PipelineStage.COLLECT,
        task="收集资料",
        llm=model,
        workflow_id="wf-1",
    )

    start_event = next(m for m in caplog.messages if "event=stage.start" in m)
    finish_event = next(m for m in caplog.messages if "event=stage.finish" in m)
    assert "workflow_id=wf-1" in start_event
    assert "stage=collect" in start_event
    assert "role=collector" in start_event
    assert "tool_calls=0" in finish_event
    assert "duration_ms=" in finish_event
    assert "chars=3" in finish_event


def test_role_stage_logs_failure_before_reraising(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)

    class _BrokenModel(_ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            raise RuntimeError("ollama unreachable")

    with pytest.raises(RuntimeError, match="ollama unreachable"):
        run_role_stage(
            PipelineStage.COLLECT,
            task="收集资料",
            llm=_BrokenModel(replies=[]),
            workflow_id="wf-1",
        )

    failure = next(m for m in caplog.messages if "event=stage.failed" in m)
    assert "workflow_id=wf-1" in failure
    assert "error=" in failure
    assert "ollama unreachable" in failure


def test_tool_call_logs_status_and_duration(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    caller = ToolCaller(_EchoRegistry([SEARCH_SPEC]), scope="wf-1")

    caller.invoke("web_search", {"query": "多 Agent"})

    call_event = next(m for m in caplog.messages if "event=tool.call" in m)
    assert "tool_name=web_search" in call_event
    assert "status=succeeded" in call_event
    assert "duration_ms=" in call_event


def test_tool_call_failure_logs_error(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)

    class _FailingRegistry(_EchoRegistry):
        def call(self, request: ToolCall) -> Any:
            raise RuntimeError("mcp server down")

    caller = ToolCaller(_FailingRegistry([SEARCH_SPEC]))

    caller.invoke("web_search", {"query": "x"})

    call_event = next(m for m in caplog.messages if "event=tool.call" in m)
    assert "status=failed" in call_event
    assert "mcp server down" in call_event


def test_workflow_stage_activity_propagates_workflow_id_to_logs(
    caplog: pytest.LogCaptureFixture,
):
    from app.workflows.pipeline import advance_pipeline_stage

    caplog.set_level(logging.INFO)
    model = _ScriptedModel(replies=["收集结果"])

    advance_pipeline_stage(
        start(new_pipeline_state(task="演示任务")),
        PipelineStage.COLLECT,
        task="演示任务",
        llm=model,
        workflow_id="wf-1",
    )

    finish_event = next(m for m in caplog.messages if "event=stage.finish" in m)
    assert "workflow_id=wf-1" in finish_event

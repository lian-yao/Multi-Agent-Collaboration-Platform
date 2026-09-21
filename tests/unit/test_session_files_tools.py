"""「Agent 能自己读文件」的会话附件工具（ADR-025）。

覆盖三层：

1. **工具本身**：清单、按 id / 完整名 / 唯一子串读取、图片与解析失败各自的答复、
   找不到与名称歧义时的报错内容；
2. **注册表装饰器**：会话工具被追加到基础工具之后，命名冲突不回退到基础实现，
   未绑定会话时不改变注册表；
3. **接线**：`advance_pipeline_stage` 真的把这两个工具绑给了模型，并且模型的调用
   结果进了阶段载荷的 `tool_calls`（对齐 `doc/data-model.md` §3）。

不碰数据库：文件来源注入替身（`FileSource`），与 `doc/testing.md` §1 的约定一致。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from app.mcp.registry import SessionFileRegistry, with_session_files
from app.orchestration.pipeline import new_pipeline_state, start
from app.orchestration.tools import ToolCall, ToolSpec, session_scoped_registry
from app.tools.base import ToolExecutionError
from app.tools.config import ToolSettings
from app.tools.session_files import (
    ListSessionFilesArgs,
    ReadSessionFileArgs,
    SessionFileListTool,
    SessionFileReadTool,
    session_file_tools,
)
from app.workflows.pipeline import advance_pipeline_stage

HANDBOOK = "handbook.md"
INVOICE = "invoice-2026.pdf"


class FakeSource:
    """内存版文件来源：只实现工具需要的那两个方法。"""

    def __init__(self, rows: list[dict[str, Any]], texts: dict[str, str | None]) -> None:
        self._rows = rows
        self._texts = texts

    def list_files(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def read_file(self, reference: str) -> dict[str, Any] | None:
        from app.tools.session_files import _match_file

        matched = _match_file(self._rows, reference)
        if matched is None:
            return None
        payload = dict(matched)
        payload["text"] = self._texts.get(str(matched["id"]))
        return payload


def _row(
    attachment_id: str,
    name: str,
    kind: str = "text",
    status: str = "ready",
    size: int = 120,
) -> dict[str, Any]:
    return {
        "id": attachment_id,
        "name": name,
        "kind": kind,
        "status": status,
        "size_bytes": size,
        "has_original": True,
    }


def _source(*, long_text: bool = False) -> FakeSource:
    rows = [
        _row("a1", HANDBOOK),
        _row("a2", INVOICE, kind="document"),
        _row("a3", "diagram.png", kind="image"),
        _row("a4", "scan.pdf", kind="document", status="failed"),
    ]
    text = "退款流程见第 3 节。" * (400 if long_text else 1)
    return FakeSource(rows, {"a1": text, "a2": "发票金额 24340 元。", "a3": None, "a4": None})


def _settings(**overrides: Any) -> ToolSettings:
    base: dict[str, Any] = {
        "session_files_enabled": True,
        "session_file_max_chars": 20_000,
        "session_file_list_limit": 50,
    }
    base.update(overrides)
    return ToolSettings(**base)


# --------------------------------------------------------------------------------------
# 工具本身
# --------------------------------------------------------------------------------------


def test_list_reports_readability_by_kind_not_only_status() -> None:
    """图片的 status 也是 ready，但它读不出正文——「可读」必须按类型算。"""

    tool = SessionFileListTool(_source())
    result = tool.invoke({})

    assert result["count"] == 4
    by_name = {item["name"]: item for item in result["files"]}
    assert by_name[HANDBOOK]["readable"] is True
    assert by_name["diagram.png"]["readable"] is False
    assert by_name["scan.pdf"]["readable"] is False
    assert by_name["scan.pdf"]["has_original"] is True


def test_list_filters_by_kind() -> None:
    tool = SessionFileListTool(_source())
    result = tool.invoke({"kind": "document"})
    assert [item["name"] for item in result["files"]] == [INVOICE, "scan.pdf"]


def test_list_says_so_when_there_are_no_files() -> None:
    tool = SessionFileListTool(FakeSource([], {}))
    result = tool.invoke({})
    assert result["count"] == 0
    assert "没有附件" in result["note"]


def test_read_by_exact_name_unique_substring_and_id() -> None:
    tool = SessionFileReadTool(_source(), settings=_settings())

    for reference in (HANDBOOK, "invoice-2026", "a1"):
        result = tool.invoke({"reference": reference})
        assert result["text"], reference
        assert result["truncated"] is False


def test_read_truncates_at_the_configured_limit() -> None:
    tool = SessionFileReadTool(_source(long_text=True), settings=_settings(session_file_max_chars=100))
    result = tool.invoke({"reference": HANDBOOK})

    assert len(result["text"]) == 100
    assert result["truncated"] is True
    assert "只给出前 100 字符" in result["note"]


def test_read_image_explains_there_is_no_text() -> None:
    tool = SessionFileReadTool(_source(), settings=_settings())
    result = tool.invoke({"reference": "diagram.png"})

    assert result["text"] is None
    assert "图片" in result["note"]


def test_read_failed_parse_keeps_the_reason_and_mentions_the_original() -> None:
    tool = SessionFileReadTool(_source(), settings=_settings())
    result = tool.invoke({"reference": "scan.pdf"})

    assert result["text"] is None
    assert "原件" in result["note"]


def test_read_unknown_name_lists_what_is_available() -> None:
    tool = SessionFileReadTool(_source(), settings=_settings())

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"reference": "nope.md"})
    message = str(excinfo.value)
    assert "找不到附件" in message
    assert HANDBOOK in message and INVOICE in message


def test_ambiguous_partial_name_is_refused_instead_of_guessed() -> None:
    """`invoice` 同时命中两份时不能猜：猜错等于让模型读到另一份文件。"""

    rows = [_row("a1", "invoice-2026.pdf"), _row("a2", "invoice-2027.pdf")]
    source = FakeSource(rows, {"a1": "2026", "a2": "2027"})
    tool = SessionFileReadTool(source, settings=_settings())

    with pytest.raises(ToolExecutionError):
        tool.invoke({"reference": "invoice"})


def test_empty_reference_is_rejected_by_the_argument_model() -> None:
    tool = SessionFileReadTool(_source(), settings=_settings())
    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"reference": ""})
    assert "参数不合法" in str(excinfo.value)


def test_session_file_tools_need_a_session_and_can_be_switched_off() -> None:
    assert session_file_tools(None) == ()
    assert session_file_tools("", settings=_settings()) == ()
    assert session_file_tools("s1", settings=_settings(session_files_enabled=False)) == ()

    tools = session_file_tools("s1", settings=_settings(), source=_source())
    assert [tool.name for tool in tools] == ["list_session_files", "read_session_file"]


def test_tool_specs_expose_a_json_schema() -> None:
    for tool in session_file_tools("s1", settings=_settings(), source=_source()):
        spec = tool.spec()
        assert spec.input_schema["type"] == "object"
        assert spec.description


# --------------------------------------------------------------------------------------
# 注册表装饰器
# --------------------------------------------------------------------------------------


class _BaseRegistry:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="calculator", description="算数")]

    def call(self, request: ToolCall) -> Any:
        self.calls.append(request.tool_name)
        return {"base": request.tool_name}


def test_session_registry_appends_tools_and_dispatches_by_name() -> None:
    base = _BaseRegistry()
    tools = session_file_tools("s1", settings=_settings(), source=_source())
    registry = SessionFileRegistry(base, tools)

    names = [spec.name for spec in registry.list_tools()]
    assert names == ["calculator", "list_session_files", "read_session_file"]

    # 会话工具在本地执行，不进基础注册表。
    local = registry.call(ToolCall(call_id="1", tool_name="list_session_files", arguments={}))
    assert local["count"] == 4
    assert base.calls == []

    # 其余工具原样透传。
    remote = registry.call(ToolCall(call_id="2", tool_name="calculator", arguments={}))
    assert remote == {"base": "calculator"}
    assert base.calls == ["calculator"]


def test_with_session_files_is_a_noop_without_a_session(monkeypatch) -> None:
    base = _BaseRegistry()
    monkeypatch.setattr("app.mcp.registry.session_file_tools", lambda *a, **k: ())
    assert with_session_files(base, None) is base
    assert with_session_files(base, "s1") is base


def test_session_scoped_registry_is_a_noop_when_the_entry_point_yields_nothing(
    monkeypatch,
) -> None:
    """入口可用但（功能关闭 / 没有会话）不产出工具时，注册表必须原样返回。

    与 `default_tool_registry` 同一条接缝：接不上就退回原行为，而不是让流水线崩在
    一个可选能力上。
    """

    base = _BaseRegistry()
    monkeypatch.setattr("app.mcp.registry.session_file_tools", lambda *a, **k: ())
    assert session_scoped_registry(base, "s1") is base
    assert session_scoped_registry(None, "s1") is None


# --------------------------------------------------------------------------------------
# 接线：阶段的工具调用
# --------------------------------------------------------------------------------------


class _ToolCallingModel(BaseChatModel):
    tool_name: str
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    bound_tools: list[Any] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "session-file-test-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain 钩子签名
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        if any(isinstance(message, ToolMessage) for message in messages):
            return _result(AIMessage(content="已读到附件正文"))
        return _result(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_arguments),
                        "id": "call-1",
                    }
                ],
            )
        )

    def _stream(self, *args, **kwargs):  # pragma: no cover - 不在本用例范围
        raise NotImplementedError


def _result(message: AIMessage) -> Any:
    from langchain_core.outputs import ChatGeneration, ChatResult

    return ChatResult(generations=[ChatGeneration(message=message)])


def test_stage_binds_session_tools_and_records_the_call(monkeypatch) -> None:
    """整条接线：会话 id → 注册表挂了两个工具 → 模型调用 → 观察回填 → 记进阶段载荷。"""

    monkeypatch.setattr(
        "app.mcp.registry.session_file_tools",
        lambda session_id, **kwargs: session_file_tools(
            session_id, settings=_settings(), source=_source()
        ),
    )

    model = _ToolCallingModel(
        tool_name="read_session_file", tool_arguments={"reference": HANDBOOK}
    )
    state = start(new_pipeline_state(task="把手册里的退款流程整理成结论"))

    outcome = advance_pipeline_stage(
        state,
        "collect",
        "把手册里的退款流程整理成结论",
        llm=model,
        session_id="session-1",
    )

    assert "read_session_file" in str(model.bound_tools)
    calls = outcome["result"]["tool_calls"]
    assert [record["tool_name"] for record in calls] == ["read_session_file"]
    assert calls[0]["status"] == "succeeded"
    assert "退款流程" in str(calls[0]["output"])
    assert outcome["result"]["content"] == "已读到附件正文"


def test_stage_without_a_session_keeps_the_plain_tool_set() -> None:
    """没有会话就没有会话文件工具：不能把别的会话的附件绑给这次执行。"""

    model = _ToolCallingModel(tool_name="calculator", tool_arguments={"expression": "1+1"})
    state = start(new_pipeline_state(task="算一下"))

    advance_pipeline_stage(state, "collect", "算一下", llm=model, session_id=None)

    assert "read_session_file" not in str(model.bound_tools)


def test_read_arguments_describe_the_lookup_contract() -> None:
    schema = ReadSessionFileArgs.model_json_schema()
    assert "reference" in schema["properties"]
    assert ListSessionFilesArgs.model_json_schema()["properties"]["kind"]["default"] == "all"


def test_fake_model_path_does_not_touch_tools() -> None:
    """假模型（恢复演练）不解析任何会话工具，也不产生工具调用记录。"""

    state = start(new_pipeline_state(task="演练"))
    outcome = advance_pipeline_stage(
        state, "collect", "演练", use_fake_model=True, session_id="session-1"
    )
    assert "tool_calls" not in outcome["result"]

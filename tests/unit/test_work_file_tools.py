"""工作目录读取工具与接线（ADR-033 阶段 1）。

两个工具是**会话级**的：没绑定工作区时它们不该出现（否则模型会看到一个永远失败的
工具），绑定之后按名字分派、其余调用透传给基础注册表。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp import registry as registry_module
from app.orchestration.tools import ToolCall, ToolSpec, session_scoped_registry
from app.tools.base import ToolExecutionError
from app.tools.work_files import work_file_tools
from app.workspace.config import WorkspaceSettings
from app.workspace.errors import WorkspaceApprovalRequired, WorkspacePathError


class FakeSource:
    """工作目录数据源替身：只声明「这一层返回什么 / 抛什么」。"""

    def __init__(
        self,
        *,
        mode: str = "read_only",
        entries: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._mode = mode
        self._entries = entries or []
        self._payload = payload or {}
        self._error = error
        self.read_paths: list[str] = []
        self.writes: list[tuple[str, str, bool]] = []

    @property
    def workspace(self) -> dict[str, Any]:
        return {"id": "w-1", "path": "project"}

    @property
    def mode(self) -> str:
        return self._mode

    def list_entries(self, path: str = "", *, depth: int = 1) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {
            "path": path,
            "depth": depth,
            "entries": self._entries,
            "truncated": False,
            "limit": 500,
        }

    def read_file(self, path: str, *, max_chars: int | None = None) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        self.read_paths.append(path)
        return {"path": path, **self._payload}

    def write_file(
        self, path: str, text: str, *, overwrite: bool = False
    ) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        self.writes.append((path, text, overwrite))
        return {"path": path, "size_bytes": len(text.encode()), "created_dirs": []}

    def make_dir(self, path: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"path": path, "created_dirs": [path]}

    def move_entry(self, source: str, target: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"source": source, "target": target}


class StubRegistry:
    """最小注册表：只回答目录与调用，其余按需在用例里断言。"""

    def __init__(self, *names: str) -> None:
        self._names = names or ("calculator",)
        self.calls: list[str] = []
        self.closed = False

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(name=name, description=f"{name}（平台自带）", input_schema={})
            for name in self._names
        )

    def call(self, request: ToolCall) -> Any:
        self.calls.append(request.tool_name)
        return {"handled_by": "base", "tool": request.tool_name}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def settings() -> WorkspaceSettings:
    return WorkspaceSettings(_env_file=None, read_max_chars=20)


# --------------------------------------------------------------------------- #
# 工具本身
# --------------------------------------------------------------------------- #


def test_tools_are_absent_without_a_session_binding(settings):
    assert work_file_tools(None, settings=settings) == ()
    assert work_file_tools("s-1", settings=settings) == ()


def test_read_only_tier_has_no_write_tools(settings):
    """档位决定可用动作集合：只读会话里**不该出现**任何写工具（ADR-033 §2）。"""

    tools = work_file_tools("s-1", settings=settings, source=FakeSource())

    assert [tool.name for tool in tools] == ["list_work_files", "read_work_file"]


def test_write_tier_adds_the_non_destructive_tools(settings):
    """阶段 2 的写档位只放开三种非破坏性动作；删除在阶段 3（要审批）之前不存在。"""

    tools = work_file_tools(
        "s-1", settings=settings, source=FakeSource(mode="workspace_write")
    )

    names = [tool.name for tool in tools]
    assert names == [
        "list_work_files",
        "read_work_file",
        "write_work_file",
        "make_work_dir",
        "move_work_entry",
    ]
    assert not any("delete" in name for name in names), "删除必须等审批链路（ADR-033 §6）"


def test_list_tool_returns_entries_and_hint(settings):
    tool = work_file_tools(
        "s-1",
        settings=settings,
        source=FakeSource(entries=[{"name": "a.txt", "path": "a.txt", "kind": "file"}]),
    )[0]

    result = tool.invoke({"path": "", "depth": 1})

    assert result["workspace"] == "project"
    assert result["count"] == 1
    assert result["entries"][0]["name"] == "a.txt"
    assert "read_work_file" in result["note"]


def test_list_tool_says_the_directory_is_empty(settings):
    tool = work_file_tools("s-1", settings=settings, source=FakeSource())[0]

    assert tool.invoke({})["note"] == "这个目录是空的。"


def test_list_tool_maps_path_errors_to_non_retryable(settings):
    """越界路径是确定性失败：重试同样的参数不会有不同结果（ADR-009 修订 3）。"""

    tool = work_file_tools(
        "s-1", settings=settings, source=FakeSource(error=WorkspacePathError("越界"))
    )[0]

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"path": "../"})

    assert excinfo.value.retryable is False
    assert "越界" in str(excinfo.value)


def test_read_tool_returns_text_and_marks_truncation(settings):
    tool = work_file_tools(
        "s-1",
        settings=settings,
        source=FakeSource(
            payload={
                "size_bytes": 40,
                "total_chars": 40,
                "text": "x" * 20,
                "truncated": True,
            }
        ),
    )[1]

    result = tool.invoke({"path": "note.md"})

    assert result["text"] == "x" * 20
    assert result["truncated"] is True
    assert "超过" in result["note"]


def test_read_tool_maps_missing_file_to_non_retryable(settings):
    tool = work_file_tools(
        "s-1",
        settings=settings,
        source=FakeSource(error=WorkspacePathError("不是工作区里的文件：nope.txt")),
    )[1]

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"path": "nope.txt"})

    assert excinfo.value.retryable is False


def test_read_tool_rejects_an_empty_path(settings):
    """`path` 是必填项：工具自己校验，不把空路径交给守卫去猜。"""

    tool = work_file_tools("s-1", settings=settings, source=FakeSource())[1]

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"path": ""})

    assert excinfo.value.retryable is False


# --------------------------------------------------------------------------- #
# 阶段 2：写工具
# --------------------------------------------------------------------------- #


def test_write_tool_creates_a_file_and_reports_the_result(settings):
    source = FakeSource(mode="workspace_write")
    tools = {tool.name: tool for tool in work_file_tools("s-1", settings=settings, source=source)}

    result = tools["write_work_file"].invoke({"path": "a.txt", "content": "hello"})

    assert result["path"] == "a.txt"
    assert result["size_bytes"] == 5
    assert source.writes == [("a.txt", "hello", False)]


def test_write_tool_maps_approval_required_to_non_retryable(settings):
    """覆盖要审批：模型必须能看懂「换个文件名」，而不是反复重试同一次调用。"""

    tool = {
        t.name: t
        for t in work_file_tools(
            "s-1",
            settings=settings,
            source=FakeSource(
                mode="workspace_write",
                error=WorkspaceApprovalRequired("覆盖已有文件需要人工审批：a.txt"),
            ),
        )
    }["write_work_file"]

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"path": "a.txt", "content": "x", "overwrite": True})

    assert excinfo.value.retryable is False
    assert "人工审批" in str(excinfo.value)


def test_write_tool_rejects_an_empty_path(settings):
    tool = {
        t.name: t
        for t in work_file_tools(
            "s-1", settings=settings, source=FakeSource(mode="workspace_write")
        )
    }["write_work_file"]

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.invoke({"path": "", "content": "x"})

    assert excinfo.value.retryable is False


def test_mkdir_tool_maps_errors_and_returns_created_dirs(settings):
    tools = {
        t.name: t
        for t in work_file_tools(
            "s-1", settings=settings, source=FakeSource(mode="workspace_write")
        )
    }

    assert tools["make_work_dir"].invoke({"path": "reports/2026"})["created_dirs"] == [
        "reports/2026"
    ]

    failing = {
        t.name: t
        for t in work_file_tools(
            "s-1",
            settings=settings,
            source=FakeSource(mode="workspace_write", error=WorkspacePathError("越界")),
        )
    }["make_work_dir"]
    with pytest.raises(ToolExecutionError) as excinfo:
        failing.invoke({"path": "../x"})
    assert excinfo.value.retryable is False


def test_move_tool_returns_source_and_target(settings):
    tool = {
        t.name: t
        for t in work_file_tools(
            "s-1", settings=settings, source=FakeSource(mode="workspace_write")
        )
    }["move_work_entry"]

    result = tool.invoke({"source": "a.txt", "target": "docs/a.txt"})

    assert result["source"] == "a.txt"
    assert result["target"] == "docs/a.txt"


# --------------------------------------------------------------------------- #
# 接线：附加工具层与会话级注册表
# --------------------------------------------------------------------------- #


def test_workspace_files_layer_is_a_noop_without_a_binding(monkeypatch):
    monkeypatch.setattr("app.tools.work_files.workspace_source", lambda *a, **k: None)
    base = StubRegistry()

    assert registry_module.with_workspace_files(base, "s-1") is base


def test_workspace_files_layer_adds_tools_and_dispatches(monkeypatch):
    source = FakeSource(entries=[{"name": "a.txt", "path": "a.txt", "kind": "file"}])
    monkeypatch.setattr(
        "app.tools.work_files.workspace_source", lambda *a, **k: source
    )
    base = StubRegistry()

    registry = registry_module.with_workspace_files(base, "s-1")

    names = [spec.name for spec in registry.list_tools()]
    assert names == ["calculator", "list_work_files", "read_work_file"]
    assert registry.call(
        ToolCall(call_id="c1", tool_name="list_work_files", arguments={})
    )["count"] == 1
    # 非附加工具透传给基础注册表
    assert registry.call(
        ToolCall(call_id="c2", tool_name="calculator", arguments={})
    )["handled_by"] == "base"


def test_session_scoped_registry_chains_attachments_and_workspace(monkeypatch):
    """编排层的接缝：一次包装同时挂上会话附件工具与工作目录工具。"""

    monkeypatch.setattr(
        "app.mcp.registry.session_file_tools",
        lambda session_id, **kwargs: (),
    )
    monkeypatch.setattr(
        "app.tools.work_files.workspace_source", lambda *a, **k: FakeSource()
    )

    registry = session_scoped_registry(StubRegistry(), "s-1")
    names = [spec.name for spec in registry.list_tools()]

    assert names == ["calculator", "list_work_files", "read_work_file"]


def test_session_scoped_registry_is_unchanged_when_workspace_is_unbound(monkeypatch):
    monkeypatch.setattr(
        "app.mcp.registry.session_file_tools",
        lambda session_id, **kwargs: (),
    )
    monkeypatch.setattr("app.tools.work_files.workspace_source", lambda *a, **k: None)
    base = StubRegistry()

    assert session_scoped_registry(base, "s-1") is base

"""工作目录读取工具（ADR-033 阶段 1）。

两个工具都只在**绑定了工作区的会话**里存在，按执行临时挂进注册表——和会话附件工具
（ADR-025）同一条接缝，理由也一样：`build_tool_registry()` 是进程级缓存，塞会话相关的
工具进去会串会话。

阶段 1 只读：`list_work_files` 看结构、`read_work_file` 读正文。写/删/移动在阶段 2，
审批在阶段 3；在那之前**不给**模型任何写入口，也不给 UI 任何写开关。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.tools.base import BuiltinTool, ToolExecutionError
from app.workspace.config import WorkspaceSettings, get_workspace_settings
from app.workspace.errors import WorkspaceError
from app.workspace.service import LocalWorkspaceSource, workspace_source

MAX_PATH_CHARS = 500
MAX_DEPTH = 8


class WorkFileSource(Protocol):
    """工具读取工作区的入口；测试注入替身，生产走 `LocalWorkspaceSource`。"""

    @property
    def workspace(self) -> dict[str, Any]: ...

    def list_entries(self, path: str = "", *, depth: int = 1) -> dict[str, Any]: ...

    def read_file(self, path: str, *, max_chars: int | None = None) -> dict[str, Any]: ...


class ListWorkFilesArgs(BaseModel):
    path: str = Field(
        default="",
        max_length=MAX_PATH_CHARS,
        description="工作目录内的相对目录路径，留空表示工作目录根",
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=MAX_DEPTH,
        description="递归层数；1 只列当前层，超过 1 会带上子目录内容",
    )


class ReadWorkFileArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="工作目录内的相对文件路径，例如 reports/summary.md",
    )


class WorkFileListTool(BuiltinTool):
    name = "list_work_files"
    description = (
        "列出本次会话工作目录里的文件与子目录（名称、类型、大小）。"
        "先看结构再决定读哪个文件，不要凭猜测调用 read_work_file。"
        "只能看到工作目录内的路径，目录之外的本地文件不可见也不可访问。"
    )
    args_model = ListWorkFilesArgs

    def __init__(self, source: WorkFileSource) -> None:
        self._source = source

    def run(self, args: ListWorkFilesArgs) -> dict[str, Any]:
        try:
            listing = self._source.list_entries(args.path, depth=args.depth)
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        entries = listing.get("entries") or []
        return {
            "workspace": self._source.workspace.get("path") or "/",
            "path": listing.get("path", ""),
            "count": len(entries),
            "entries": entries,
            "truncated": bool(listing.get("truncated")),
            "note": (
                "这个目录是空的。"
                if not entries
                else "用 read_work_file 读取其中某个文件的正文。"
            ),
        }


class WorkFileReadTool(BuiltinTool):
    name = "read_work_file"
    description = (
        "读取本次会话工作目录里某个文件的正文（UTF-8 文本）。"
        "参数是工作目录内的相对路径；二进制文件读不出来，需要处理时改用 code_execution。"
    )
    args_model = ReadWorkFileArgs

    def __init__(
        self,
        source: WorkFileSource,
        *,
        settings: WorkspaceSettings | None = None,
    ) -> None:
        self._source = source
        self._settings = settings or get_workspace_settings()

    def run(self, args: ReadWorkFileArgs) -> dict[str, Any]:
        try:
            payload = self._source.read_file(args.path)
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        result = {
            "path": payload.get("path"),
            "size_bytes": int(payload.get("size_bytes") or 0),
            "total_chars": int(payload.get("total_chars") or 0),
            "text": payload.get("text"),
            "truncated": bool(payload.get("truncated")),
            "note": "",
        }
        if result["truncated"]:
            result["note"] = (
                f"正文超过 {self._settings.read_max_chars} 字符，这里只给出前面的部分。"
            )
        return result


def work_file_tools(
    session_id: str | None,
    *,
    settings: WorkspaceSettings | None = None,
    source: WorkFileSource | None = None,
) -> tuple[BuiltinTool, ...]:
    """按会话构造两个工作区读取工具；未绑定工作区或功能关闭时返回空元组。"""

    resolved = settings or get_workspace_settings()
    if not resolved.enabled or not session_id:
        return ()
    resolved_source: WorkFileSource | None = source or workspace_source(
        str(session_id), settings=resolved
    )
    if resolved_source is None:
        return ()
    return (
        WorkFileListTool(resolved_source),
        WorkFileReadTool(resolved_source, settings=resolved),
    )


__all__ = [
    "ListWorkFilesArgs",
    "LocalWorkspaceSource",
    "ReadWorkFileArgs",
    "WorkFileListTool",
    "WorkFileReadTool",
    "work_file_tools",
]

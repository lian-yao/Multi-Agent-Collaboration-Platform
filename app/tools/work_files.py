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
MAX_CONTENT_CHARS = 200_000
"""单次写入的字符上限（schema 层护栏）；真正的字节配额由 `WORKSPACE_*` 决定。"""


class WorkFileSource(Protocol):
    """工具读取工作区的入口；测试注入替身，生产走 `LocalWorkspaceSource`。"""

    @property
    def workspace(self) -> dict[str, Any]: ...

    @property
    def mode(self) -> str: ...

    def list_entries(self, path: str = "", *, depth: int = 1) -> dict[str, Any]: ...

    def read_file(self, path: str, *, max_chars: int | None = None) -> dict[str, Any]: ...

    def write_file(
        self, path: str, text: str, *, overwrite: bool = False
    ) -> dict[str, Any]: ...

    def make_dir(self, path: str) -> dict[str, Any]: ...

    def move_entry(self, source: str, target: str) -> dict[str, Any]: ...

    def delete_entry(self, path: str) -> dict[str, Any]: ...


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


class WriteWorkFileArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="工作目录内的相对文件路径，例如 reports/2026-09.md",
    )
    content: str = Field(
        max_length=MAX_CONTENT_CHARS,
        description="要写入的 UTF-8 文本；父目录不存在时会自动创建",
    )
    overwrite: bool = Field(
        default=False,
        description="是否覆盖已有文件；覆盖需要人工审批，当前会被拒绝，请改用新文件名",
    )


class MakeWorkDirArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="要创建的工作目录内相对路径，例如 reports/2026",
    )


class MoveWorkEntryArgs(BaseModel):
    source: str = Field(
        min_length=1, max_length=MAX_PATH_CHARS, description="工作目录内的源路径"
    )
    target: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="工作目录内的目标路径，目标已存在时会被拒绝（覆盖需要人工审批）",
    )


class DeleteWorkEntryArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="要删除的工作目录内相对路径（文件或目录）",
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


class WorkFileWriteTool(BuiltinTool):
    name = "write_work_file"
    description = (
        "在本次会话的工作目录里写一个 UTF-8 文本文件（父目录会自动创建）。"
        "目标已存在时默认失败；带 overwrite=true 会**提交一次人工审批**并立即返回，"
        "用户批准后重试同一调用才会真正覆盖。受目录配额限制，超限时错误里会给出"
        "当前用量与上限。"
    )
    args_model = WriteWorkFileArgs

    def __init__(self, source: WorkFileSource) -> None:
        self._source = source

    def run(self, args: WriteWorkFileArgs) -> dict[str, Any]:
        try:
            payload = self._source.write_file(
                args.path, args.content, overwrite=args.overwrite
            )
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        return {
            "path": payload.get("path"),
            "size_bytes": int(payload.get("size_bytes") or 0),
            "created_dirs": payload.get("created_dirs") or [],
            "note": "已写入。需要读回时用 read_work_file。",
        }


class WorkFileMkdirTool(BuiltinTool):
    name = "make_work_dir"
    description = (
        "在本次会话的工作目录里创建目录（可一次创建多层）。已存在时失败，"
        "这是为了让你知道目标已经在那儿，而不是静默复用。"
    )
    args_model = MakeWorkDirArgs

    def __init__(self, source: WorkFileSource) -> None:
        self._source = source

    def run(self, args: MakeWorkDirArgs) -> dict[str, Any]:
        try:
            payload = self._source.make_dir(args.path)
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        return {
            "path": payload.get("path"),
            "created_dirs": payload.get("created_dirs") or [],
        }


class WorkFileMoveTool(BuiltinTool):
    name = "move_work_entry"
    description = (
        "在本次会话的工作目录内移动或重命名文件/目录。目标已存在时拒绝执行"
        "（覆盖目标需要人工审批：会提交一次审批申请）；目录不能移动到它自己的子路径下。"
    )
    args_model = MoveWorkEntryArgs

    def __init__(self, source: WorkFileSource) -> None:
        self._source = source

    def run(self, args: MoveWorkEntryArgs) -> dict[str, Any]:
        try:
            payload = self._source.move_entry(args.source, args.target)
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        return {
            "source": payload.get("source"),
            "target": payload.get("target"),
            "note": "已移动；内容本身没有被改写。",
        }


class WorkFileDeleteTool(BuiltinTool):
    name = "delete_work_entry"
    description = (
        "删除本次会话工作目录里的文件或目录（**软删除**：先进工作区内的 .trash/）。"
        "删除必须人工审批：第一次调用只会提交审批申请，用户批准后重试同一调用才真正删除。"
    )
    args_model = DeleteWorkEntryArgs

    def __init__(self, source: WorkFileSource) -> None:
        self._source = source

    def run(self, args: DeleteWorkEntryArgs) -> dict[str, Any]:
        try:
            payload = self._source.delete_entry(args.path)
        except WorkspaceError as exc:
            raise ToolExecutionError(str(exc), retryable=False) from exc
        return {
            "path": payload.get("path"),
            "trashed_to": payload.get("trashed_to"),
            "kind": payload.get("kind"),
            "note": "已移入工作区内的 .trash/，原件仍在工作区里。",
        }


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
    tools: list[BuiltinTool] = [
        WorkFileListTool(resolved_source),
        WorkFileReadTool(resolved_source, settings=resolved),
    ]
    if resolved_source.mode == "workspace_write":
        # 只读档位下**不出现**写工具：档位决定可用动作集合，而不是"出现了再报错"
        # （ADR-033 §2，与 ADR-012 的沙箱策略同一取向）。
        tools.extend(
            (
                WorkFileWriteTool(resolved_source),
                WorkFileMkdirTool(resolved_source),
                WorkFileMoveTool(resolved_source),
                WorkFileDeleteTool(resolved_source),
            )
        )
    return tuple(tools)


__all__ = [
    "ListWorkFilesArgs",
    "LocalWorkspaceSource",
    "MakeWorkDirArgs",
    "MoveWorkEntryArgs",
    "DeleteWorkEntryArgs",
    "ReadWorkFileArgs",
    "WorkFileListTool",
    "WorkFileDeleteTool",
    "WorkFileMkdirTool",
    "WorkFileMoveTool",
    "WorkFileReadTool",
    "WorkFileWriteTool",
    "WriteWorkFileArgs",
    "work_file_tools",
]

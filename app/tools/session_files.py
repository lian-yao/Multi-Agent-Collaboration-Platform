"""会话文件读取工具（成员 C 边界，D9-10 跨边界新增，ADR-025）。

## 为什么是「读附件表」而不是「给沙箱挂卷」

「Agent 能不能自己读文件」其实是三个不同的能力，容易混成一件事：

1. **模型能看见你传的文件**——附件随消息进提示词，已经能用（ADR-021）；
2. **模型能按需再去读一份**——就是本模块：多轮之后回头看当初那份文件、或者只读其中
   一份而不必把四份都塞进上下文；
3. **沙箱里的代码能打开宿主文件**——仍然做不到，而且是**刻意**不做。

第 3 条不做，理由是叠加风险：沙箱已经拿到宿主 Docker 的控制权（ADR-023），再给它挂
一个宿主目录，等于把「能起越权容器」升级成「能起越权容器并直接读到平台数据」。
而附件表这条路既不用放宽沙箱策略（`open` / `import` 依旧禁止），也不用碰挂载，
还天然继承了 ADR-021 的限额（单文件 5 MB、单条消息 4 份）。

## 工具的作用域

这两个工具只在**一次执行**里存在（`session_id` 绑定的那次），所以它们是按会话临时
拼进注册表的，不出现在 `GET /api/v1/tools` 的静态目录里——那份目录列的是进程级
注册表（`doc/api.md` §5.3），把会话级工具算进去会让人以为它们随时可调。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.attachments import list_session_attachments, read_attachment_text
from app.attachments.spec import MAX_NAME_CHARS
from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.config import ToolSettings, get_tool_settings


class FileSource(Protocol):
    """工具读取会话文件的入口；测试注入替身，生产走附件表。"""

    def list_files(self) -> list[dict[str, Any]]: ...

    def read_file(self, reference: str) -> dict[str, Any] | None: ...


class CheckpointFileSource:
    """默认来源：本次会话在 `attachments` 表里的附件。"""

    def __init__(self, session_id: str, *, settings: ToolSettings) -> None:
        self._session_id = session_id
        self._settings = settings

    def list_files(self) -> list[dict[str, Any]]:
        rows = list_session_attachments(self._session_id)
        return rows[: self._settings.session_file_list_limit]

    def read_file(self, reference: str) -> dict[str, Any] | None:
        wanted = (reference or "").strip()
        if not wanted:
            return None
        matched = _match_file(self.list_files(), wanted)
        if matched is None:
            return None
        return read_attachment_text(str(matched["id"]))


def _match_file(rows: Sequence[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    """按 id → 完整文件名 → 唯一子串 的顺序找一份附件。

    只接受**唯一**的子串匹配：用户会说"读那份发票"，而 `invoice.pdf` 与
    `invoice-2026.pdf` 同时存在时，猜哪一个都是错的——宁可回去让模型问清楚。
    """

    for row in rows:
        if str(row.get("id")) == reference:
            return row

    lowered = reference.lower()
    for row in rows:
        if str(row.get("name", "")).lower() == lowered:
            return row

    partial = [row for row in rows if lowered in str(row.get("name", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    return None


class ListSessionFilesArgs(BaseModel):
    kind: Literal["all", "text", "document", "image"] = Field(
        default="all", description="只看某一类附件；默认全部"
    )


class ReadSessionFileArgs(BaseModel):
    reference: str = Field(
        min_length=1,
        max_length=MAX_NAME_CHARS + 64,
        description="附件的文件名（支持唯一的子串匹配）或附件 id",
    )


class SessionFileListTool(BuiltinTool):
    name = "list_session_files"
    description = (
        "列出本次会话中用户上传过的附件：文件名、类型、大小，以及正文是否可读。"
        "先看清单再决定读哪一份，不要凭猜测调用 read_session_file。"
    )
    args_model = ListSessionFilesArgs

    def __init__(self, source: FileSource) -> None:
        self._source = source

    def run(self, args: ListSessionFilesArgs) -> dict[str, Any]:
        rows = self._source.list_files()
        if args.kind != "all":
            rows = [row for row in rows if row.get("kind") == args.kind]
        files = [_file_summary(row) for row in rows]
        return {
            "count": len(files),
            "files": files,
            "note": (
                "没有附件" if not files else "用 read_session_file 读取其中任一份的正文。"
            ),
        }


class SessionFileReadTool(BuiltinTool):
    name = "read_session_file"
    description = (
        "读取本次会话中某份附件的正文，参数是文件名（支持唯一子串）或附件 id。"
        "适用于文本、PDF、Word、Excel 等；图片没有可读文本，"
        "它的内容已经随消息以图像形式交给模型了。"
    )
    args_model = ReadSessionFileArgs

    def __init__(self, source: FileSource, *, settings: ToolSettings) -> None:
        self._source = source
        self._settings = settings

    def run(self, args: ReadSessionFileArgs) -> dict[str, Any]:
        payload = self._source.read_file(args.reference)
        if payload is None:
            raise ToolExecutionError(_not_found_message(args.reference, self._source))
        return _read_result(payload, max_chars=self._settings.session_file_max_chars)


def _file_summary(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "")
    # 图片的 status 也是 ready，但它没有正文可读——「可读」要按类型算，不能只看 status。
    readable = kind != "image" and str(row.get("status") or "") == "ready"
    return {
        "name": row.get("name"),
        "kind": kind,
        "size_bytes": int(row.get("size_bytes") or 0),
        "status": row.get("status"),
        "readable": readable,
        "has_original": bool(row.get("has_original")),
    }


def _read_result(payload: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    result = {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "kind": payload.get("kind"),
        "status": payload.get("status"),
        "size_bytes": int(payload.get("size_bytes") or 0),
        "text": None,
        "truncated": False,
        "note": "",
    }
    if payload.get("kind") == "image":
        result["note"] = (
            "这是图片附件，没有文本可读；它的内容已随消息作为图像交给模型。"
            "需要图片里的信息时请直接使用你此前看到的那张图。"
        )
        return result

    text = payload.get("text")
    if not text:
        reason = payload.get("error") or "解析时未能提取出正文"
        result["note"] = (
            f"这份附件没有可用正文：{reason}。"
            "原件仍然保留，用户可以在界面上把它下载回去。"
        )
        return result

    if len(text) > max_chars:
        result["text"] = text[:max_chars]
        result["truncated"] = True
        result["note"] = f"正文超过 {max_chars} 字符，此处只给出前 {max_chars} 字符。"
    else:
        result["text"] = text
    return result


def _not_found_message(reference: str, source: FileSource) -> str:
    names = [str(row.get("name")) for row in source.list_files()]
    if not names:
        return f"本次会话没有附件，因此读不到「{reference}」。"
    return (
        f"找不到附件「{reference}」：本次会话里的附件是 "
        + "、".join(names)
        + "。名称不唯一时请给出更完整的文件名。"
    )


def session_file_tools(
    session_id: str | None,
    *,
    settings: ToolSettings | None = None,
    source: FileSource | None = None,
) -> tuple[BuiltinTool, ...]:
    """按会话构造两个文件工具；未绑定会话或已关闭时返回空元组。"""

    resolved = settings or get_tool_settings()
    if not resolved.session_files_enabled or not session_id:
        return ()
    files = source or CheckpointFileSource(str(session_id), settings=resolved)
    return (
        SessionFileListTool(files),
        SessionFileReadTool(files, settings=resolved),
    )


__all__ = [
    "CheckpointFileSource",
    "FileSource",
    "ListSessionFilesArgs",
    "ReadSessionFileArgs",
    "SessionFileListTool",
    "SessionFileReadTool",
    "session_file_tools",
]

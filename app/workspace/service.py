"""工作区服务：登记、目录树、用量与只读读取（ADR-033 阶段 1）。

阶段 1 只开放 `read_only` 档位：写/删/移动在阶段 2 落地，审批链路在阶段 3。
接口面因此**不提供**暂不可用的档位——`mode=workspace_write` 返回明确的
`WORKSPACE_MODE_UNAVAILABLE`，而不是存下来却什么都不生效（"假开关"）。

层级：路径规则在 `app/workspace/paths.py`，表访问在 `app/core/checkpoint.py`，
本模块只做业务编排与视图组装；API 层负责 HTTP 语义与会话存在性校验。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.exc import SQLAlchemyError

from app.core import checkpoint
from app.observability.logging import get_logger, log_event
from app.workspace.config import WorkspaceSettings, get_workspace_settings
from app.workspace.errors import (
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceModeUnavailable,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceRootUnavailable,
)
from app.workspace.paths import (
    normalize_relative,
    relative_to_root,
    resolve_in_workspace,
    resolve_root,
)

logger = get_logger("workspace")

DEFAULT_SESSION_DIR = "sessions"
"""每个会话默认绑定的子目录：`sessions/<session_id>/`（ADR-033 §5）。"""

ALL_MODES = ("read_only", "workspace_write")
"""数据模型允许的档位（`doc/data-model.md` §3.3）。"""

AVAILABLE_MODES = ("read_only",)
"""阶段 1 真正生效的档位。`workspace_write` 在阶段 2 开放。"""


def _resolved_settings(settings: WorkspaceSettings | None) -> WorkspaceSettings:
    return settings or get_workspace_settings()


def _ensure_enabled(settings: WorkspaceSettings) -> None:
    if not settings.enabled:
        raise WorkspaceDisabled("工作区功能已关闭（WORKSPACE_ENABLED=false）")


def default_quota(settings: WorkspaceSettings) -> dict[str, int]:
    return {
        "max_file_bytes": settings.max_file_bytes,
        "max_total_bytes": settings.max_total_bytes,
        "max_entries": settings.max_entries,
    }


def _validate_mode(mode: str | None) -> str:
    resolved = (mode or "read_only").strip() or "read_only"
    if resolved not in ALL_MODES:
        raise WorkspaceError(f"mode 取值无效：{resolved}（可选 {' / '.join(ALL_MODES)}）")
    if resolved not in AVAILABLE_MODES:
        raise WorkspaceModeUnavailable(
            "写档位（workspace_write）还未开放，当前只提供 read_only；"
            "阶段 2 会连同写/删/移动工具一起上线（ADR-033 §2）"
        )
    return resolved


def _relative_path(value: str | None) -> str:
    """`None` / 空 / `.` 表示工作区根本身；其余按相对路径规范化。"""

    normalized = normalize_relative(value)
    text = normalized.as_posix()
    return "" if text == "." else text


# --------------------------------------------------------------------------- #
# 登记与查询
# --------------------------------------------------------------------------- #


def create_workspace(
    *,
    session_id: str | None = None,
    path: str | None = None,
    mode: str | None = "read_only",
    name: str | None = None,
    actor: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """登记一个工作区并返回视图（`doc/api.md` §5.19）。

    目录不存在时由**平台**创建（`sessions/<id>/` 首次登记必然不存在）——这不是 Agent
    行为，因此不受 `read_only` 档位限制。宿主绝对路径在这里是不可达的：`path` 只能是
    相对路径，越界与符号链接逃逸由 `resolve_in_workspace()` 拒绝。
    """

    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    root = resolve_root(resolved_settings.root)
    resolved_mode = _validate_mode(mode)

    if path is None or not str(path).strip():
        relative = f"{DEFAULT_SESSION_DIR}/{session_id}" if session_id else ""
    else:
        relative = _relative_path(path)

    target = resolve_in_workspace(root, relative)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspacePathError(f"无法创建工作区目录：{relative or '/'}（{exc}）") from exc

    if checkpoint.find_workspace_by_path(relative) is not None:
        raise WorkspaceExistsError(f"该路径已登记：{relative or '/'}")

    row = checkpoint.create_workspace(
        workspace_id=uuid.uuid4(),
        session_id=session_id,
        path=relative,
        mode=resolved_mode,
        name=name,
        quota=default_quota(resolved_settings),
        created_by=actor,
    )
    log_event(
        logger,
        "workspace.created",
        workspace_id=row["id"],
        session_id=session_id,
        path=relative,
        mode=resolved_mode,
        actor=actor,
    )
    return _view(row, settings=resolved_settings)


def list_workspaces(
    *,
    session_id: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    rows = checkpoint.list_workspaces(session_id=session_id)
    items = [_view(row, settings=resolved_settings, with_usage=False) for row in rows]
    return {"items": items, "total": len(items)}


def get_workspace(
    workspace_id: str, *, settings: WorkspaceSettings | None = None
) -> dict[str, Any]:
    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    row = checkpoint.get_workspace(workspace_id)
    if row is None:
        raise WorkspaceNotFoundError(workspace_id)
    return _view(row, settings=resolved_settings)


def delete_workspace(workspace_id: str, *, actor: str | None = None) -> None:
    """解除登记。**不删宿主文件**——工作区里的东西是用户的（ADR-033）。"""

    if not checkpoint.delete_workspace(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    log_event(logger, "workspace.deleted", workspace_id=workspace_id, actor=actor)


def _view(
    row: dict[str, Any],
    *,
    settings: WorkspaceSettings,
    with_usage: bool = True,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": row["id"],
        "session_id": row.get("session_id"),
        "path": row["path"],
        "mode": row["mode"],
        "name": row.get("name"),
        "quota": {**default_quota(settings), **(row.get("quota") or {})},
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
    }
    if with_usage:
        try:
            view["usage"] = scan_usage(
                resolve_in_workspace(settings.root, row["path"]), settings=settings
            )
        except (WorkspaceRootUnavailable, WorkspacePathError) as exc:
            # 根被挪走 / 目录被删：视图仍要能读，用量标记为不可用而不是 500。
            view["usage"] = {"available": False, "reason": str(exc)}
    return view


def scan_usage(path: Path, *, settings: WorkspaceSettings) -> dict[str, Any]:
    """统计工作区用量；条目数超过 `scan_limit` 时截断并标记。"""

    total_bytes = 0
    entries = 0
    truncated = False
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for item in iterator:
                if item.name == settings.delete_trash_dir:
                    continue
                if entries >= settings.scan_limit:
                    truncated = True
                    stack.clear()
                    break
                entries += 1
                try:
                    if item.is_dir(follow_symlinks=False):
                        stack.append(Path(item.path))
                    elif item.is_file(follow_symlinks=False):
                        total_bytes += item.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return {
        "available": True,
        "total_bytes": total_bytes,
        "entries": entries,
        "truncated": truncated,
        "scan_limit": settings.scan_limit,
    }


# --------------------------------------------------------------------------- #
# 目录树
# --------------------------------------------------------------------------- #


def workspace_tree(
    workspace_id: str,
    *,
    path: str = "",
    depth: int = 1,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """列出工作区内的目录树（`doc/api.md` §5.19）。"""

    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    row = checkpoint.get_workspace(workspace_id)
    if row is None:
        raise WorkspaceNotFoundError(workspace_id)

    base = resolve_in_workspace(
        resolved_settings.root, row["path"], expect="dir"
    )
    # 以工作区为根再守一次：`path` 不能借符号链接跑回工作区根之外。
    target = resolve_in_workspace(base, path)
    if not target.is_dir():
        raise WorkspacePathError(f"不是目录：{path or '/'}")

    resolved_depth = max(1, min(int(depth or 1), resolved_settings.tree_max_depth))
    budget = {"left": resolved_settings.tree_max_entries, "truncated": False}
    entries = _collect_entries(
        base, target, depth=resolved_depth, budget=budget, trash=resolved_settings.delete_trash_dir
    )
    return {
        "workspace_id": row["id"],
        "path": relative_to_root(base, target),
        "depth": resolved_depth,
        "entries": entries,
        "truncated": budget["truncated"],
        "limit": resolved_settings.tree_max_entries,
    }


def _collect_entries(
    base: Path,
    directory: Path,
    *,
    depth: int,
    budget: dict[str, Any],
    trash: str,
) -> list[dict[str, Any]]:
    if budget["left"] <= 0:
        budget["truncated"] = True
        return []
    try:
        children = sorted(
            (item for item in os.scandir(directory) if item.name != trash),
            key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower()),
        )
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for item in children:
        if budget["left"] <= 0:
            budget["truncated"] = True
            break
        budget["left"] -= 1
        entry = _entry_view(base, item)
        if depth > 1 and entry["kind"] == "dir":
            entry["children"] = _collect_entries(
                base, Path(item.path), depth=depth - 1, budget=budget, trash=trash
            )
        entries.append(entry)
    return entries


def _entry_view(base: Path, item: os.DirEntry[str]) -> dict[str, Any]:
    path = Path(item.path)
    try:
        stat = item.stat(follow_symlinks=False)
        size_bytes = int(stat.st_size) if item.is_file(follow_symlinks=False) else None
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size_bytes = None
        modified_at = None

    if item.is_symlink():
        # 指向工作区之外的符号链接只标记、不跟随：它们不是可读内容，
        # 也不该让目录树把根外的路径结构暴露出去（ADR-033 §4）。
        try:
            inside = Path(os.path.realpath(path)).is_relative_to(base)
        except OSError:
            inside = False
        return {
            "name": item.name,
            "path": _lexical_relative(base, path),
            "kind": "symlink",
            "outside": not inside,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
        }

    kind = "dir" if item.is_dir(follow_symlinks=False) else ("file" if item.is_file(follow_symlinks=False) else "other")
    return {
        "name": item.name,
        "path": _lexical_relative(base, path),
        "kind": kind,
        "outside": False,
        "size_bytes": size_bytes,
        "modified_at": modified_at,
    }


def _lexical_relative(base: Path, path: Path) -> str:
    """按**字面**路径算相对位置，不解析符号链接。

    目录树里必须这么做：`relative_to_root()` 会先 `resolve()`，而指向工作区之外的符号
    链接一旦被跟随，`relative_to()` 就会抛 `ValueError`——那是"列目录时被一个链接炸掉"，
    正是要避免的失败形态。条目本来就是 `scandir` 从 `base` 下一层拿到的，字面关系成立。
    """

    try:
        return path.relative_to(base).as_posix()
    except ValueError:  # pragma: no cover - 正常路径不会走到这里
        return path.name


# --------------------------------------------------------------------------- #
# 只读文件源（工具用）
# --------------------------------------------------------------------------- #


class LocalWorkspaceSource:
    """按会话绑定的工作区构造的只读文件源（`app/tools/work_files.py` 消费）。"""

    def __init__(
        self,
        row: dict[str, Any],
        *,
        settings: WorkspaceSettings | None = None,
    ) -> None:
        self._row = row
        self._settings = _resolved_settings(settings)

    @property
    def workspace(self) -> dict[str, Any]:
        return dict(self._row)

    def root(self) -> Path:
        return resolve_in_workspace(self._settings.root, self._row["path"], expect="dir")

    def list_entries(self, path: str = "", *, depth: int = 1) -> dict[str, Any]:
        base = self.root()
        target = resolve_in_workspace(base, path)
        if not target.is_dir():
            raise WorkspacePathError(f"不是目录：{path or '/'}")
        resolved_depth = max(1, min(int(depth or 1), self._settings.tree_max_depth))
        budget: dict[str, Any] = {"left": self._settings.tree_max_entries, "truncated": False}
        entries = _collect_entries(
            base, target, depth=resolved_depth, budget=budget, trash=self._settings.delete_trash_dir
        )
        return {
            "path": relative_to_root(base, target),
            "depth": resolved_depth,
            "entries": entries,
            "truncated": budget["truncated"],
            "limit": self._settings.tree_max_entries,
        }

    def read_file(self, path: str, *, max_chars: int | None = None) -> dict[str, Any]:
        base = self.root()
        target = resolve_in_workspace(base, path, expect="file")
        size_bytes = target.stat().st_size
        if size_bytes > self._settings.max_file_bytes:
            raise WorkspaceError(
                f"文件超过读取上限（{size_bytes} > {self._settings.max_file_bytes} 字节）："
                f"{path}。需要处理大文件请分片或改用 code_execution。"
            )
        try:
            with _open_readonly(target) as handle:
                raw = handle.read(self._settings.max_file_bytes + 1)
        except OSError as exc:
            raise WorkspaceError(f"读取失败：{path}（{exc}）") from exc
        if len(raw) > self._settings.max_file_bytes:
            raise WorkspaceError(f"文件超过读取上限：{path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                f"不是 UTF-8 文本（{exc}）：{path}。二进制文件请改用 code_execution 处理。"
            ) from exc

        limit = max_chars if max_chars is not None else self._settings.read_max_chars
        limit = max(1, min(int(limit), self._settings.read_max_chars))
        truncated = len(text) > limit
        return {
            "path": relative_to_root(base, target),
            "size_bytes": size_bytes,
            "total_chars": len(text),
            "text": text[:limit] if truncated else text,
            "truncated": truncated,
        }


def _open_readonly(path: Path) -> Any:
    """打开文件；POSIX 上用 `O_NOFOLLOW` 拒绝最后一段是符号链接的情况。

    守卫已经把路径解析到工作区内，这里再挡一次"校验通过之后才出现的符号链接"——
    也就是 TOCTOU 窗口。Windows 没有 `O_NOFOLLOW`，退化为普通只读打开。
    """

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        try:
            return os.fdopen(os.open(path, flags | nofollow), "rb")
        except OSError:
            raise
    return open(path, "rb")


def workspace_source(
    session_id: str | None,
    *,
    settings: WorkspaceSettings | None = None,
) -> LocalWorkspaceSource | None:
    """会话绑定的工作区文件源；没有绑定 / 功能关闭 / 根不可用时返回 `None`。

    与 ADR-026 读取注册条目同一取向：**读不到配置=没有额外工具**，不把一次存储或挂载
    故障升级成流水线失败。
    """

    resolved_settings = _resolved_settings(settings)
    if not resolved_settings.enabled or not session_id:
        return None
    try:
        rows = checkpoint.list_workspaces(session_id=session_id)
    except (SQLAlchemyError, OSError, ValueError) as exc:
        # 会话 id 不是合法 UUID、存储不可达……都归到「没有额外工具」：
        # 这条路径每次执行都会走（`session_scoped_registry`），不能让它把阶段带崩
        # （与 ADR-026 读取注册条目失败时的取向一致）。
        log_event(
            logger,
            "workspace.binding_unavailable",
            session_id=session_id,
            reason=f"{type(exc).__name__}: {exc}",
        )
        return None
    if not rows:
        return None
    row = rows[0]
    try:
        resolve_in_workspace(resolved_settings.root, row["path"], expect="dir")
    except (WorkspaceRootUnavailable, WorkspacePathError) as exc:
        log_event(
            logger,
            "workspace.unavailable",
            workspace_id=row["id"],
            session_id=session_id,
            reason=str(exc),
        )
        return None
    return LocalWorkspaceSource(row, settings=resolved_settings)


def iter_workspace_entries(source: LocalWorkspaceSource, path: str = "") -> Iterator[dict[str, Any]]:
    """便捷迭代器（测试与调试用），把目录树拉平。"""

    listing = source.list_entries(path, depth=1)
    for entry in listing["entries"]:
        yield entry


__all__ = [
    "ALL_MODES",
    "AVAILABLE_MODES",
    "DEFAULT_SESSION_DIR",
    "LocalWorkspaceSource",
    "create_workspace",
    "default_quota",
    "delete_workspace",
    "get_workspace",
    "iter_workspace_entries",
    "list_workspaces",
    "scan_usage",
    "workspace_source",
    "workspace_tree",
]

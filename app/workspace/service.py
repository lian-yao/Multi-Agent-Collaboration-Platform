"""工作区服务：登记、目录树、用量与只读读取（ADR-033 阶段 1）。

阶段 1 只开放 `read_only` 档位：写/删/移动在阶段 2 落地，审批链路在阶段 3。
接口面因此**不提供**暂不可用的档位——`mode=workspace_write` 返回明确的
`WORKSPACE_MODE_UNAVAILABLE`，而不是存下来却什么都不生效（"假开关"）。

层级：路径规则在 `app/workspace/paths.py`，表访问在 `app/core/checkpoint.py`，
本模块只做业务编排与视图组装；API 层负责 HTTP 语义与会话存在性校验。
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.exc import SQLAlchemyError

from app.core import checkpoint
from app.observability.logging import get_logger, log_event
from app.workspace import approvals
from app.workspace.config import WorkspaceSettings, get_workspace_settings
from app.workspace.errors import (
    WorkspaceApprovalRequired,
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
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

AVAILABLE_MODES = ("read_only", "workspace_write")
"""当前真正生效的档位。

阶段 2 起 `workspace_write` 可用，但只放开**非破坏性**写操作（新建 / 写入 / 建目录 /
移动）；覆盖与删除按 ADR-033 §6 要人工审批，与审批链路一起在阶段 3 落地。
"""


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


def update_workspace(
    workspace_id: str,
    *,
    mode: str | None = None,
    name: str | None = None,
    actor: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """调整档位或展示名（`doc/api.md` §5.19）。

    这是**人的动作**：ADR-033 §3 明确规定 Agent 没有提权通道。所以提档只出现在 API /
    Web UI 上，永远不是一个工具——否则提示注入就能通过"申请提权"的卡片说服人放权。
    """

    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    row = checkpoint.get_workspace(workspace_id)
    if row is None:
        raise WorkspaceNotFoundError(workspace_id)

    fields: dict[str, Any] = {}
    if mode is not None:
        fields["mode"] = _validate_mode(mode)
    if name is not None:
        fields["name"] = name
    if not fields:
        return _view(row, settings=resolved_settings)

    updated = checkpoint.update_workspace(workspace_id, updated_by=actor, **fields)
    if updated is None:
        raise WorkspaceNotFoundError(workspace_id)
    log_event(
        logger,
        "workspace.updated",
        workspace_id=workspace_id,
        fields=",".join(sorted(fields)),
        actor=actor,
    )
    return _view(updated, settings=resolved_settings)


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


def import_files(
    workspace_id: str,
    files: Sequence[dict[str, Any]],
    *,
    overwrite: bool = False,
    actor: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """把一批文件**导入**工作区（浏览器「选择文件夹」按钮的服务端一侧）。

    与附件的区别值得写清楚：附件是"把内容送进模型上下文"，这里是"把内容存进工作区目录"。
    浏览器**不会**把本地路径交给后端（`<input type="file" webkitdirectory>` 只给相对路径 +
    内容），所以这条路的语义是**导入一份副本**，而不是"让 Agent 直接操作你本机那个文件夹"。
    真正的宿主目录直连见 `scripts/pick_work_dir.ps1` 与 `doc/api.md` §7.1。

    写入是**用户动作**，不受 `read_only` 档位限制（与登记时创建目录同理）：档位约束的是
    Agent，不是使用者。路径仍然逐个过守卫，越界一律拒绝。
    """

    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    row = checkpoint.get_workspace(workspace_id)
    if row is None:
        raise WorkspaceNotFoundError(workspace_id)
    if not files:
        raise WorkspaceError("没有要导入的文件")
    if len(files) > resolved_settings.import_max_files:
        raise WorkspaceError(
            f"单次最多导入 {resolved_settings.import_max_files} 个文件（本次 {len(files)}）；"
            "浏览器会分片上传，请重试"
        )

    base = resolve_in_workspace(resolved_settings.root, row["path"], expect="dir")

    # 第一遍：**先算清楚会写什么**（路径合法、不撞目录、已存在且不覆盖的跳过），
    # 这样配额能在动盘之前一次判掉——导入是批量操作，写到一半才发现超配额最难收拾。
    planned: list[tuple[str, bytes, Path]] = []
    items: list[dict[str, Any]] = []
    for entry in files:
        relative = _import_path(entry)
        raw = _import_bytes(entry, resolved_settings)
        target = resolve_in_workspace(base, relative)
        if target.is_dir():
            items.append({"path": relative, "status": "failed", "reason": "同名目录已存在"})
            continue
        if target.exists() and not overwrite:
            items.append(
                {"path": relative, "status": "skipped", "reason": "已存在（覆盖需要 overwrite=true）"}
            )
            continue
        planned.append((relative, raw, target))

    extra_bytes = sum(len(raw) for _rel, raw, target in planned if not target.exists())
    extra_entries = sum(
        1 + len(_missing_parents(base, target.parent)) for _rel, _raw, target in planned
    )
    if planned:
        _ensure_quota(
            base,
            extra_bytes=extra_bytes,
            extra_entries=extra_entries,
            path=f"导入 {len(planned)} 个文件",
            settings=resolved_settings,
        )

    # 第二遍：真正写盘。文本按 UTF-8 写、二进制按字节写；都走独占创建（覆盖时才用替换）。
    imported_bytes = 0
    for relative, raw, target in planned:
        created_dirs = _missing_parents(base, target.parent)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            replacing = target.exists()
            if _looks_like_text(raw):
                with open(
                    target, "w" if replacing else "x", encoding="utf-8", newline=""
                ) as handle:
                    handle.write(raw.decode("utf-8"))
            else:
                with open(target, "wb" if replacing else "xb") as handle:
                    handle.write(raw)
        except OSError as exc:
            items.append({"path": relative, "status": "failed", "reason": f"写入失败：{exc}"})
            continue
        imported_bytes += len(raw)
        log_event(
            logger,
            "workspace.import",
            workspace_id=workspace_id,
            path=relative,
            bytes=len(raw),
            actor=actor,
        )
        items.append(
            {
                "path": relative,
                "status": "imported",
                "size_bytes": len(raw),
                "created_dirs": [relative_to_root(base, item) for item in created_dirs],
            }
        )

    imported = sum(1 for item in items if item["status"] == "imported")
    skipped = sum(1 for item in items if item["status"] == "skipped")
    failed = sum(1 for item in items if item["status"] == "failed")
    view = _view(checkpoint.get_workspace(workspace_id) or row, settings=resolved_settings)
    return {
        "workspace_id": workspace_id,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "imported_bytes": imported_bytes,
        "items": items,
        "usage": view.get("usage"),
    }


def _import_path(entry: dict[str, Any]) -> str:
    value = str(entry.get("path") or "").replace("\\", "/").strip().lstrip("/")
    if not value:
        raise WorkspaceError("导入项缺少 path")
    return value


def _looks_like_text(raw: bytes) -> bool:
    """能不能当文本写？判据只有一条：**能按 UTF-8 解码**。

    导入是存盘而不是解析，所以不做更多嗅探；二进制内容（图片、压缩包）按字节写即可。
    """

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _import_bytes(entry: dict[str, Any], settings: WorkspaceSettings) -> bytes:
    encoded = entry.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise WorkspaceError(f"导入项缺少 content_base64：{entry.get('path')}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkspaceError(f"content_base64 不是合法 base64：{entry.get('path')}") from exc
    if len(raw) > settings.import_max_file_bytes:
        raise WorkspaceError(
            f"文件超过单文件导入上限（{len(raw)} > {settings.import_max_file_bytes} 字节）："
            f"{entry.get('path')}"
        )
    return raw


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
        "updated_by": row.get("updated_by"),
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


def root_tree(
    *,
    path: str = "",
    depth: int = 1,
    settings: WorkspaceSettings | None = None,
) -> dict[str, Any]:
    """列出**工作区根**下的目录树（`doc/api.md` §5.19）。

    与 `workspace_tree()` 的区别是**不需要已登记的工作区**：用户「选文件夹位置」这件事
    发生在登记之前，而 `{id}/tree` 要求先有一条登记——用它来选位置是循环依赖。

    只读：不建目录、不写库。`path` 照样过 `resolve_in_workspace()` 守卫，越界（`..`、
    绝对路径、指向根外的符号链接）一律 `WorkspacePathError`。
    """

    resolved_settings = _resolved_settings(settings)
    _ensure_enabled(resolved_settings)
    root = resolve_root(resolved_settings.root)
    target = resolve_in_workspace(root, path)
    if not target.is_dir():
        raise WorkspacePathError(f"不是目录：{path or '/'}")

    resolved_depth = max(1, min(int(depth or 1), resolved_settings.tree_max_depth))
    budget = {"left": resolved_settings.tree_max_entries, "truncated": False}
    entries = _collect_entries(
        root, target, depth=resolved_depth, budget=budget, trash=resolved_settings.delete_trash_dir
    )
    return {
        "path": relative_to_root(root, target),
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

    @property
    def mode(self) -> str:
        return str(self._row.get("mode") or "read_only")

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

    # -- 写（阶段 2：只放开非破坏性动作） ---------------------------------- #

    def _require_write_mode(self) -> None:
        if self.mode != "workspace_write":
            raise WorkspaceError(
                "当前是只读档位（read_only），不能写入；"
                "需要写权限请由使用者把工作区提档到 workspace_write（ADR-033 §3）"
            )

    def write_file(
        self, path: str, text: str, *, overwrite: bool = False
    ) -> dict[str, Any]:
        """写入文件；覆盖已有文件需要一条**已批准**的审批（ADR-033 §6）。"""

        self._require_write_mode()
        base = self.root()
        target = resolve_in_workspace(base, path)
        if target.is_dir():
            raise WorkspacePathError(f"目标是目录，不能写文件：{path}")
        relative = relative_to_root(base, target)
        replacing = target.exists()
        if replacing:
            if not overwrite:
                raise WorkspaceExistsError(
                    f"文件已存在：{relative}；覆盖需要人工审批，请改用新的文件名，"
                    "或带 overwrite=true 重新调用以提交审批"
                )
            self._require_approval(
                kind="overwrite",
                target=relative,
                reason="覆盖已有文件",
                payload={"size_bytes": target.stat().st_size},
            )

        payload = text.encode("utf-8")
        if len(payload) > self._settings.max_file_bytes:
            raise WorkspaceQuotaExceeded(
                f"内容超过单文件上限（{len(payload)} > {self._settings.max_file_bytes} 字节）：{relative}"
            )
        created_dirs = _missing_parents(base, target.parent)
        # 覆盖时只有"超出的部分"占新空间；按全量算会把配额误判成超限。
        existing_bytes = target.stat().st_size if replacing else 0
        self._ensure_quota(
            extra_bytes=max(0, len(payload) - existing_bytes),
            extra_entries=1 + len(created_dirs),
            path=relative,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 新建用 `x`（独占创建：并发下第二个写者拿到 FileExistsError，而不是静默覆盖）；
            # 覆盖走 `w`，但前面已经拿过审批。`newline=""` 关掉平台换行转换，
            # 落盘字节与入参一致。
            with open(target, "w" if replacing else "x", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except FileExistsError as exc:
            raise WorkspaceExistsError(
                f"文件已存在：{relative}（覆盖需要人工审批）"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(f"写入失败：{relative}（{exc}）") from exc
        log_event(
            logger,
            "workspace.overwrite" if replacing else "workspace.write",
            workspace_id=self._row["id"],
            path=relative,
            bytes=len(payload),
        )
        return {
            "path": relative,
            "size_bytes": len(payload),
            "created_dirs": [relative_to_root(base, item) for item in created_dirs],
            "replaced": replacing,
        }

    def delete_entry(self, path: str) -> dict[str, Any]:
        """删除工作区内的条目：先入 `.trash/`，且必须先拿到删除审批。"""

        self._require_write_mode()
        base = self.root()
        target = resolve_in_workspace(base, path)
        if target == base:
            raise WorkspacePathError("不能删除工作区根目录")
        if not target.exists():
            raise WorkspacePathError(f"不存在：{path}")
        relative = relative_to_root(base, target)
        self._require_approval(
            kind="delete",
            target=relative,
            reason="删除工作区内的条目",
            payload={"kind": "dir" if target.is_dir() else "file"},
        )
        trash = base / self._settings.delete_trash_dir
        trash.mkdir(exist_ok=True)
        destination = _unique_trash_path(trash, target.name)
        try:
            target.rename(destination)
        except OSError as exc:
            raise WorkspaceError(f"删除失败：{relative}（{exc}）") from exc
        log_event(
            logger,
            "workspace.delete",
            workspace_id=self._row["id"],
            path=relative,
            trashed_to=relative_to_root(base, destination),
        )
        return {
            "path": relative,
            "trashed_to": relative_to_root(base, destination),
            "kind": "dir" if target.is_dir() else "file",
        }

    def make_dir(self, path: str) -> dict[str, Any]:
        self._require_write_mode()
        base = self.root()
        target = resolve_in_workspace(base, path)
        if target.exists():
            raise WorkspaceExistsError(f"已经存在：{path}")
        created_dirs = _missing_parents(base, target)
        self._ensure_quota(
            extra_bytes=0, extra_entries=len(created_dirs), path=path
        )
        try:
            target.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise WorkspaceExistsError(f"已经存在：{path}") from exc
        except OSError as exc:
            raise WorkspaceError(f"创建目录失败：{path}（{exc}）") from exc
        log_event(
            logger,
            "workspace.mkdir",
            workspace_id=self._row["id"],
            path=relative_to_root(base, target),
        )
        return {
            "path": relative_to_root(base, target),
            "created_dirs": [relative_to_root(base, item) for item in created_dirs],
        }

    def move_entry(self, source: str, target: str) -> dict[str, Any]:
        self._require_write_mode()
        base = self.root()
        src = resolve_in_workspace(base, source)
        dst = resolve_in_workspace(base, target)
        if not src.exists():
            raise WorkspacePathError(f"源不存在：{source}")
        if dst.exists():
            if src.is_dir() or dst.is_dir():
                # 目录的"覆盖"语义是整棵子树被替换，破坏性太大，本轮直接不做。
                raise WorkspacePathError(
                    f"目标是目录，不能覆盖：{target}；请先删除或改名"
                )
            relative = relative_to_root(base, dst)
            self._require_approval(
                kind="overwrite",
                target=relative,
                reason="移动会覆盖目标文件",
                payload={"operation": "move", "source": relative_to_root(base, src)},
            )
        if src.is_dir() and dst.is_relative_to(src):
            raise WorkspacePathError(f"不能把目录移动到它自己的子路径下：{target}")
        created_dirs = _missing_parents(base, dst.parent)
        self._ensure_quota(
            extra_bytes=0, extra_entries=len(created_dirs), path=target
        )
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst.exists():
                # 已经拿到"覆盖该目标"的审批，用 replace 一步换掉。
                os.replace(src, dst)
            else:
                src.rename(dst)
        except FileExistsError as exc:
            raise WorkspaceExistsError(f"目标已存在：{target}") from exc
        except OSError as exc:
            raise WorkspaceError(f"移动失败：{source} → {target}（{exc}）") from exc
        log_event(
            logger,
            "workspace.move",
            workspace_id=self._row["id"],
            source=relative_to_root(base, src),
            target=relative_to_root(base, dst),
        )
        return {
            "source": relative_to_root(base, src),
            "target": relative_to_root(base, dst),
        }

    def _require_approval(
        self,
        *,
        kind: str,
        target: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """拿到可用审批就放行（并消费掉），否则登记一条 `pending` 并拒绝本次调用。"""

        workspace_id = self._row["id"]
        if approvals.consume(workspace_id=workspace_id, kind=kind, target=target):
            return
        request = approvals.request_or_reuse(
            workspace_id=workspace_id,
            session_id=self._row.get("session_id"),
            kind=kind,
            target=target,
            reason=reason,
            payload=payload,
            settings=self._settings,
        )
        raise WorkspaceApprovalRequired(
            f"{reason}需要人工审批：{target}；已提交审批 {request['id']}，"
            "请在界面上批准后重试同一调用（不批准就不要重试）"
        )

    def _ensure_quota(
        self, *, extra_bytes: int, extra_entries: int, path: str
    ) -> dict[str, Any]:
        """写之前查配额；超限时把**当前用量与上限**一起说出来。

        `scan_usage` 在条目数超过 `scan_limit` 时会截断，此时用量是**下界**——
        仍然按它拦（宁可保守），因此错误信息里的数字可能偏小。
        """

        return _ensure_quota(
            self.root(),
            extra_bytes=extra_bytes,
            extra_entries=extra_entries,
            path=path,
            settings=self._settings,
        )


def _ensure_quota(
    base: Path,
    *,
    extra_bytes: int,
    extra_entries: int,
    path: str,
    settings: WorkspaceSettings,
) -> dict[str, Any]:
    """配额检查的唯一实现：写文件与批量导入都走这里（两份口径最容易漂）。"""

    usage = scan_usage(base, settings=settings)
    limits = default_quota(settings)
    if usage["total_bytes"] + extra_bytes > limits["max_total_bytes"]:
        raise WorkspaceQuotaExceeded(
            f"超过目录总字节配额：当前 {usage['total_bytes']} + 本次 {extra_bytes} "
            f"> 上限 {limits['max_total_bytes']} 字节（{path}）"
        )
    if usage["entries"] + extra_entries > limits["max_entries"]:
        raise WorkspaceQuotaExceeded(
            f"超过目录条目配额：当前 {usage['entries']} + 本次 {extra_entries} "
            f"> 上限 {limits['max_entries']}（{path}）"
        )
    return usage


def _missing_parents(base: Path, directory: Path) -> list[Path]:
    """从 `base` 到 `directory` 之间尚不存在的层级（用于条目配额与回执）。"""

    missing: list[Path] = []
    current = directory
    while current != base and current.is_relative_to(base) and not current.exists():
        missing.append(current)
        current = current.parent
    return missing


def _unique_trash_path(trash: Path, name: str) -> Path:
    """软删除目标：`<UTC 时间戳>-<原名>`，重名时加序号——不覆盖回收站里的旧条目。"""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    candidate = trash / f"{stamp}-{name}"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = trash / f"{stamp}-{counter}-{name}"
    return candidate


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
    "update_workspace",
    "workspace_source",
    "workspace_tree",
]

"""工作区服务：登记、目录树、用量与只读读取（ADR-033 阶段 1）。

存储用替身（不连 PostgreSQL，与 `tests/unit/conftest.py` 的口径一致），
文件系统用 `tmp_path` 真目录——路径规则必须对着真文件系统验，替身验不出符号链接逃逸。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import checkpoint
from app.workspace import service
from app.workspace.config import WorkspaceSettings
from app.workspace.errors import (
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceModeUnavailable,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceRootUnavailable,
)


class FakeWorkspaceStore:
    """复现 `checkpoint` 里工作区相关函数的读写语义。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def install(self, monkeypatch) -> "FakeWorkspaceStore":
        for name in (
            "list_workspaces",
            "get_workspace",
            "find_workspace_by_path",
            "create_workspace",
            "delete_workspace",
        ):
            monkeypatch.setattr(checkpoint, name, getattr(self, name))
        return self

    def list_workspaces(self, *, session_id=None) -> list[dict]:
        rows = sorted(self.rows.values(), key=lambda row: (row["created_at"], row["id"]))
        if session_id is not None:
            rows = [row for row in rows if row.get("session_id") == str(session_id)]
        return [dict(row) for row in rows]

    def get_workspace(self, workspace_id) -> dict | None:
        row = self.rows.get(str(workspace_id))
        return dict(row) if row else None

    def find_workspace_by_path(self, path: str) -> dict | None:
        for row in self.rows.values():
            if row["path"] == path:
                return dict(row)
        return None

    def create_workspace(
        self,
        *,
        workspace_id,
        path: str,
        mode: str,
        session_id=None,
        name=None,
        quota=None,
        created_by=None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        row = {
            "id": str(workspace_id),
            "session_id": str(session_id) if session_id else None,
            "path": path,
            "mode": mode,
            "name": name,
            "quota": dict(quota or {}),
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        self.rows[row["id"]] = row
        return dict(row)

    def delete_workspace(self, workspace_id) -> bool:
        return self.rows.pop(str(workspace_id), None) is not None


@pytest.fixture
def store(monkeypatch) -> FakeWorkspaceStore:
    return FakeWorkspaceStore().install(monkeypatch)


@pytest.fixture
def settings(tmp_path: Path) -> WorkspaceSettings:
    return WorkspaceSettings(
        _env_file=None,
        root=str(tmp_path),
        # 读取上限要大于 read_max_chars，否则"截断"和"超限"两件事会撞在一起。
        max_file_bytes=256,
        read_max_chars=100,
        tree_max_entries=5,
        scan_limit=5,
    )


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 取决于本机权限
        pytest.skip(f"本机不允许创建符号链接：{exc}")


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #


def test_default_path_is_the_session_directory(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", settings=settings)

    assert view["path"] == "sessions/s-1"
    assert (tmp_path / "sessions" / "s-1").is_dir(), "目录不存在时由平台创建"
    assert view["mode"] == "read_only"
    assert view["quota"]["max_file_bytes"] == 256
    assert view["usage"]["available"] is True
    assert view["usage"]["entries"] == 0


def test_explicit_path_is_bound_and_created(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project/docs", settings=settings)

    assert view["path"] == "project/docs"
    assert (tmp_path / "project" / "docs").is_dir()


def test_duplicate_path_is_rejected(store, settings):
    service.create_workspace(session_id="s-1", path="shared", settings=settings)

    with pytest.raises(WorkspaceExistsError):
        service.create_workspace(session_id="s-2", path="shared", settings=settings)


def test_workspace_write_is_not_available_in_phase_one(store, settings):
    """阶段 1 只读：写档位必须显式拒绝，而不是存下一个不生效的档位。"""

    with pytest.raises(WorkspaceModeUnavailable):
        service.create_workspace(session_id="s-1", mode="workspace_write", settings=settings)


def test_unknown_mode_is_a_validation_error(store, settings):
    with pytest.raises(WorkspaceError) as excinfo:
        service.create_workspace(session_id="s-1", mode="admin", settings=settings)

    assert not isinstance(excinfo.value, WorkspaceModeUnavailable)


def test_escaping_path_is_rejected_at_registration(store, settings):
    with pytest.raises(WorkspacePathError):
        service.create_workspace(session_id="s-1", path="../escape", settings=settings)


def test_disabled_workspace_fails_loudly(store, tmp_path: Path):
    disabled = WorkspaceSettings(_env_file=None, root=str(tmp_path), enabled=False)

    with pytest.raises(WorkspaceDisabled):
        service.create_workspace(session_id="s-1", settings=disabled)


def test_missing_root_is_unavailable(store, tmp_path: Path):
    broken = WorkspaceSettings(_env_file=None, root=str(tmp_path / "nope"))

    with pytest.raises(WorkspaceRootUnavailable):
        service.create_workspace(session_id="s-1", settings=broken)


# --------------------------------------------------------------------------- #
# 列表 / 读取 / 删除
# --------------------------------------------------------------------------- #


def test_list_get_and_delete(store, settings, tmp_path: Path):
    created = service.create_workspace(session_id="s-1", path="project", settings=settings)
    service.create_workspace(session_id="s-2", path="other", settings=settings)
    (tmp_path / "project" / "keep.txt").write_text("keep", encoding="utf-8")

    listing = service.list_workspaces(session_id="s-1", settings=settings)
    assert listing["total"] == 1
    assert listing["items"][0]["path"] == "project"

    view = service.get_workspace(created["id"], settings=settings)
    assert view["id"] == created["id"]

    service.delete_workspace(created["id"])
    assert store.get_workspace(created["id"]) is None
    assert (tmp_path / "project" / "keep.txt").is_file(), "解除登记不删宿主文件"

    with pytest.raises(WorkspaceNotFoundError):
        service.get_workspace(created["id"], settings=settings)


def test_delete_missing_workspace_is_not_found(store):
    with pytest.raises(WorkspaceNotFoundError):
        service.delete_workspace("missing")


# --------------------------------------------------------------------------- #
# 目录树
# --------------------------------------------------------------------------- #


def _seed_workspace(store, settings, tmp_path: Path) -> dict:
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    base = tmp_path / "project"
    (base / "zebra").mkdir()
    (base / "zebra" / "deep.txt").write_text("deep", encoding="utf-8")
    (base / "alpha.txt").write_text("alpha", encoding="utf-8")
    (base / service.DEFAULT_SESSION_DIR).mkdir(exist_ok=True)
    (base / ".trash").mkdir()
    (base / ".trash" / "gone.txt").write_text("gone", encoding="utf-8")
    return view


def test_tree_lists_dirs_first_and_skips_trash(store, settings, tmp_path: Path):
    view = _seed_workspace(store, settings, tmp_path)

    tree = service.workspace_tree(view["id"], settings=settings)

    names = [entry["name"] for entry in tree["entries"]]
    assert names == ["sessions", "zebra", "alpha.txt"]
    assert ".trash" not in names
    assert tree["truncated"] is False


def test_tree_depth_includes_children(store, settings, tmp_path: Path):
    view = _seed_workspace(store, settings, tmp_path)

    tree = service.workspace_tree(view["id"], path="zebra", depth=2, settings=settings)

    assert tree["path"] == "zebra"
    assert [entry["name"] for entry in tree["entries"]] == ["deep.txt"]
    assert tree["entries"][0]["kind"] == "file"
    assert tree["entries"][0]["size_bytes"] == 4


def test_tree_truncates_when_over_the_entry_limit(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    for index in range(8):
        (tmp_path / "project" / f"f{index}.txt").write_text("x", encoding="utf-8")

    tree = service.workspace_tree(view["id"], settings=settings)

    assert len(tree["entries"]) == 5
    assert tree["truncated"] is True


def test_tree_marks_symlinks_that_escape_the_workspace(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(tmp_path / "project" / "escape", outside, directory=True)

    tree = service.workspace_tree(view["id"], settings=settings, )

    entry = next(item for item in tree["entries"] if item["name"] == "escape")
    assert entry["kind"] == "symlink"
    assert entry["outside"] is True
    assert "children" not in entry, "越界链接不跟随"


def test_tree_rejects_paths_outside_the_workspace(store, settings, tmp_path: Path):
    view = _seed_workspace(store, settings, tmp_path)

    with pytest.raises(WorkspacePathError):
        service.workspace_tree(view["id"], path="../", settings=settings)


# --------------------------------------------------------------------------- #
# 用量与只读读取
# --------------------------------------------------------------------------- #


def test_usage_counts_bytes_entries_and_skips_trash(store, settings, tmp_path: Path):
    view = _seed_workspace(store, settings, tmp_path)

    # 视图里的 usage 是登记那一刻的快照，所以要重新读一次（目录是登记之后才塞满的）。
    usage = service.get_workspace(view["id"], settings=settings)["usage"]

    assert usage["available"] is True
    assert usage["total_bytes"] == len("deep") + len("alpha")
    assert ".trash/gone.txt" not in str(usage)
    # sessions、zebra、zebra/deep.txt、alpha.txt；`.trash` 与其内容跳过
    assert usage["entries"] == 4


def test_usage_truncates_at_scan_limit(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    for index in range(9):
        (tmp_path / "project" / f"f{index}.txt").write_text("x", encoding="utf-8")

    usage = service.get_workspace(view["id"], settings=settings)["usage"]

    assert usage["truncated"] is True
    assert usage["entries"] == 5


def test_read_file_returns_text_and_truncates(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    (tmp_path / "project" / "note.md").write_text("x" * 150, encoding="utf-8")
    source = service.LocalWorkspaceSource(view, settings=settings)

    payload = source.read_file("note.md")

    assert payload["total_chars"] == 150
    assert len(payload["text"]) == 100
    assert payload["truncated"] is True


def test_read_file_rejects_binary_and_oversized(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    base = tmp_path / "project"
    (base / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    (base / "big.txt").write_text("x" * 300, encoding="utf-8")
    source = service.LocalWorkspaceSource(view, settings=settings)

    with pytest.raises(WorkspaceError, match="UTF-8"):
        source.read_file("blob.bin")
    with pytest.raises(WorkspaceError, match="读取上限"):
        source.read_file("big.txt")


def test_read_file_rejects_paths_outside_the_workspace(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    source = service.LocalWorkspaceSource(view, settings=settings)

    with pytest.raises(WorkspacePathError):
        source.read_file("../secret.txt")


# --------------------------------------------------------------------------- #
# 会话绑定（工具接线用）
# --------------------------------------------------------------------------- #


def test_workspace_source_needs_a_binding(store, settings):
    assert service.workspace_source("s-1", settings=settings) is None

    service.create_workspace(session_id="s-1", path="project", settings=settings)
    bound = service.workspace_source("s-1", settings=settings)

    assert bound is not None
    assert bound.workspace["path"] == "project"
    assert service.workspace_source("s-2", settings=settings) is None


def test_workspace_source_is_fail_soft_when_the_root_disappears(store, tmp_path: Path):
    """根被挪走时返回 `None`（没有额外工具），而不是把流水线带崩（ADR-026 同一取向）。"""

    settings = WorkspaceSettings(_env_file=None, root=str(tmp_path))
    service.create_workspace(session_id="s-1", path="project", settings=settings)
    broken = WorkspaceSettings(_env_file=None, root=str(tmp_path / "gone"))

    assert service.workspace_source("s-1", settings=broken) is None

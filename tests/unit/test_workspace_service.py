"""工作区服务：登记、目录树、用量与只读读取（ADR-033 阶段 1）。

存储用替身（不连 PostgreSQL，与 `tests/unit/conftest.py` 的口径一致），
文件系统用 `tmp_path` 真目录——路径规则必须对着真文件系统验，替身验不出符号链接逃逸。
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import checkpoint
from app.workspace import service
from app.workspace.config import WorkspaceSettings
from app.workspace.errors import (
    WorkspaceApprovalRequired,
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceHostBrowseDisabled,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceRootUnavailable,
)


class FakeWorkspaceStore:
    """复现 `checkpoint` 里工作区相关函数的读写语义。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}

    def install(self, monkeypatch) -> "FakeWorkspaceStore":
        for name in (
            "list_workspaces",
            "get_workspace",
            "find_workspace_by_path",
            "create_workspace",
            "update_workspace",
            "delete_workspace",
            "create_approval",
            "get_approval",
            "find_approval",
            "update_approval",
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

    def find_workspace_by_path(self, path: str, *, session_id=None) -> dict | None:
        """与 `checkpoint` 同口径：给了 `session_id` 就限定该会话（去重范围是会话内）。"""

        for row in self.rows.values():
            if row["path"] != path:
                continue
            if session_id is not None and str(row.get("session_id")) != str(session_id):
                continue
            if session_id is None and row.get("session_id") is not None:
                continue
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

    def update_workspace(
        self, workspace_id, *, mode=None, name=None, updated_by=None
    ) -> dict | None:
        row = self.rows.get(str(workspace_id))
        if row is None:
            return None
        if mode is not None:
            row["mode"] = mode
        if name is not None:
            row["name"] = name
        row["updated_by"] = updated_by
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)

    # -- 审批（阶段 3） ---------------------------------------------------- #

    def create_approval(
        self,
        *,
        approval_id,
        kind: str,
        target: str,
        workspace_id=None,
        session_id=None,
        run_id=None,
        reason=None,
        payload=None,
    ) -> dict:
        row = {
            "id": str(approval_id),
            "workspace_id": str(workspace_id) if workspace_id else None,
            "session_id": str(session_id) if session_id else None,
            "run_id": None,
            "kind": kind,
            "target": target,
            "reason": reason,
            "payload": dict(payload or {}),
            "status": "pending",
            "decided_by": None,
            "requested_at": datetime.now(timezone.utc),
            "decided_at": None,
        }
        self.approvals[row["id"]] = row
        return dict(row)

    def get_approval(self, approval_id) -> dict | None:
        row = self.approvals.get(str(approval_id))
        return dict(row) if row else None

    def find_approval(self, *, workspace_id, kind, target, status) -> dict | None:
        matched = [
            row
            for row in self.approvals.values()
            if row["workspace_id"] == str(workspace_id)
            and row["kind"] == kind
            and row["target"] == target
            and row["status"] == status
        ]
        if not matched:
            return None
        return dict(max(matched, key=lambda row: row["requested_at"]))

    def update_approval(self, approval_id, *, status: str, decided_by=None) -> dict | None:
        row = self.approvals.get(str(approval_id))
        if row is None:
            return None
        row["status"] = status
        if decided_by is not None:
            row["decided_by"] = decided_by
        if status != "pending":
            row["decided_at"] = datetime.now(timezone.utc)
        return dict(row)


@pytest.fixture
def store(monkeypatch) -> FakeWorkspaceStore:
    return FakeWorkspaceStore().install(monkeypatch)


@pytest.fixture
def settings(tmp_path: Path) -> WorkspaceSettings:
    return WorkspaceSettings(source="container",
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


def test_the_same_folder_can_be_registered_by_different_sessions(store, settings):
    """一个目录被多个项目共用是常态：不同会话各自登记，互不排斥（2026-09-23 修正）。"""

    first = service.create_workspace(session_id="s-1", path="shared", settings=settings)
    second = service.create_workspace(session_id="s-2", path="shared", settings=settings)

    assert first["id"] != second["id"]
    assert (first["path"], second["path"]) == ("shared", "shared")
    # 各会话持有自己的档位：一个提档不影响另一个。
    lifted = service.update_workspace(first["id"], mode="workspace_write", settings=settings)
    assert lifted["mode"] == "workspace_write"
    assert service.get_workspace(second["id"], settings=settings)["mode"] == "read_only"


def test_registering_the_same_path_again_is_idempotent_but_keeps_the_new_mode(store, settings):
    """同一会话 + 同一路径 = 幂等（不重建那条登记，审批不会因此丢），但档位跟随这次选择。"""

    first = service.create_workspace(
        session_id="s-1", path="shared", mode="read_only", settings=settings
    )

    again = service.create_workspace(
        session_id="s-1", path="shared", mode="workspace_write", settings=settings
    )

    assert again["id"] == first["id"], "同路径重复登记不该换一条新行"
    assert again["mode"] == "workspace_write", "档位是使用者这次明确选的，不能静默忽略"
    assert len(service.list_workspaces(session_id="s-1", settings=settings)["items"]) == 1


def test_registering_another_folder_rebinds_the_session(store, settings, tmp_path: Path):
    """**一个会话只绑定一个工作区**：换文件夹＝改绑，旧绑定在同一步解除。

    这条钉的是本轮修的缺陷本身：之前一个会话能留下多条登记，而执行侧只取最早那条，
    于是"界面上选了哪个"与"Agent 实际用哪个"不一致。
    """

    service.create_workspace(session_id="s-1", path="first", settings=settings)
    other = service.create_workspace(session_id="s-2", path="shared", settings=settings)

    rebound = service.create_workspace(session_id="s-1", path="second", settings=settings)

    rows = service.list_workspaces(session_id="s-1", settings=settings)["items"]
    assert [row["path"] for row in rows] == ["second"]
    assert rebound["id"] != other["id"]
    # 别的会话不受影响
    assert [row["path"] for row in service.list_workspaces(session_id="s-2", settings=settings)["items"]] == [
        "shared"
    ]
    # **准确性**：执行侧拿到的就是那条唯一的绑定，与界面所见一致
    source = service.workspace_source("s-1", settings=settings)
    assert source is not None
    assert source.root() == service.workspace_base(rebound, settings=settings)


def test_workspace_write_can_be_created_or_lifted(store, settings, tmp_path: Path):
    """阶段 2 起写档位可用；提档是人的动作，记 `updated_by` 便于追责。"""

    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    assert view["mode"] == "read_only"

    lifted = service.update_workspace(view["id"], mode="workspace_write", actor="ui", settings=settings)
    assert lifted["mode"] == "workspace_write"
    assert lifted["updated_by"] == "ui"

    lowered = service.update_workspace(view["id"], mode="read_only", settings=settings)
    assert lowered["mode"] == "read_only"


def test_update_missing_workspace_is_not_found(store, settings):
    with pytest.raises(WorkspaceNotFoundError):
        service.update_workspace("missing", mode="read_only", settings=settings)


def test_unknown_mode_is_a_validation_error(store, settings):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)

    with pytest.raises(WorkspaceError, match="mode 取值无效"):
        service.update_workspace(view["id"], mode="admin", settings=settings)


def test_unknown_mode_is_rejected_at_registration(store, settings):
    with pytest.raises(WorkspaceError, match="mode 取值无效"):
        service.create_workspace(session_id="s-1", mode="admin", settings=settings)


def test_escaping_path_is_rejected_at_registration(store, settings):
    with pytest.raises(WorkspacePathError):
        service.create_workspace(session_id="s-1", path="../escape", settings=settings)


def test_disabled_workspace_fails_loudly(store, tmp_path: Path):
    disabled = WorkspaceSettings(source="container", _env_file=None, root=str(tmp_path), enabled=False)

    with pytest.raises(WorkspaceDisabled):
        service.create_workspace(session_id="s-1", settings=disabled)


def test_missing_root_is_unavailable(store, tmp_path: Path):
    broken = WorkspaceSettings(source="container", _env_file=None, root=str(tmp_path / "nope"))

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
# 根的目录树（登记之前选位置）
# --------------------------------------------------------------------------- #


def test_root_tree_lists_the_root_without_any_workspace(store, settings, tmp_path: Path):
    """「选文件夹位置」发生在登记之前：不该要求先有一条工作区。"""
    (tmp_path / "project").mkdir()
    (tmp_path / "notes.txt").write_text("note", encoding="utf-8")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "gone.txt").write_text("gone", encoding="utf-8")

    tree = service.root_tree(settings=settings)

    # 目录优先、跳过回收站，与 `workspace_tree` 同一口径。
    assert [entry["name"] for entry in tree["entries"]] == ["project", "notes.txt"]
    assert tree["path"] == ""
    assert store.rows == {}, "列根目录不该写库"


def test_root_tree_walks_into_a_subdirectory(store, settings, tmp_path: Path):
    (tmp_path / "project" / "reports").mkdir(parents=True)
    (tmp_path / "project" / "readme.md").write_text("hi", encoding="utf-8")

    tree = service.root_tree(path="project", depth=2, settings=settings)

    assert tree["path"] == "project"
    assert tree["depth"] == 2
    entry = next(item for item in tree["entries"] if item["name"] == "reports")
    assert entry["kind"] == "dir"
    assert entry["path"] == "project/reports"


@pytest.mark.parametrize("value", ["../", "/etc", "C:/windows", "..\\..\\x"])
def test_root_tree_rejects_paths_outside_the_root(store, settings, tmp_path: Path, value):
    with pytest.raises(WorkspacePathError):
        service.root_tree(path=value, settings=settings)


def test_root_tree_marks_symlinks_that_escape_the_root(store, settings, tmp_path: Path):
    # 这里 tmp_path **就是**工作区根，所以"根外"必须是 tmp_path 之外——
    # 不能用 `tmp_path / "outside"`（那仍在根内，标记成 outside 反而错）。
    _symlink_or_skip(
        tmp_path / "escape", tmp_path.parent / "macp-root-tree-outside", directory=True
    )

    tree = service.root_tree(settings=settings)

    entry = next(item for item in tree["entries"] if item["name"] == "escape")
    assert entry["kind"] == "symlink"
    assert entry["outside"] is True
    assert "children" not in entry, "越界链接不跟随，也不暴露根外结构"


def test_root_tree_is_disabled_with_the_feature(store, settings, tmp_path: Path):
    disabled = settings.model_copy(update={"enabled": False})

    with pytest.raises(WorkspaceDisabled):
        service.root_tree(settings=disabled)


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
# 阶段 2：写档位（非破坏性写操作）
# --------------------------------------------------------------------------- #


def _writable(store, settings, tmp_path: Path) -> service.LocalWorkspaceSource:
    view = service.create_workspace(
        session_id="s-1", path="project", mode="workspace_write", settings=settings
    )
    return service.LocalWorkspaceSource(view, settings=settings)


def test_read_only_tier_refuses_writes(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    source = service.LocalWorkspaceSource(view, settings=settings)

    for call in (
        lambda: source.write_file("a.txt", "hi"),
        lambda: source.make_dir("d"),
        lambda: source.move_entry("a.txt", "b.txt"),
    ):
        with pytest.raises(WorkspaceError, match="只读档位"):
            call()


def test_write_file_creates_parents_and_reports_size(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    result = source.write_file("reports/2026/summary.md", "hello")

    assert result["path"] == "reports/2026/summary.md"
    assert result["size_bytes"] == 5
    assert result["created_dirs"] == ["reports/2026", "reports"]
    assert (tmp_path / "project" / "reports" / "2026" / "summary.md").read_text(
        encoding="utf-8"
    ) == "hello"


def test_write_file_refuses_to_overwrite_without_approval(store, settings, tmp_path: Path):
    """覆盖是破坏性动作：ADR-033 §6 要人工审批，阶段 3 之前一律拒绝。"""

    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "first")

    with pytest.raises(WorkspaceExistsError, match="覆盖需要人工审批"):
        source.write_file("a.txt", "second")
    with pytest.raises(WorkspaceApprovalRequired):
        source.write_file("a.txt", "second", overwrite=True)
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "first"


def test_overwrite_flag_on_a_new_path_is_still_a_create(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    source.write_file("fresh.txt", "new", overwrite=True)

    assert (tmp_path / "project" / "fresh.txt").read_text(encoding="utf-8") == "new"


def test_write_file_rejects_oversized_content(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    with pytest.raises(WorkspaceQuotaExceeded, match="单文件上限"):
        source.write_file("big.txt", "x" * 300)


def test_write_file_rejects_paths_outside_the_workspace(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    with pytest.raises(WorkspacePathError):
        source.write_file("../escape.txt", "x")


def test_total_bytes_quota_is_enforced_with_current_usage(store, tmp_path: Path):
    tight = WorkspaceSettings(source="container",
        _env_file=None, root=str(tmp_path), max_file_bytes=256, max_total_bytes=10
    )
    source = _writable(store, tight, tmp_path)

    with pytest.raises(WorkspaceQuotaExceeded) as excinfo:
        source.write_file("a.txt", "x" * 11)

    message = str(excinfo.value)
    assert "当前 0" in message and "上限 10" in message


def test_entry_quota_counts_directories_too(store, tmp_path: Path):
    tight = WorkspaceSettings(source="container",
        _env_file=None, root=str(tmp_path), max_file_bytes=256, max_entries=2
    )
    source = _writable(store, tight, tmp_path)

    source.make_dir("a")
    with pytest.raises(WorkspaceQuotaExceeded, match="条目配额"):
        source.make_dir("b/c")


def test_make_dir_rejects_an_existing_path(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.make_dir("reports")

    with pytest.raises(WorkspaceExistsError, match="已经存在"):
        source.make_dir("reports")


def test_move_entry_renames_and_gates_overwrite_behind_approval(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "content")
    source.write_file("b.txt", "other")

    moved = source.move_entry("a.txt", "docs/a.txt")
    assert moved["source"] == "a.txt"
    assert moved["target"] == "docs/a.txt"
    assert not (tmp_path / "project" / "a.txt").exists()
    assert (tmp_path / "project" / "docs" / "a.txt").read_text(encoding="utf-8") == "content"

    # 阶段 3 起：覆盖目标改成"提交审批"，不再是一句"目标已存在"。
    with pytest.raises(WorkspaceApprovalRequired, match="已提交审批"):
        source.move_entry("docs/a.txt", "b.txt")


def test_move_entry_rejects_moving_a_directory_into_itself(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.make_dir("reports")

    with pytest.raises(WorkspacePathError):
        source.move_entry("reports", "reports/2026")


def test_move_entry_rejects_a_missing_source(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    with pytest.raises(WorkspacePathError, match="源不存在"):
        source.move_entry("nope.txt", "other.txt")


def test_move_entry_rejects_escaping_targets(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "content")

    with pytest.raises(WorkspacePathError):
        source.move_entry("a.txt", "../outside.txt")


# --------------------------------------------------------------------------- #
# 阶段 3：破坏性动作走审批
# --------------------------------------------------------------------------- #


def _approve(store, *, workspace_id: str, kind: str, target: str) -> None:
    """模拟用户在界面上批准：找到那条 pending 并决策。"""

    from app.workspace import approvals as approvals_module

    row = store.find_approval(workspace_id=workspace_id, kind=kind, target=target, status="pending")
    assert row is not None, "应当先有一条待决策的审批"
    approvals_module.decide(row["id"], decision="approved", actor="ui")


def test_new_files_need_no_approval(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    source.write_file("fresh.txt", "content")

    assert store.approvals == {}


def test_overwrite_requires_an_approval_then_replaces_once(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "first")

    with pytest.raises(WorkspaceApprovalRequired) as excinfo:
        source.write_file("a.txt", "second", overwrite=True)
    assert "已提交审批" in str(excinfo.value)
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "first"

    _approve(store, workspace_id=source.workspace["id"], kind="overwrite", target="a.txt")

    result = source.write_file("a.txt", "second", overwrite=True)
    assert result["replaced"] is True
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "second"

    # 一次授权只放行一次：再来一次又要审批
    with pytest.raises(WorkspaceApprovalRequired):
        source.write_file("a.txt", "third", overwrite=True)


def test_delete_moves_the_entry_into_trash_after_approval(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("reports/a.txt", "content")

    with pytest.raises(WorkspaceApprovalRequired):
        source.delete_entry("reports/a.txt")
    assert (tmp_path / "project" / "reports" / "a.txt").exists()

    _approve(store, workspace_id=source.workspace["id"], kind="delete", target="reports/a.txt")

    result = source.delete_entry("reports/a.txt")
    assert result["kind"] == "file"
    assert result["trashed_to"].startswith(".trash/")
    assert not (tmp_path / "project" / "reports" / "a.txt").exists()
    assert (tmp_path / "project" / ".trash").is_dir()
    assert len(list((tmp_path / "project" / ".trash").iterdir())) == 1


def test_delete_refuses_the_workspace_root(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    with pytest.raises(WorkspacePathError, match="不能删除工作区根目录"):
        source.delete_entry("")


def test_delete_rejects_escaping_paths(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)

    with pytest.raises(WorkspacePathError):
        source.delete_entry("../outside.txt")


def test_move_over_a_file_needs_an_approval(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "content")
    source.write_file("b.txt", "other")

    with pytest.raises(WorkspaceApprovalRequired):
        source.move_entry("a.txt", "b.txt")

    _approve(store, workspace_id=source.workspace["id"], kind="overwrite", target="b.txt")

    result = source.move_entry("a.txt", "b.txt")
    assert result["target"] == "b.txt"
    assert (tmp_path / "project" / "b.txt").read_text(encoding="utf-8") == "content"
    assert not (tmp_path / "project" / "a.txt").exists()


def test_move_over_a_directory_is_refused_outright(store, settings, tmp_path: Path):
    """目录的"覆盖"等于替换整棵子树，破坏性太大：本轮直接不做。"""

    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "content")
    source.make_dir("docs")

    with pytest.raises(WorkspacePathError, match="不能覆盖"):
        source.move_entry("a.txt", "docs")


def test_approval_is_not_transferable_between_targets(store, settings, tmp_path: Path):
    source = _writable(store, settings, tmp_path)
    source.write_file("a.txt", "first")
    source.write_file("b.txt", "first")

    with pytest.raises(WorkspaceApprovalRequired):
        source.write_file("a.txt", "second", overwrite=True)
    _approve(store, workspace_id=source.workspace["id"], kind="overwrite", target="a.txt")

    with pytest.raises(WorkspaceApprovalRequired):
        source.write_file("b.txt", "second", overwrite=True)


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

    settings = WorkspaceSettings(source="container", _env_file=None, root=str(tmp_path))
    service.create_workspace(session_id="s-1", path="project", settings=settings)
    broken = WorkspaceSettings(source="container", _env_file=None, root=str(tmp_path / "gone"))

    assert service.workspace_source("s-1", settings=broken) is None


# --------------------------------------------------------------------------- #
# 导入文件（浏览器「选择文件夹」的服务端一侧）
# --------------------------------------------------------------------------- #


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_import_writes_nested_files_and_reports_each(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)

    result = service.import_files(
        view["id"],
        [
            {"path": "docs/readme.md", "content_base64": _b64("# hi")},
            {"path": "notes.txt", "content_base64": _b64("note")},
        ],
        settings=settings,
    )

    assert (result["imported"], result["skipped"], result["failed"]) == (2, 0, 0)
    assert (tmp_path / "project" / "docs" / "readme.md").read_text(encoding="utf-8") == "# hi"
    assert result["usage"]["available"] is True


def test_import_accepts_binary_and_rejects_escaping_paths(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    blob = base64.b64encode(b"\xff\xfe\x00\x01").decode("ascii")

    with pytest.raises(WorkspacePathError):
        service.import_files(
            view["id"],
            [{"path": "../escape.bin", "content_base64": blob}],
            settings=settings,
        )
    assert not (tmp_path / "escape.bin").exists()

    service.import_files(
        view["id"], [{"path": "blob.bin", "content_base64": blob}], settings=settings
    )
    assert (tmp_path / "project" / "blob.bin").read_bytes() == b"\xff\xfe\x00\x01"


def test_import_skips_existing_until_overwrite_is_set(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    files = [{"path": "a.txt", "content_base64": _b64("first")}]
    service.import_files(view["id"], files, settings=settings)

    again = service.import_files(view["id"], files, settings=settings)
    assert again["skipped"] == 1
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "first"

    replaced = service.import_files(
        view["id"],
        [{"path": "a.txt", "content_base64": _b64("second")}],
        overwrite=True,
        settings=settings,
    )
    assert replaced["imported"] == 1
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "second"


def test_import_checks_quota_before_writing_anything(store, tmp_path: Path):
    """批量导入是"先算清楚、再动盘"：不能写到一半才发现超配额。"""

    tight = WorkspaceSettings(source="container",
        _env_file=None, root=str(tmp_path), max_file_bytes=256, max_total_bytes=8
    )
    view = service.create_workspace(session_id="s-1", path="project", settings=tight)

    with pytest.raises(WorkspaceQuotaExceeded):
        service.import_files(
            view["id"],
            [
                {"path": "a.txt", "content_base64": _b64("12345")},
                {"path": "b.txt", "content_base64": _b64("67890")},
            ],
            settings=tight,
        )
    assert list((tmp_path / "project").iterdir()) == [], "配额不通过时一个字节都不该落盘"


def test_import_rejects_too_many_files_and_oversized_ones(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)
    tiny = WorkspaceSettings(source="container",
        _env_file=None, root=str(tmp_path), import_max_files=1, import_max_file_bytes=4
    )

    with pytest.raises(WorkspaceError, match="最多导入"):
        service.import_files(
            view["id"],
            [
                {"path": "a.txt", "content_base64": _b64("x")},
                {"path": "b.txt", "content_base64": _b64("y")},
            ],
            settings=tiny,
        )
    with pytest.raises(WorkspaceError, match="单文件导入上限"):
        service.import_files(
            view["id"], [{"path": "big.txt", "content_base64": _b64("12345")}], settings=tiny
        )


def test_import_rejects_bad_base64_and_missing_path(store, settings, tmp_path: Path):
    view = service.create_workspace(session_id="s-1", path="project", settings=settings)

    with pytest.raises(WorkspaceError, match="base64"):
        service.import_files(
            view["id"], [{"path": "a.txt", "content_base64": "not base64!"}], settings=settings
        )
    with pytest.raises(WorkspaceError, match="缺少 path"):
        service.import_files(
            view["id"], [{"path": "  ", "content_base64": _b64("x")}], settings=settings
        )


# --------------------------------------------------------------------------- #
# 宿主形态（ADR-035）：授权单位是用户当场选的文件夹
# --------------------------------------------------------------------------- #


def _host_settings() -> WorkspaceSettings:
    """宿主形态：`path` 是绝对路径，`root`（容器挂载点）不再参与解析。"""

    return WorkspaceSettings(source="host", _env_file=None, root="/workspace")


def test_host_form_binds_the_chosen_folder_and_defaults_to_write(store, tmp_path: Path):
    chosen = tmp_path / "我的项目"
    chosen.mkdir()
    (chosen / "a.txt").write_text("hi", encoding="utf-8")
    settings = _host_settings()

    view = service.create_workspace(session_id="s-1", path=str(chosen), settings=settings)

    assert view["path"] == str(chosen.resolve())
    assert view["mode"] == "workspace_write", "选了文件夹交出去就是可写（ADR-035 §4）"
    tree = service.workspace_tree(view["id"], settings=settings)
    assert [entry["name"] for entry in tree["entries"]] == ["a.txt"]
    # 工具侧的根走同一条解析
    source = service.LocalWorkspaceSource(view, settings=settings)
    assert source.root() == chosen.resolve()


def test_host_form_writes_inside_the_chosen_folder(store, tmp_path: Path):
    chosen = tmp_path / "工作区"
    chosen.mkdir()
    settings = _host_settings()
    view = service.create_workspace(session_id="s-1", path=str(chosen), settings=settings)
    source = service.LocalWorkspaceSource(view, settings=settings)

    source.write_file("reports/2026.md", "内容")

    assert (chosen / "reports" / "2026.md").read_text(encoding="utf-8") == "内容"
    # 「只能在这个文件夹下面」：越界仍然被拒，与容器形态同一条守卫。
    with pytest.raises(WorkspacePathError):
        source.write_file("../outside.md", "x")


@pytest.mark.parametrize("value", ["project", "sessions/s-1", "./x"])
def test_host_form_rejects_relative_paths(store, tmp_path: Path, value):
    """相对路径在宿主形态下没有意义——它是容器形态留下的登记，不能悄悄按另一层含义解释。"""

    with pytest.raises(WorkspacePathError):
        service.create_workspace(session_id="s-1", path=value, settings=_host_settings())


def test_host_form_refuses_missing_or_non_directory(store, tmp_path: Path):
    settings = _host_settings()
    with pytest.raises(WorkspacePathError):
        service.create_workspace(session_id="s-1", path=str(tmp_path / "nope"), settings=settings)
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspacePathError):
        service.create_workspace(session_id="s-1", path=str(file_path), settings=settings)


def test_host_form_refuses_the_platform_source(store):
    """沙箱对工作区可写，而仓库里是平台自己的源码（与 `pick_work_dir.ps1` 同一条禁令）。"""

    from app.workspace.paths import PLATFORM_HOME

    settings = _host_settings()
    for candidate in (PLATFORM_HOME, PLATFORM_HOME / "app", PLATFORM_HOME.parent):
        with pytest.raises(WorkspacePathError):
            service.create_workspace(session_id="s-1", path=str(candidate), settings=settings)


def test_host_form_default_path_lands_under_home(store, monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    view = service.create_workspace(session_id="s-7", settings=_host_settings())

    assert view["path"] == str(fake_home / "MacpWorkspace" / "sessions" / "s-7")
    assert Path(view["path"]).is_dir()


def test_host_tree_lists_directories_only(store, tmp_path: Path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub2").mkdir()
    (root / "note.txt").write_text("x", encoding="utf-8")

    tree = service.host_tree(path=str(root), settings=_host_settings())

    assert tree["path"] == str(root.resolve())
    assert tree["parent"] == str(tmp_path.resolve())
    # 只列目录：这一步选的是文件夹，铺文件只会让人误点；条目路径是**绝对路径**，
    # 选择器要按它继续往下走。
    assert [entry["path"] for entry in tree["entries"]] == [
        str(root.resolve() / "sub"),
        str(root.resolve() / "sub2"),
    ]
    assert "note.txt" not in str(tree["entries"])
    assert tree["roots"], "盘符 / 根入口要给出，否则没法换盘"


def test_host_tree_starts_at_home_and_can_walk_up(store, monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "home"
    (fake_home / "deep").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    tree = service.host_tree(settings=_host_settings())
    assert tree["path"] == str(fake_home.resolve())
    assert [entry["name"] for entry in tree["entries"]] == ["deep"]

    up = service.host_tree(path=tree["parent"] or str(fake_home), settings=_host_settings())
    assert up["path"] == str(tmp_path.resolve())


def test_host_tree_is_refused_in_container_form(store, settings):
    """容器里看不到宿主路径——开了就是"能选、不能用"的假入口（ADR-035 §3）。"""

    with pytest.raises(WorkspaceHostBrowseDisabled):
        service.host_tree(settings=settings)


def test_source_value_is_validated(store, tmp_path: Path):
    broken = WorkspaceSettings(source="Host ", _env_file=None, root=str(tmp_path))
    # 大小写与空白是被容忍的（部署脚本里手写环境变量很容易带空格）
    service.create_workspace(session_id="s-1", path=str(tmp_path), settings=broken)
    bad = WorkspaceSettings(source="somewhere", _env_file=None, root=str(tmp_path))
    with pytest.raises(WorkspaceError):
        service.create_workspace(session_id="s-2", path=str(tmp_path), settings=bad)

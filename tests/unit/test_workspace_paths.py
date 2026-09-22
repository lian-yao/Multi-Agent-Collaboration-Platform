"""工作区路径守卫（ADR-033 §4、`doc/api.md` §5.19）。

这一层只防一件事：**让相对路径跑出工作区**。所以用例按攻击面列，不按函数列——
`..`、绝对路径、盘符、UNC、Windows 保留名/数据流、符号链接逃逸各自成例。

符号链接那几条在 Windows 上需要开发者模式或管理员权限；建不出来时跳过（`pytest.skip`），
而不是悄悄跳过断言。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.workspace.errors import WorkspacePathError, WorkspaceRootUnavailable
from app.workspace.paths import (
    normalize_relative,
    relative_to_root,
    resolve_in_workspace,
    resolve_root,
)

ACCEPTED = [
    "",
    ".",
    "reports",
    "reports/summary.md",
    "./reports//summary.md",
    "reports\\summary.md",
    "a/b/c.txt",
]

REJECTED = [
    "..",
    "../etc/passwd",
    "reports/../../etc/passwd",
    "/etc/passwd",
    "C:/Windows/System32",
    "c:file.txt",
    r"\\server\share",
    "nul",
    "CON.txt",
    "reports/aux.log",
    "a:b.txt",
    "x" * 501,
    # 整串首尾空白会被 strip，所以这两条要放在**中间段**才测得到段级规则。
    "a/trailing /x",
    "a/trailing./x",
    "bad?name.txt",
    "bad<name>.txt",
    "bad|name.txt",
    'bad"name.txt',
    "bad*name.txt",
    "null\x00byte",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "summary.md").write_text("hi", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("value", ACCEPTED)
def test_relative_paths_inside_the_workspace_are_accepted(root: Path, value: str):
    resolved = resolve_in_workspace(root, value)

    assert resolved.is_relative_to(root.resolve())


@pytest.mark.parametrize("value", REJECTED)
def test_escaping_or_illegal_paths_are_rejected(root: Path, value: str):
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(root, value)


def test_normalize_relative_collapses_empty_segments(root: Path):
    assert normalize_relative("./reports//summary.md").as_posix() == "reports/summary.md"
    assert normalize_relative("") == normalize_relative(".")


def test_relative_to_root_round_trips(root: Path):
    assert relative_to_root(root, root / "reports" / "summary.md") == "reports/summary.md"
    assert relative_to_root(root, root) == ""


def test_expect_asserts_type(root: Path):
    assert resolve_in_workspace(root, "reports", expect="dir").is_dir()
    assert resolve_in_workspace(root, "reports/summary.md", expect="file").is_file()

    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(root, "reports", expect="file")
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(root, "reports/summary.md", expect="dir")


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 取决于本机权限
        pytest.skip(f"本机不允许创建符号链接：{exc}")


def test_symlink_pointing_outside_the_workspace_is_rejected(root: Path):
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    _symlink(root / "escape", outside, directory=True)

    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(root, "escape")
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(root, "escape/secret.txt")


def test_symlink_staying_inside_the_workspace_is_allowed(root: Path):
    _symlink(root / "alias", root / "reports", directory=True)

    resolved = resolve_in_workspace(root, "alias/summary.md", expect="file")

    assert resolved == (root / "reports" / "summary.md").resolve()


def test_missing_root_is_reported_as_unavailable(tmp_path: Path):
    with pytest.raises(WorkspaceRootUnavailable):
        resolve_root(tmp_path / "does-not-exist")


def test_root_must_be_a_directory(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceRootUnavailable):
        resolve_root(target)

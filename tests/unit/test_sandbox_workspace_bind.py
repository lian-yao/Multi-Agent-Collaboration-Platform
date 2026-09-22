"""宿主路径反查：把 backend 容器里的工作区路径翻译成沙箱能挂的宿主路径。

沙箱容器是 backend 的**兄弟容器**（建在宿主守护进程上，ADR-023），bind 来源必须是宿主
路径；翻译错了会挂出一个空目录——代码能跑、文件却"不见了"，是最难查的一类失败。
所以这里用假 mountinfo 把三种情形都钉住：精确命中、子路径、以及最长挂载点优先。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.workspace_bind import parse_mountinfo, resolve_host_path

# 第 2/3 行取自实测：Linux 上的普通 bind mount（source 是宿主路径、root=/）。
# 第 4 行取自本机 Docker Desktop 的真实 `/proc/self/mountinfo`（见下方注释）：
# 9p/drvfs 的 source 是盘符 `C:\`，**root 字段**才带路径。
MOUNTINFO = "\n".join(
    [
        "25 1 0:22 / / rw,relatime - overlay overlay rw",
        "42 25 0:31 / /workspace rw,relatime - ext4 /dev/sdd rw",
        "43 42 0:32 / /workspace/phase2 rw,relatime - ext4 /dev/sde rw",
        (
            "1252 1225 0:68 /Users/zq/PycharmProjects/Multi-Agent\\040Collaboration"
            "\\040Platform/workspaces /workspace-desktop rw,noatime - 9p C:\\134 rw,"
            "aname=drvfs;path=C:\\;uid=0;gid=0"
        ),
    ]
)


def test_parse_mountinfo_returns_mount_points_longest_first():
    entries = parse_mountinfo(MOUNTINFO)

    assert entries[0].mount_point == "/workspace-desktop"
    assert all(
        len(entries[index].mount_point) >= len(entries[index + 1].mount_point)
        for index in range(len(entries) - 1)
    )


def test_windows_drive_source_maps_to_the_desktop_mount_prefix(tmp_path: Path):
    """Docker Desktop：`C:\\` + root → `/run/desktop/mnt/host/c` + root。"""

    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO, encoding="utf-8")

    assert resolve_host_path("/workspace-desktop", mountinfo_path=str(info)) == (
        "/run/desktop/mnt/host/c/Users/zq/PycharmProjects/"
        "Multi-Agent Collaboration Platform/workspaces"
    )


def test_parse_mountinfo_ignores_malformed_lines():
    assert parse_mountinfo("garbage\n\n") == []


def test_override_wins_over_detection(tmp_path: Path):
    resolved = resolve_host_path(
        "/workspace/phase2", override=str(tmp_path), mountinfo_path="ignored"
    )

    assert resolved == str(tmp_path)


def test_exact_mount_point_resolves_to_its_source(tmp_path: Path):
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO, encoding="utf-8")

    assert resolve_host_path("/workspace", mountinfo_path=str(info)) == "/dev/sdd"


def test_sub_path_resolves_to_source_plus_remainder(tmp_path: Path):
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO, encoding="utf-8")

    assert (
        resolve_host_path("/workspace/shared/report.md", mountinfo_path=str(info))
        == "/dev/sdd/shared/report.md"
    )


def test_longest_mount_point_wins(tmp_path: Path):
    """`/workspace/phase2` 比 `/workspace` 更具体：取它，否则会多拼一层路径。"""

    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO, encoding="utf-8")

    assert resolve_host_path("/workspace/phase2", mountinfo_path=str(info)) == "/dev/sde"


def test_missing_mountinfo_returns_none(tmp_path: Path):
    assert (
        resolve_host_path("/workspace", mountinfo_path=str(tmp_path / "nope")) is None
    )


def test_unrelated_mount_points_return_none(tmp_path: Path):
    info = tmp_path / "mountinfo"
    info.write_text(
        "25 1 0:22 / /etc/hostname rw,relatime - tmpfs tmpfs rw", encoding="utf-8"
    )

    assert resolve_host_path("/workspace/phase2", mountinfo_path=str(info)) is None

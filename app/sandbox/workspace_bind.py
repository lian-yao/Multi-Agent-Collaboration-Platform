"""把 backend 容器里的工作区路径翻译成**宿主**路径（ADR-033 §7）。

沙箱容器是 backend 的**兄弟容器**（ADR-023：容器建在宿主的 Docker 守护进程上），
所以 `-v` 的来源必须是宿主路径；而 backend 里看到的只是挂载点（例如 `/workspace`）。
两者不同，直接拿容器路径去挂会得到一个空目录——代码能跑、文件却"不见了"，
这种失败最难查，所以这里显式翻译，翻不出来就报错而不是静默挂错。

默认从 `/proc/self/mountinfo` 反查：取**覆盖该路径的最长挂载点**，再拼出宿主路径。
两种形态（都是实测过的真实数据）：

- Linux bind mount：`- ext4 /dev/sdd`、root=`/` → 宿主路径就是 source；
- Docker Desktop（WSL2）的 9p/drvfs：source 是盘符 `C:\\`、**root 字段**才是路径
  （如 `/Users/zq/…/workspaces`）→ 宿主路径是 `/run/desktop/mnt/host/c` + root。

后者是 Docker Desktop 的固定布局，对同一个守护进程有效，所以默认无需用户配置。
"""

from __future__ import annotations

import os
import posixpath
import re

DEFAULT_MOUNTINFO = "/proc/self/mountinfo"

_ESCAPES = (
    ("\\040", " "),
    ("\\011", "\t"),
    ("\\012", "\n"),
    ("\\134", "\\"),
)


DRVFS_PREFIX = "/run/desktop/mnt/host"
"""Docker Desktop 在 WSL 虚拟机里暴露 Windows 盘符的位置。"""

_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):\\?$")


class MountEntry(tuple):
    """`(挂载点, source, root, 文件系统类型)`；用 tuple 子类是为了让解构照旧自然。"""

    def __new__(cls, mount_point: str, source: str, root: str, fstype: str):
        return super().__new__(cls, (mount_point, source, root, fstype))

    @property
    def mount_point(self) -> str:
        return self[0]

    @property
    def source(self) -> str:
        return self[1]

    @property
    def root(self) -> str:
        return self[2]

    @property
    def fstype(self) -> str:
        return self[3]

    def host_prefix(self) -> str:
        """该挂载点的宿主侧前缀（drvfs 盘符转成虚拟机里的等价路径）。"""

        drive = _WINDOWS_DRIVE.match(self.source)
        if drive:
            return f"{DRVFS_PREFIX}/{drive.group(1).lower()}"
        return self.source


def parse_mountinfo(text: str) -> list[MountEntry]:
    """解析 mountinfo，返回 `(挂载点, source, root, fstype)`（已反转义、按挂载点长度降序）。

    格式（见 `man proc`）：字段以空格分隔，第 4 个是 root、第 5 个是挂载点，
    `-` 之后依次是文件系统类型、来源、挂载选项。
    """

    entries: list[MountEntry] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or "-" not in parts:
            continue
        separator = parts.index("-")
        if separator + 2 >= len(parts):
            continue
        entries.append(
            MountEntry(
                mount_point=_unescape(parts[4]),
                source=_unescape(parts[separator + 2]),
                root=_unescape(parts[3]),
                fstype=parts[separator + 1],
            )
        )
    entries.sort(key=lambda item: len(item.mount_point), reverse=True)
    return entries


def resolve_host_path(
    container_path: str | os.PathLike[str],
    *,
    override: str | None = None,
    mountinfo_path: str = DEFAULT_MOUNTINFO,
) -> str | None:
    """返回宿主侧路径；显式 `override` 优先，其次按 mountinfo 反查，失败返回 `None`。"""

    if override:
        return override

    # 输入永远是**容器内**的 POSIX 路径（mountinfo 也是 POSIX 语义），所以用 posixpath
    # 归一化：在 Windows 上跑（开发机）时 `os.path.abspath("/workspace")` 会变成
    # `C:\workspace`，那样永远匹配不上任何挂载点。
    wanted = posixpath.normpath(str(container_path))
    try:
        with open(mountinfo_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None

    for entry in parse_mountinfo(text):
        mount_point = entry.mount_point
        if wanted == mount_point:
            return posixpath.normpath(entry.host_prefix() + entry.root)
        if wanted.startswith(mount_point.rstrip("/") + "/"):
            remainder = wanted[len(mount_point.rstrip("/")) :].lstrip("/")
            base = posixpath.normpath(entry.host_prefix() + entry.root)
            return posixpath.join(base, remainder) if remainder else base
    return None


def _unescape(value: str) -> str:
    for escaped, plain in _ESCAPES:
        value = value.replace(escaped, plain)
    return value


__all__ = ["DEFAULT_MOUNTINFO", "parse_mountinfo", "resolve_host_path"]

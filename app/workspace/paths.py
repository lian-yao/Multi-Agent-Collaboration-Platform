"""工作区路径守卫（ADR-033 §4）。

**所有**文件访问都要过 `resolve_in_workspace()`，没有旁路：工具、目录树、读取、
以及将来阶段 2 的写/删/移动，来源与目标各自都要过一遍。

两条不变量：

1. 只接受**相对路径**，绝对路径、盘符、UNC 一律拒绝——接口面不为「能不能写 `/etc`」
   这种问题留讨论空间；
2. 解析（展开符号链接）之后必须仍落在工作区根内，指向根外的符号链接一律拒绝。

跨平台的额外拒绝项（Windows 保留名、ADS 冒号、非法字符）**不按平台分支**：容器里跑的是
Linux，但工作区内容来自用户的 Windows 宿主，按最严的一侧统一拒绝才能保证行为可预期。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.workspace.errors import WorkspacePathError, WorkspaceRootUnavailable

FORBIDDEN_CHARS = frozenset('<>:"|?*')
"""Windows 非法字符；`:` 同时挡住盘符与 NTFS 数据流（`file.txt:stream`）。"""

MAX_PATH_CHARS = 500
"""与 `workspaces.path` 的列宽一致。"""

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def normalize_relative(value: str | None) -> PurePosixPath:
    """把接口收到的路径规范成工作区内的相对路径（`.` 表示根）。

    整串的首尾空白先被去掉（表单里带空格是常见的手滑），随后逐段校验：段内以空格或点
    结尾按 Windows 语义拒绝——那类名字在宿主上不可靠，跨平台行为也很难解释。
    """

    raw = (value or "").strip().replace("\\", "/")
    if not raw or raw == ".":
        return PurePosixPath(".")
    if len(raw) > MAX_PATH_CHARS:
        raise WorkspacePathError(f"路径过长（上限 {MAX_PATH_CHARS} 字符）")
    if "\x00" in raw:
        raise WorkspacePathError("路径包含空字符")
    if raw.startswith("/"):
        raise WorkspacePathError("只接受工作区内的相对路径，不接受绝对路径")
    if len(raw) >= 2 and raw[1] == ":":
        raise WorkspacePathError("只接受工作区内的相对路径，不接受盘符")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspacePathError("路径不能包含 `..`")
        if any(char in FORBIDDEN_CHARS for char in part):
            raise WorkspacePathError(f"路径段含非法字符：{part}")
        if any(ord(char) < 32 for char in part):
            raise WorkspacePathError(f"路径段含控制字符：{part}")
        if part != part.rstrip(" ."):
            raise WorkspacePathError(f"路径段不能以空格或点结尾：{part}")
        if part.split(".")[0].upper() in _RESERVED_NAMES:
            raise WorkspacePathError(f"路径段是系统保留名：{part}")
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def resolve_root(root: str | Path) -> Path:
    """解析并校验工作区根；不可用时抛 `WorkspaceRootUnavailable`。"""

    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkspaceRootUnavailable(
            f"工作区根不可用：{root}（{exc}）。容器部署请检查 WORKSPACE_HOST_ROOT 挂载，"
            "宿主机直跑请设置 WORKSPACE_ROOT。"
        ) from exc
    if not resolved.is_dir():
        raise WorkspaceRootUnavailable(f"工作区根不是目录：{root}")
    return resolved


def resolve_in_workspace(
    root: str | Path,
    value: str | None,
    *,
    expect: str | None = None,
) -> Path:
    """把相对路径解析成工作区内的绝对路径，越界一律拒绝。

    `expect` 取 `file` / `dir` 时顺带断言类型，省掉调用方各自判断。
    """

    relative = normalize_relative(value)
    resolved_root = resolve_root(root)
    candidate = (resolved_root / Path(*relative.parts)).resolve()
    if candidate != resolved_root and not candidate.is_relative_to(resolved_root):
        # 走到这里只剩一种可能：路径里有一段符号链接指向了根外。
        raise WorkspacePathError(f"路径越出工作区：{value}")
    if expect == "file" and not candidate.is_file():
        raise WorkspacePathError(f"不是工作区里的文件：{value}")
    if expect == "dir" and not candidate.is_dir():
        raise WorkspacePathError(f"不是工作区里的目录：{value}")
    return candidate


def relative_to_root(root: str | Path, path: Path) -> str:
    """把绝对路径还原成工作区相对路径（用于返回给模型与接口）。"""

    relative = path.resolve().relative_to(resolve_root(root))
    text = relative.as_posix()
    return "" if text == "." else text

"""工作区错误族（ADR-033）。

错误码与 `doc/api.md` §5.19 一一对应；API 层按类型映射状态码，不做字符串匹配
（与 ADR-009 修订 3 的口径一致）。
"""

from __future__ import annotations


class WorkspaceError(ValueError):
    """工作区操作的基类；未细分的取值问题归类为 422。"""


class WorkspacePathError(WorkspaceError):
    """路径非法或越出工作区（§5.19 的 `WORKSPACE_PATH_REJECTED`）。"""

    code = "WORKSPACE_PATH_REJECTED"


class WorkspaceModeUnavailable(WorkspaceError):
    """档位暂未开放。阶段 1 只提供 `read_only`（ADR-033 §2）。"""

    code = "WORKSPACE_MODE_UNAVAILABLE"


class WorkspaceNotFoundError(WorkspaceError):
    """工作区不存在。"""

    code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"工作区不存在：{workspace_id}")
        self.workspace_id = workspace_id


class WorkspaceExistsError(WorkspaceError):
    """同一个相对路径已经登记过（表上有唯一约束）。"""

    code = "WORKSPACE_EXISTS"


class WorkspaceRootUnavailable(RuntimeError):
    """工作区根不可用：未配置、路径不存在或不是目录（§5.19 的 503）。"""

    code = "WORKSPACE_ROOT_UNAVAILABLE"


class WorkspaceDisabled(WorkspaceRootUnavailable):
    """`WORKSPACE_ENABLED=false` 时的显式失败，避免"静默不生效"。"""

    code = "WORKSPACE_DISABLED"

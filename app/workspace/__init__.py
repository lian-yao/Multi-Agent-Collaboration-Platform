"""工作区（work_dir）：路径守卫、登记与只读视图（ADR-033）。

对外只暴露服务层函数与错误族；路径规则在 `app/workspace/paths.py`，
配置在 `app/workspace/config.py`，表结构在 `app/core/checkpoint.py`。
"""

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
from app.workspace.paths import normalize_relative, resolve_in_workspace, resolve_root
from app.workspace.service import (
    LocalWorkspaceSource,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    update_workspace,
    workspace_source,
    workspace_tree,
)

__all__ = [
    "LocalWorkspaceSource",
    "WorkspaceApprovalRequired",
    "WorkspaceDisabled",
    "WorkspaceError",
    "WorkspaceExistsError",
    "WorkspaceNotFoundError",
    "WorkspacePathError",
    "WorkspaceQuotaExceeded",
    "WorkspaceRootUnavailable",
    "WorkspaceSettings",
    "create_workspace",
    "delete_workspace",
    "get_workspace",
    "get_workspace_settings",
    "list_workspaces",
    "normalize_relative",
    "resolve_in_workspace",
    "resolve_root",
    "update_workspace",
    "workspace_source",
    "workspace_tree",
]

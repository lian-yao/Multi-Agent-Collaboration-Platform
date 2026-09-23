"""工作区配置（ADR-033，环境变量前缀 `WORKSPACE_`）。

`root` 是**容器内**的挂载点（默认 `/workspace`）；宿主要挂到哪里由部署层的
`WORKSPACE_HOST_ROOT` 决定，见 `doc/deployment.md`。宿主绝对路径不进数据库
（`doc/data-model.md` §3.3），否则改一次部署根就会让历史行失效。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkspaceSettings(BaseSettings):
    """工作区运行时参数。"""

    model_config = SettingsConfigDict(
        env_prefix="WORKSPACE_",
        env_file=".env",
        extra="ignore",
    )

    enabled: bool = True
    """关闭后所有工作区接口返回 503 `WORKSPACE_DISABLED`，不静默降级成"没有工作区"。"""

    root: str = "/workspace"
    """工作区根（容器内路径）。宿主机直跑后端时改成宿主目录的绝对路径。"""

    max_file_bytes: int = 5 * 1024 * 1024
    """单文件大小上限。与附件侧同量级（`app/attachments/spec.py`），读超过即拒绝。"""

    max_total_bytes: int = 256 * 1024 * 1024
    """目录总字节配额，用于 `usage` 展示与写档位（阶段 2）的准入判断。"""

    max_entries: int = 2000
    """目录条目配额（含子目录）。"""

    read_max_chars: int = 20_000
    """单次读取返回的字符上限；与 ADR-025 的会话文件读取同量级。"""

    tree_max_entries: int = 500
    """目录树单次返回的条目上限，超出时 `truncated=true`。"""

    tree_max_depth: int = 8
    """目录树递归深度上限。"""

    scan_limit: int = 10_000
    """`usage` 扫描的条目上限：工作区被塞进海量小文件时，接口不允许被一次统计拖死。"""

    delete_trash_dir: str = ".trash"
    """软删除目录名（阶段 2 的删除工具使用；阶段 1 只用于跳过它，不列给模型）。"""

    approval_ttl_seconds: int = 900
    """审批的有效期。超时未决策的 `pending` 会被标成 `expired`：不放行，也不删记录。"""

    import_max_files: int = 200
    """单次导入的文件数上限（浏览器「选择文件夹」会按这个分片上传）。"""

    import_max_file_bytes: int = 20 * 1024 * 1024
    """单个导入文件的字节上限。比附件（5 MB）宽：导入是**存盘**而不是塞进模型上下文，
    但要挡住"一个 2 GB 的视频把请求体和配额一起打爆"这种情况。"""


@lru_cache
def get_workspace_settings() -> WorkspaceSettings:
    return WorkspaceSettings()

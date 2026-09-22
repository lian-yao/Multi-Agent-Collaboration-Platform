"""沙箱配置（成员 C D7-8）。

对齐事实源：

- `doc/15 AI Native多智能体协作平台.md` 模块 3「代码执行（Python/Shell在沙箱中运行）」
  与 §五「工具调用的安全性」：对代码执行等敏感工具实现沙箱隔离（Docker 容器或 Wasm 运行时），
  限制系统资源访问和网络权限；
- `doc/architecture.md` 模块划分：`app/sandbox` 负责「敏感工具执行边界」。

环境变量统一使用 `SANDBOX_` 前缀，例如 `SANDBOX_BACKEND=denied`、`SANDBOX_TIMEOUT_SECONDS=20`。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

SandboxBackend = Literal["docker", "denied"]
WorkspaceMountMode = Literal["rw", "ro", "none"]


class SandboxSettings(BaseSettings):
    """沙箱运行时配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_",
        env_file=".env",
        extra="ignore",
    )

    backend: SandboxBackend = "docker"
    """执行后端：`docker` 为容器隔离；`denied` 直接拒绝执行（无 Docker 环境时的显式降级）。"""

    image: str = "python:3.12-slim"
    """沙箱容器镜像。

    默认是官方 slim 镜像——**小、无本项目代码**，是正常网络环境下该用的。
    离线/内网部署拉不到它时，可指到本地已有镜像（`deploy/compose.yaml` 就指到
    本项目自建镜像，因为 compose 一定会把它构建出来）。
    """

    auto_pull_image: bool = False
    """镜像不在宿主机时是否自动拉取。

    默认关：拉取是一个可能长时间阻塞的网络动作（本机实测直连 Docker Hub 十分钟不返回），
    而沙箱是安全边界组件，**失败要快、原因要准**。开启后首次执行会自动拉一次。
    """

    timeout_seconds: int = 15
    memory_limit: str = "256m"
    cpu_limit: float = 0.5
    pids_limit: int = 64
    network_enabled: bool = False
    output_limit_chars: int = 4000
    max_code_chars: int = 20_000

    workspace_mount: WorkspaceMountMode = "rw"
    """沙箱怎么看到本次会话的工作区（ADR-033 §7）。

    `rw`（默认）：沙箱代码能直接读写工作区；`ro`：只读挂载，写只能走工作区文件工具
    （**审批才能拦得住**）；`none`：完全不挂，沙箱里看不到用户文件。

    另外：只读档位（`read_only`）的工作区**一律按 `ro` 挂**，与这里的取值无关——
    档位是人的授权，不该被代码执行绕过。
    """

    workspace_host_root: str = ""
    """宿主侧工作区根路径的显式覆盖。

    沙箱容器建在**宿主**守护进程上（ADR-023），bind 的来源必须是宿主路径，而 backend
    看到的是挂载点（如 `/workspace`）。留空时从 `/proc/self/mountinfo` 反查；
    反查不到（非 bind mount、特殊环境）时用这个变量显式指定。
    """

    uid: int | None = None
    gid: int | None = None
    """沙箱进程的 uid/gid。默认跑 `nobody`。

    Linux 宿主上要让沙箱写进宿主用户的目录，通常得把这里设成宿主用户（例如 1000:1000），
    否则 `nobody` 没有写权限；Docker Desktop 的 bind mount 由虚拟文件系统接管，影响有限。
    """

    egress_network: str = ""
    """允许沙箱联网时要接入的**内部**网络名（compose 网络的完整名字）。

    `SANDBOX_NETWORK_ENABLED=true` 且这里非空时，沙箱只接这一张网；这张网在 compose 里是
    `internal: true`（没有默认路由），所以它唯一的出口就是同一张网上的 egress 代理——
    "沙箱能联网"于是等于"沙箱能经代理访问公网"，而不是"沙箱直接连出去"。
    """

    egress_proxy_url: str = ""
    """注入给沙箱的 `HTTP_PROXY` / `HTTPS_PROXY`。

    沙箱里的代码要联网就得走代理；`NO_PROXY` 固定排除回环，避免把沙箱自己的 localhost
    也绕进代理。
    """

    egress_no_proxy: str = "localhost,127.0.0.1,::1"
    """沙箱的 `NO_PROXY`（默认只排除回环）。"""


@lru_cache
def get_sandbox_settings() -> SandboxSettings:
    return SandboxSettings()

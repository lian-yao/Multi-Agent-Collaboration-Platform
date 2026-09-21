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


@lru_cache
def get_sandbox_settings() -> SandboxSettings:
    return SandboxSettings()

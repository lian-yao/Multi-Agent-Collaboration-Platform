"""沙箱运行时边界（成员 C D7-8）。

`Sandbox` 是敏感工具（代码执行）唯一允许的执行入口：先做语言层策略校验
（`app.sandbox.policy`），再交给具体后端。后端由 `SANDBOX_BACKEND` 选择：

- `docker`：容器隔离（`app.sandbox.docker_runtime.DockerSandbox`）；
- `denied`：显式拒绝执行，用于没有 Docker 的本地环境，避免静默降级为宿主进程执行。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` §五「工具调用的安全性」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.sandbox.config import SandboxSettings, get_sandbox_settings
from app.sandbox.policy import (
    SandboxViolation,
    check_python_source,
    check_shell_command,
    normalize_language,
)


class SandboxUnavailable(RuntimeError):
    """沙箱后端不可用（例如没有 Docker），调用方应把工具标记为 failed。"""


@dataclass(frozen=True)
class SandboxWorkspace:
    """挂在沙箱里的工作区（ADR-033 §7）。

    `container_path` 是 backend 侧的路径（如 `/workspace`）；真正的 bind 来源由
    `app/sandbox/workspace_bind.py` 翻译成宿主路径。`mode` 取 `rw` / `ro`。

    `host_path` 是**已经知道**的 bind 来源（宿主绝对路径）。宿主直跑形态（ADR-035）下必须给：
    那时 backend 就在宿主上，工作区路径本身就是宿主路径，既没有 bind mount 可反查、
    `container_path` 也可能是 Windows 盘符——把盘符当 Linux 容器的挂载点会挂出一个空目录。
    因此那种形态下 `container_path` 固定用 `/workspace`（沙箱内看到的位置），
    `host_path` 放真正的来源。
    """

    container_path: str
    mode: str = "rw"
    host_path: str | None = None


@dataclass(frozen=True)
class SandboxResult:
    """一次沙箱执行的结果；输出已按 `output_limit_chars` 截断。"""

    backend: str
    language: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class Sandbox(Protocol):
    """敏感代码的执行后端。"""

    name: str

    def available(self) -> bool: ...

    def unavailable_reason(self) -> str | None:
        """不可用的**具体原因**；可用时返回 `None`。

        为什么不只留 `available()`：界面上的「执行边界」分区要把「为什么不可用」
        显示出来（`doc/api.md` §5.15）。只回布尔值时调用方只能写一句猜测的兜底文案，
        真实原因（套接字没挂 / 镜像不在宿主机 / 根本装不上 Docker SDK）就丢了。
        """

        ...

    def run(
        self,
        code: str,
        *,
        language: str = "python",
        workspace: SandboxWorkspace | None = None,
    ) -> SandboxResult: ...


class DeniedSandbox:
    """拒绝一切执行的兜底后端：宁可让工具失败，也不在宿主进程里跑用户代码。"""

    name = "denied"

    def __init__(self, reason: str = "沙箱未启用（SANDBOX_BACKEND=denied）") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str | None:
        return self._reason

    def run(
        self,
        code: str,
        *,
        language: str = "python",
        workspace: SandboxWorkspace | None = None,
    ) -> SandboxResult:
        raise SandboxUnavailable(self._reason)


def build_sandbox(settings: SandboxSettings | None = None) -> Sandbox:
    """按配置构建沙箱后端；`docker` 后端延迟导入，避免无谓依赖 Docker SDK。"""

    resolved = settings or get_sandbox_settings()
    if resolved.backend == "denied":
        return DeniedSandbox()

    from app.sandbox.docker_runtime import DockerSandbox

    return DockerSandbox(resolved)


def run_in_sandbox(
    code: str,
    *,
    language: str = "python",
    sandbox: Sandbox | None = None,
    settings: SandboxSettings | None = None,
    workspace: SandboxWorkspace | None = None,
) -> SandboxResult:
    """策略校验 + 沙箱执行的统一入口。

    策略拒绝抛 `SandboxViolation`（代码本身越权，调用方不应重试）；
    后端不可用抛 `SandboxUnavailable`（环境问题，重试无意义但原因不同）。
    """

    resolved_settings = settings or get_sandbox_settings()
    resolved_language = normalize_language(language)
    if len(code) > resolved_settings.max_code_chars:
        raise SandboxViolation(
            f"代码长度 {len(code)} 超过上限 {resolved_settings.max_code_chars}"
        )

    if resolved_language == "python":
        # 联网开关同时决定"能不能写网络代码"：默认禁网时连 urllib/httpx 一起禁。
        check_python_source(code, allow_network=resolved_settings.network_enabled)
    else:
        check_shell_command(code)

    backend = sandbox if sandbox is not None else build_sandbox(resolved_settings)
    return backend.run(code, language=resolved_language, workspace=workspace)

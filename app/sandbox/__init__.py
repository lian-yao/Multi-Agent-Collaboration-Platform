"""敏感工具的执行沙箱：策略校验 + 隔离后端（见 `doc/architecture.md` 模块划分）。"""

from app.sandbox.config import SandboxSettings, get_sandbox_settings
from app.sandbox.policy import (
    SandboxViolation,
    check_python_source,
    check_shell_command,
    normalize_language,
)
from app.sandbox.runtime import (
    DeniedSandbox,
    Sandbox,
    SandboxResult,
    SandboxUnavailable,
    build_sandbox,
    run_in_sandbox,
)

__all__ = [
    "DeniedSandbox",
    "Sandbox",
    "SandboxResult",
    "SandboxSettings",
    "SandboxUnavailable",
    "SandboxViolation",
    "build_sandbox",
    "check_python_source",
    "check_shell_command",
    "get_sandbox_settings",
    "normalize_language",
    "run_in_sandbox",
]

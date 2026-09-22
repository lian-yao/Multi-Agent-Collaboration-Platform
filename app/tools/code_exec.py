"""代码执行工具（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3 示例工具集
「代码执行（Python/Shell在沙箱中运行）」与 §五「工具调用的安全性」。

职责边界：本工具是 `app.sandbox` 的调用方，自己不实现隔离——策略拒绝
（`SandboxViolation`）与后端不可用（`SandboxUnavailable`）都归一化为
`ToolExecutionError`，由编排层记录为 failed 调用而不中断流水线（ADR-009）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.sandbox.config import SandboxSettings, get_sandbox_settings
from app.sandbox.policy import SandboxViolation
from app.sandbox.runtime import (
    Sandbox,
    SandboxUnavailable,
    SandboxWorkspace,
    run_in_sandbox,
)
from app.tools.base import BuiltinTool, ToolExecutionError

__all__ = [
    "CodeExecutionArgs",
    "CodeExecutionTool",
    "code_execution_tool",
]


class CodeExecutionArgs(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=20_000,
        description="要执行的代码片段（Python 或 Shell）",
    )
    language: Literal["python", "shell"] = Field(
        default="python", description="代码语言"
    )


class CodeExecutionTool(BuiltinTool):
    name = "code_execution"
    description = (
        "在隔离沙箱中运行 Python 或 Shell 代码片段并返回标准输出与标准错误；"
        "沙箱禁用网络、限制内存/CPU/进程数并强制超时，越权代码会被策略拒绝。"
    )
    args_model = CodeExecutionArgs

    def __init__(
        self,
        sandbox: Sandbox | None = None,
        *,
        sandbox_settings: SandboxSettings | None = None,
        workspace: SandboxWorkspace | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._settings = sandbox_settings or get_sandbox_settings()
        self._workspace = workspace

    def run(self, args: CodeExecutionArgs) -> dict[str, Any]:
        try:
            result = run_in_sandbox(
                args.code,
                language=args.language,
                sandbox=self._sandbox,
                settings=self._settings,
                workspace=self._workspace,
            )
        except SandboxViolation as exc:
            # 策略拒绝由代码内容决定，重试同样会被拒绝（ADR-009 修订 3）。
            raise ToolExecutionError(f"沙箱策略拒绝: {exc}", retryable=False) from exc
        except SandboxUnavailable as exc:
            raise ToolExecutionError(f"沙箱不可用: {exc}") from exc
        return {
            "backend": result.backend,
            "language": result.language,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }


def code_execution_tool(
    session_id: str | None,
    *,
    sandbox: Sandbox | None = None,
    sandbox_settings: SandboxSettings | None = None,
) -> CodeExecutionTool | None:
    """构造「看得见本次会话工作区」的 `code_execution`（ADR-033 §7）。

    没有绑定工作区、或 `SANDBOX_WORKSPACE_MOUNT=none` 时返回 `None`——那时沿用进程级
    那个内置实例，沙箱里看不到任何用户文件，行为与之前完全一致。

    挂载模式由**档位**决定：只读档位一律 `ro`（代码能读能算，写不了）；写档位才 `rw`。
    这样"能不能写"始终由人控制的档位决定，而不是由代码自己决定。
    """

    resolved = sandbox_settings or get_sandbox_settings()
    if not session_id or resolved.workspace_mount == "none":
        return None

    from app.workspace import workspace_source

    source = workspace_source(str(session_id))
    if source is None:
        return None
    try:
        container_path = str(source.root())
    except Exception:  # 工作区不可用：退回不挂载的沙箱，而不是让工具消失
        return None
    mode = (
        "rw"
        if source.mode == "workspace_write" and resolved.workspace_mount == "rw"
        else "ro"
    )
    return CodeExecutionTool(
        sandbox,
        sandbox_settings=resolved,
        workspace=SandboxWorkspace(container_path=container_path, mode=mode),
    )

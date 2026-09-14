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
from app.sandbox.runtime import Sandbox, SandboxUnavailable, run_in_sandbox
from app.tools.base import BuiltinTool, ToolExecutionError


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
    ) -> None:
        self._sandbox = sandbox
        self._settings = sandbox_settings or get_sandbox_settings()

    def run(self, args: CodeExecutionArgs) -> dict[str, Any]:
        try:
            result = run_in_sandbox(
                args.code,
                language=args.language,
                sandbox=self._sandbox,
                settings=self._settings,
            )
        except SandboxViolation as exc:
            raise ToolExecutionError(f"沙箱策略拒绝: {exc}") from exc
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

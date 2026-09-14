"""Docker 沙箱后端（成员 C D7-8）。

隔离边界（逐个对应 `doc/15 ...平台.md` §五「限制系统资源访问和网络权限」）：

| 措施 | 代码 |
| --- | --- |
| 禁用网络 | `network_disabled=True` |
| 只读根文件系统 + 有限可写临时目录 | `read_only=True`、`tmpfs={'/tmp': ...}` |
| 内存/CPU/进程数上限 | `mem_limit`、`nano_cpus`、`pids_limit` |
| 禁止提权 | `security_opt=['no-new-privileges']`、非 root 用户 |
| 执行超时 | `container.wait(timeout=...)` 超时即 kill |

Docker 不可用时抛 `SandboxUnavailable`，由调用方标记为工具失败，
**不降级为宿主进程执行**（安全边界不做静默降级）。
"""

from __future__ import annotations

import logging
import time

from app.observability.logging import get_logger, log_event
from app.sandbox.config import SandboxSettings, get_sandbox_settings
from app.sandbox.runtime import SandboxResult, SandboxUnavailable

logger = get_logger("sandbox.docker")

TIMEOUT_EXIT_CODE = 124
TMPFS_SIZE_BYTES = 16 * 1024 * 1024


class DockerSandbox:
    """在一次性容器中执行代码。"""

    name = "docker"

    def __init__(self, settings: SandboxSettings | None = None) -> None:
        self._settings = settings or get_sandbox_settings()
        self._client = None

    def _docker(self):
        if self._client is None:
            try:
                import docker
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确原因
                raise SandboxUnavailable(f"Docker SDK 不可用: {exc}") from exc
            try:
                self._client = docker.from_env()
            except Exception as exc:
                raise SandboxUnavailable(f"Docker 守护进程不可用: {exc}") from exc
        return self._client

    def available(self) -> bool:
        try:
            self._docker().ping()
        except SandboxUnavailable:
            return False
        except Exception:
            return False
        return True

    def run(self, code: str, *, language: str = "python") -> SandboxResult:
        client = self._docker()
        command = _command(language, code)
        started = time.perf_counter()
        try:
            container = client.containers.run(
                self._settings.image,
                command,
                detach=True,
                network_disabled=not self._settings.network_enabled,
                mem_limit=self._settings.memory_limit,
                nano_cpus=int(self._settings.cpu_limit * 1_000_000_000),
                pids_limit=self._settings.pids_limit,
                read_only=True,
                tmpfs={"/tmp": f"size={TMPFS_SIZE_BYTES}"},
                security_opt=["no-new-privileges"],
                working_dir="/tmp",
                user="nobody",
                labels={"macp.role": "tool-sandbox"},
            )
        except SandboxUnavailable:
            raise
        except Exception as exc:
            raise SandboxUnavailable(f"启动沙箱容器失败: {exc}") from exc

        timed_out = False
        exit_code = TIMEOUT_EXIT_CODE
        try:
            status = container.wait(timeout=self._settings.timeout_seconds)
            exit_code = int(status.get("StatusCode", exit_code))
        except Exception as exc:  # docker SDK 等待超时时抛 requests 超时异常
            timed_out = True
            log_event(
                logger,
                "sandbox.timeout",
                level=logging.WARNING,
                timeout_seconds=self._settings.timeout_seconds,
                error=f"{type(exc).__name__}: {exc}",
            )
            _kill(container)
        finally:
            stdout, stderr, truncated = self._collect(container)
            _remove(container)

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log_event(
            logger,
            "sandbox.run",
            backend=self.name,
            language=language,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
        if timed_out and not stderr:
            stderr = f"执行超过 {self._settings.timeout_seconds}s，容器已终止"
        return SandboxResult(
            backend=self.name,
            language=language,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=truncated,
        )

    def _collect(self, container) -> tuple[str, str, bool]:
        """读取容器输出并按配置截断，避免超长输出撑爆工具观测与审计载荷。"""

        try:
            raw_stdout = container.logs(stdout=True, stderr=False)
            raw_stderr = container.logs(stdout=False, stderr=True)
        except Exception as exc:
            return "", f"读取容器输出失败: {exc}", False
        limit = self._settings.output_limit_chars
        stdout, stdout_cut = _truncate(raw_stdout.decode("utf-8", "replace"), limit)
        stderr, stderr_cut = _truncate(raw_stderr.decode("utf-8", "replace"), limit)
        return stdout, stderr, stdout_cut or stderr_cut


def _command(language: str, code: str) -> list[str]:
    if language == "python":
        return ["python", "-I", "-c", code]
    return ["sh", "-c", code]


def _kill(container) -> None:
    try:
        container.kill()
    except Exception:  # 容器可能已退出，忽略
        pass


def _remove(container) -> None:
    try:
        container.remove(force=True)
    except Exception:  # 清理失败不影响执行结果
        pass


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[输出已截断]", True

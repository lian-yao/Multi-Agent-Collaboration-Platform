"""Docker 沙箱后端（成员 C D7-8）。

隔离边界（逐个对应 `doc/15 ...平台.md` §五「限制系统资源访问和网络权限」）：

| 措施 | 代码 |
| --- | --- |
| 禁用网络 | `network_disabled=True` |
| 只读根文件系统 + 有限可写临时目录 | `read_only=True`、`tmpfs={'/tmp': ...}` |
| 内存/CPU/进程数上限 | `mem_limit`、`nano_cpus`、`pids_limit` |
| 禁止提权 | `security_opt=['no-new-privileges']`、非 root 用户、`cap_drop=['ALL']` |
| 执行超时 | `container.wait(timeout=...)` 超时即 kill |
| 不携带平台自身源码 | 镜像可能复用本项目镜像，用 tmpfs 遮住 `/app` |

**容器建在宿主机的守护进程上**：backend 容器挂载宿主机套接字（`deploy/compose.yaml`），
沙箱容器因此是 backend 的**兄弟容器**而不是子容器（ADR-023）。这带来一个必须检查的
前置条件——沙箱镜像必须先存在于**宿主机**，`unavailable_reason()` 会把这件事查出来；
只 `ping()` 不看镜像的探测会给出「绿灯但第一次执行就失败」的假象。

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

MASKED_APP_TMPFS = "size=1m"
"""遮住 `/app` 的空 tmpfs。

离线/内网环境拉不到 `python:3.12-slim` 时，沙箱镜像会复用本项目自建镜像
（`SANDBOX_IMAGE`，见 ADR-023），那里面有平台自己的源码。往里挂一个空 tmpfs 就把它遮住：
越权代码读不到平台代码，根文件系统依旧只读。镜像本来没有 `/app` 时无副作用。
"""

DOCKER_SOCKET_HINT = (
    "沙箱通过**宿主机**的 Docker 守护进程创建一次性容器，所以 backend 容器必须挂载该套接字"
    "（`deploy/compose.yaml` 里 backend 的 `volumes` 已含 "
    "`/var/run/docker.sock:/var/run/docker.sock`）。另需注意：宿主机上该路径不存在时，"
    "Docker 会创建一个同名**目录**而不是报错，此时套接字实际并不存在，"
    "请用 SANDBOX_DOCKER_SOCKET 指向真实套接字（例如 Podman 的 /run/podman/podman.sock）。"
)

IMAGE_MISSING_HINT = (
    "沙箱容器创建在**宿主机**的守护进程上，镜像必须先在宿主机上可用。"
    "可先在宿主机执行 `docker pull {image}`；离线环境请把 SANDBOX_IMAGE 指向本地已有镜像"
    "（`deploy/compose.yaml` 默认指向本项目自建镜像）；"
    "或设 SANDBOX_AUTO_PULL_IMAGE=1 让沙箱首次执行时自行拉取。"
)


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
                # 连异常**类型**一起写进消息：`FileNotFoundError(2, ...)` 字符串化后是
                # `[Errno 2] No such file or directory`，不带类名——只看这句话无法判断
                # 是套接字缺失还是权限不足。
                raise SandboxUnavailable(
                    f"Docker 守护进程不可用: {type(exc).__name__}: {exc}"
                ) from exc
        return self._client

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """不可用的具体原因；可用时 `None`。探测两件事，缺一不可：

        1. 守护进程可达（`ping`）；
        2. 沙箱镜像已在宿主机上（`images.get`）——容器建在宿主机的守护进程上，
           镜像不在时若回「可用」，界面就是假绿灯，要等真的执行代码才失败。
        """

        try:
            client = self._docker()
            client.ping()
        except SandboxUnavailable as exc:
            return f"{exc}\n{DOCKER_SOCKET_HINT}"
        except Exception as exc:
            return (
                f"Docker 守护进程不可达: {type(exc).__name__}: {exc}\n{DOCKER_SOCKET_HINT}"
            )

        import docker

        try:
            client.images.get(self._settings.image)
        except docker.errors.ImageNotFound:
            if self._settings.auto_pull_image:
                # 已允许自拉，首次执行会补上，不算不可用。
                return None
            return f"沙箱镜像 {self._settings.image} 不在宿主机上。" + (
                IMAGE_MISSING_HINT.format(image=self._settings.image)
            )
        except Exception as exc:
            return f"查询沙箱镜像失败: {type(exc).__name__}: {exc}"
        return None

    def run(self, code: str, *, language: str = "python") -> SandboxResult:
        client = self._docker()
        command = _command(language, code)
        started = time.perf_counter()
        container = self._create(client, command)

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

    def _create(self, client, command: list[str]):
        """创建一次性沙箱容器。镜像缺失时给出**可行动**的错误而不是 404 原文。"""

        options: dict[str, object] = {
            "detach": True,
            "network_disabled": not self._settings.network_enabled,
            "mem_limit": self._settings.memory_limit,
            "nano_cpus": int(self._settings.cpu_limit * 1_000_000_000),
            "pids_limit": self._settings.pids_limit,
            "read_only": True,
            "tmpfs": {
                "/tmp": f"size={TMPFS_SIZE_BYTES}",
                "/app": MASKED_APP_TMPFS,
            },
            "security_opt": ["no-new-privileges"],
            # 默认能力集里有 CHOWN / SETUID / NET_RAW 等，而沙箱里一样都用不上：
            # 它是一个 `nobody` 无权、只读、无网的一次性容器，全部丢掉。
            "cap_drop": ["ALL"],
            "working_dir": "/tmp",
            "user": "nobody",
            "labels": {"macp.role": "tool-sandbox"},
        }

        import docker

        try:
            return client.containers.run(self._settings.image, command, **options)
        except docker.errors.ImageNotFound as exc:
            if not self._settings.auto_pull_image:
                raise SandboxUnavailable(
                    f"沙箱镜像 {self._settings.image} 不在宿主机上。"
                    + IMAGE_MISSING_HINT.format(image=self._settings.image)
                ) from exc
            log_event(logger, "sandbox.pull_image", image=self._settings.image)
            try:
                client.images.pull(self._settings.image)
            except Exception as pull_exc:
                raise SandboxUnavailable(
                    f"拉取沙箱镜像 {self._settings.image} 失败: "
                    f"{type(pull_exc).__name__}: {pull_exc}"
                ) from pull_exc
            try:
                return client.containers.run(self._settings.image, command, **options)
            except Exception as retry_exc:
                raise SandboxUnavailable(
                    f"启动沙箱容器失败: {type(retry_exc).__name__}: {retry_exc}"
                ) from retry_exc
        except SandboxUnavailable:
            raise
        except Exception as exc:
            raise SandboxUnavailable(f"启动沙箱容器失败: {exc}") from exc

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

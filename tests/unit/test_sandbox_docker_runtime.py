"""`DockerSandbox` 的可用性探测与容器硬化（ADR-023）。

用假 client 覆盖真正会出错、又最难在真机上复现的几条路径：

- 守护进程连不上（套接字没挂）——原因里必须点到套接字，否则用户只能去翻容器日志；
- 守护进程连得上、但**镜像不在宿主机**——这正是「探测说可用、第一次执行才失败」的
  假绿灯；容器建在宿主机守护进程上，所以探测必须自己查这件事；
- 镜像缺失时给的是可行动的错误，而不是 docker 库的 `ImageNotFound` 原文。

真实的沙箱执行要守护进程，不在这里跑（见 `doc/testing.md` §4）。
"""

from __future__ import annotations

import docker
import pytest

from app.sandbox.config import SandboxSettings
from app.sandbox.docker_runtime import DockerSandbox
from app.sandbox.runtime import SandboxUnavailable


class _Images:
    """镜像库替身。`missing` 是唯一事实源——`pull()` 成功后就地翻成 False，
    这样「自拉后重试」测的是真链路，而不是被替身自己的状态卡住。"""

    def __init__(self, *, missing: bool, pull_error: Exception | None = None) -> None:
        self.missing = missing
        self.pull_error = pull_error
        self.pulled: list[str] = []

    def get(self, image: str):
        if self.missing:
            raise docker.errors.ImageNotFound(f"No such image: {image}")
        return {"Id": "sha256:fake"}

    def pull(self, image: str):
        self.pulled.append(image)
        if self.pull_error is not None:
            raise self.pull_error
        self.missing = False
        return {}


class _Container:
    def __init__(self) -> None:
        self.removed = False

    def wait(self, timeout: int | None = None):
        return {"StatusCode": 0}

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        return b"ok"

    def kill(self) -> None:  # pragma: no cover - 只在超时路径上被调用
        pass

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _Containers:
    def __init__(self, images: _Images) -> None:
        self._images = images
        self.commands: list[list[str]] = []
        self.options: list[dict] = []

    def run(self, image: str, command, **options):
        if self._images.missing:
            raise docker.errors.ImageNotFound(f"No such image: {image}")
        self.commands.append(list(command))
        self.options.append(dict(options))
        return _Container()


class _Client:
    def __init__(
        self,
        *,
        ping_error: Exception | None = None,
        image_missing: bool = False,
        pull_error: Exception | None = None,
    ) -> None:
        self._ping_error = ping_error
        self.images = _Images(missing=image_missing, pull_error=pull_error)
        self.containers = _Containers(self.images)
        self.pinged = 0

    def ping(self):
        self.pinged += 1
        if self._ping_error is not None:
            raise self._ping_error
        return True


def _sandbox(client: _Client, **overrides) -> DockerSandbox:
    sandbox = DockerSandbox(SandboxSettings(image="sandbox:test", **overrides))
    sandbox._client = client  # 注入替身：跳过 docker.from_env()
    return sandbox


# --------------------------------------------------------------------------------------
# 可用性探测
# --------------------------------------------------------------------------------------


def test_from_env_failure_reason_points_at_the_socket(monkeypatch) -> None:
    """套接字没挂时，原因里必须有「怎么办」，不只是异常类名。"""

    def boom():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(docker, "from_env", boom)
    sandbox = DockerSandbox(SandboxSettings(image="sandbox:test"))

    reason = sandbox.unavailable_reason()

    assert reason is not None
    assert "FileNotFoundError" in reason
    assert "docker.sock" in reason
    assert sandbox.available() is False


def test_available_requires_the_image_on_the_daemon() -> None:
    """只 ping 通不算可用：镜像不在宿主机时第一次执行必然失败。"""

    sandbox = _sandbox(_Client(image_missing=True))

    assert sandbox.available() is False
    reason = sandbox.unavailable_reason()
    assert reason is not None and "不在宿主机上" in reason


def test_missing_image_is_not_a_blocker_when_auto_pull_is_on() -> None:
    sandbox = _sandbox(_Client(image_missing=True), auto_pull_image=True)

    assert sandbox.available() is True
    assert sandbox.unavailable_reason() is None


def test_daemon_error_other_than_sandbox_unavailable_is_reported() -> None:
    """`ping` 抛的不是我们自己的异常时也不能变成一句「不可用」。"""

    sandbox = _sandbox(_Client(ping_error=RuntimeError("connection refused")))

    reason = sandbox.unavailable_reason()

    assert reason is not None
    assert "connection refused" in reason


# --------------------------------------------------------------------------------------
# 启动容器
# --------------------------------------------------------------------------------------


def test_run_reports_actionable_error_when_image_is_missing() -> None:
    sandbox = _sandbox(_Client(image_missing=True))

    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run("print(1)")

    message = str(excinfo.value)
    assert "sandbox:test" in message
    assert "SANDBOX_IMAGE" in message  # 给出可执行的动作，而不是 404 原文


def test_run_pulls_once_and_retries_when_auto_pull_is_on() -> None:
    client = _Client(image_missing=True)
    sandbox = _sandbox(client, auto_pull_image=True)

    result = sandbox.run("print(1)")

    assert result.exit_code == 0
    assert client.images.pulled == ["sandbox:test"]
    assert len(client.containers.commands) == 1


def test_pull_failure_is_reported_as_unavailable() -> None:
    client = _Client(image_missing=True, pull_error=RuntimeError("no route to host"))
    sandbox = _sandbox(client, auto_pull_image=True)

    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run("print(1)")

    assert "no route to host" in str(excinfo.value)


def test_container_options_are_hardened() -> None:
    """逐条钉住隔离参数：这些是安全边界，被人顺手改掉要立刻红。"""

    client = _Client()
    sandbox = _sandbox(client)

    sandbox.run("print(1)", language="python")

    options = client.containers.options[0]
    assert options["cap_drop"] == ["ALL"]
    assert options["read_only"] is True
    assert options["network_disabled"] is True
    assert options["user"] == "nobody"
    assert options["security_opt"] == ["no-new-privileges"]
    assert options["working_dir"] == "/tmp"
    assert options["labels"] == {"macp.role": "tool-sandbox"}
    assert options["pids_limit"] == 64
    # 沙箱镜像在离线环境会复用本项目镜像，那时镜像里有平台源码；遮住它。
    assert "/app" in options["tmpfs"]
    assert "/tmp" in options["tmpfs"]
    assert client.containers.commands[0] == ["python", "-I", "-c", "print(1)"]


def test_shell_language_uses_sh_c() -> None:
    client = _Client()

    _sandbox(client).run("echo hi", language="shell")

    assert client.containers.commands[0] == ["sh", "-c", "echo hi"]

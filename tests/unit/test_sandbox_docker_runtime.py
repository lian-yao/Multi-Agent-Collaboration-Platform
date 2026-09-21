"""成员 C D7-8：Docker 沙箱后端的隔离参数与失败语义。

`tests/unit/test_sandbox_policy.py` 覆盖的是**策略层**（哪些代码被拒绝）与 `denied`
后端；真正的容器后端 `app/sandbox/docker_runtime.py` 此前只靠人工核对配置
（该文件第 11-12 行原话）。这里用一个假 Docker 客户端把它变成可回归的断言：

- **隔离参数**：`network_disabled` / `read_only` / `tmpfs` / `mem_limit` /
  `nano_cpus` / `pids_limit` / `security_opt` / 非 root 用户，任何一个被改掉都会红。
  这些参数是 `doc/15 AI Native多智能体协作平台.md` §五「限制系统资源访问和网络权限」
  的落地形态，属于安全边界，必须有守卫。
- **fail closed**：Docker SDK 缺失、守护进程不可达、容器启动失败、等待超时，
  四种失败都归一化成 `SandboxUnavailable`，**绝不退回宿主机进程执行**。
- **清理**：容器在等待超时后也要被 kill + remove，不留孤儿容器。

不需要 Docker：假客户端注入在 `sys.modules["docker"]` 上，容器对象是纯 Python 替身。
"""

from __future__ import annotations

import sys
import types

import pytest

from app.sandbox.config import SandboxSettings
from app.sandbox.docker_runtime import TIMEOUT_EXIT_CODE, DockerSandbox
from app.sandbox.runtime import SandboxUnavailable

CODE = "print(6 * 7)"


class ImageNotFound(Exception):
    """替身：`docker.errors.ImageNotFound` 的进程内等价，注入到假 docker 模块。

    成员 D 的 `docker_runtime.py` 用 `docker.errors.ImageNotFound` 兜住镜像缺失；
    master 的假 docker 模块（`_install_fake_docker`）原先不提供 `errors`，合并后
    这里补一个最小替身，让「镜像缺失」这条分支照样可回归。
    """


class FakeContainer:
    """`docker.models.containers.Container` 的最小替身。"""

    def __init__(
        self,
        *,
        status_code: int = 0,
        stdout: bytes = b"42\n",
        stderr: bytes = b"",
        wait_error: Exception | None = None,
        logs_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.stdout = stdout
        self.stderr = stderr
        self.wait_error = wait_error
        self.logs_error = logs_error
        self.waited: list[float | None] = []
        self.killed = 0
        self.removed: list[bool] = []

    def wait(self, timeout: float | None = None) -> dict[str, int]:
        self.waited.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        return {"StatusCode": self.status_code}

    def logs(self, *, stdout: bool = True, stderr: bool = False) -> bytes:
        if self.logs_error is not None:
            raise self.logs_error
        return self.stdout if stdout else self.stderr

    def kill(self) -> None:
        self.killed += 1

    def remove(self, force: bool = False) -> None:
        self.removed.append(force)


class _FakeImages:
    """`docker.models.images` 的最小替身：默认认为镜像已在宿主机上。

    成员 D 的 `available()` 会探测 `images.get(image)`；master 的假客户端原先没有
    `images`，合并后补上，让「守护进程可达 + 镜像在位 → 可用」这条路径可回归。
    """

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing

    def get(self, name: str) -> str:
        if self.missing:
            raise ImageNotFound(f"No such image: {name}")
        return name


class FakeDockerClient:
    def __init__(
        self,
        container: FakeContainer | None = None,
        *,
        run_error: Exception | None = None,
        ping_error: Exception | None = None,
        image_missing: bool = False,
    ) -> None:
        self.container = container or FakeContainer()
        self.run_error = run_error
        self.ping_error = ping_error
        self.images = _FakeImages(missing=image_missing)
        self.run_calls: list[tuple[tuple, dict]] = []
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    @property
    def containers(self) -> FakeDockerClient:
        return self

    def run(self, *args, **kwargs):
        self.run_calls.append((args, kwargs))
        if self.run_error is not None:
            raise self.run_error
        return self.container


def _install_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeDockerClient | None = None,
    *,
    from_env_error: Exception | None = None,
    missing_sdk: bool = False,
) -> FakeDockerClient | None:
    """把 `docker.from_env()` 换成假客户端（或让 SDK 整个缺失）。"""

    if missing_sdk:
        monkeypatch.setitem(sys.modules, "docker", None)
        return None

    resolved = client or FakeDockerClient()

    def from_env(*_args, **_kwargs):
        if from_env_error is not None:
            raise from_env_error
        return resolved

    module = types.ModuleType("docker")
    module.from_env = from_env  # type: ignore[attr-defined]
    errors = types.ModuleType("docker.errors")
    errors.ImageNotFound = ImageNotFound  # 供 `docker_runtime.py` 的镜像缺失分支
    module.errors = errors  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "docker", module)
    monkeypatch.setitem(sys.modules, "docker.errors", errors)
    return resolved


def _settings(**overrides) -> SandboxSettings:
    base = {
        "backend": "docker",
        "image": "python:3.12-slim",
        "timeout_seconds": 7,
        "memory_limit": "256m",
        "cpu_limit": 0.5,
        "pids_limit": 64,
        "network_enabled": False,
        "output_limit_chars": 4000,
        "max_code_chars": 20_000,
    }
    base.update(overrides)
    return SandboxSettings(_env_file=None, **base)


# --- 隔离参数：安全边界的回归守卫 -------------------------------------------


def test_container_is_started_with_the_full_isolation_boundary(monkeypatch):
    """禁网 + 只读根 + 有限 tmpfs + 资源上限 + 禁提权 + 非 root。"""

    client = _install_fake_docker(monkeypatch)
    sandbox = DockerSandbox(_settings())

    sandbox.run(CODE, language="python")

    (_image, _command), kwargs = client.run_calls[0]
    assert _image == "python:3.12-slim"
    assert kwargs["network_disabled"] is True
    assert kwargs["read_only"] is True
    assert kwargs["tmpfs"] == {"/tmp": "size=16777216", "/app": "size=1m"}
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["user"] == "nobody"
    assert kwargs["working_dir"] == "/tmp"
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["nano_cpus"] == 500_000_000
    assert kwargs["pids_limit"] == 64
    assert kwargs["detach"] is True


def test_network_is_only_opened_when_configuration_explicitly_allows_it(monkeypatch):
    """默认禁网；`SANDBOX_NETWORK_ENABLED=true` 是唯一的放行路径。"""

    client = _install_fake_docker(monkeypatch)
    DockerSandbox(_settings(network_enabled=True)).run(CODE)

    _args, kwargs = client.run_calls[0]
    assert kwargs["network_disabled"] is False


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("python", ["python", "-I", "-c", CODE]),
        ("sh", ["sh", "-c", CODE]),
    ],
)
def test_command_pins_the_interpreter_mode(language, expected):
    """Python 走 `-I` 隔离模式：忽略 PYTHONPATH / 用户 site-packages，不读环境隐式配置。"""

    from app.sandbox.docker_runtime import _command

    assert _command(language, CODE) == expected


def test_execution_timeout_is_taken_from_configuration(monkeypatch):
    client = _install_fake_docker(monkeypatch)
    container = client.container

    DockerSandbox(_settings(timeout_seconds=7)).run(CODE)

    assert container.waited == [7]


# --- fail closed：四种失败都不落到宿主机 -------------------------------------


def test_missing_docker_sdk_is_reported_as_sandbox_unavailable(monkeypatch):
    _install_fake_docker(monkeypatch, missing_sdk=True)

    with pytest.raises(SandboxUnavailable, match="Docker SDK 不可用"):
        DockerSandbox(_settings()).run(CODE)


def test_unreachable_daemon_is_reported_as_sandbox_unavailable(monkeypatch):
    _install_fake_docker(monkeypatch, from_env_error=RuntimeError("no socket"))

    with pytest.raises(SandboxUnavailable, match="Docker 守护进程不可用"):
        DockerSandbox(_settings()).run(CODE)


def test_container_start_failure_is_reported_as_sandbox_unavailable(monkeypatch):
    _install_fake_docker(
        monkeypatch, FakeDockerClient(run_error=RuntimeError("image pull denied"))
    )

    with pytest.raises(SandboxUnavailable, match="启动沙箱容器失败"):
        DockerSandbox(_settings()).run(CODE)


def test_timeout_kills_the_container_and_is_never_reported_as_success(monkeypatch):
    """等待超时是**工具失败**：kill + 明确原因，绝不返回 exit_code=0。"""

    container = FakeContainer(wait_error=TimeoutError("read timed out"))
    _install_fake_docker(monkeypatch, FakeDockerClient(container))

    result = DockerSandbox(_settings(timeout_seconds=7)).run(CODE)

    assert container.killed == 1
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.succeeded is False
    assert "7s" in result.stderr
    assert container.removed == [True]


def test_container_is_removed_even_when_waiting_fails(monkeypatch):
    """清理在 `finally` 里：等待抛异常也不留孤儿容器。"""

    client = _install_fake_docker(
        monkeypatch, FakeDockerClient(FakeContainer(status_code=1))
    )

    DockerSandbox(_settings()).run(CODE)

    assert client.container.removed == [True]


def test_available_reflects_the_daemon_without_raising(monkeypatch):
    _install_fake_docker(monkeypatch, FakeDockerClient(ping_error=RuntimeError("down")))
    assert DockerSandbox(_settings()).available() is False

    _install_fake_docker(monkeypatch, FakeDockerClient())
    assert DockerSandbox(_settings()).available() is True


# --- 结果归一化 -------------------------------------------------------------


def test_exit_code_and_streams_are_reported(monkeypatch):
    container = FakeContainer(status_code=3, stdout=b"partial", stderr=b"boom")
    _install_fake_docker(monkeypatch, FakeDockerClient(container))

    result = DockerSandbox(_settings()).run(CODE)

    assert (result.backend, result.language) == ("docker", "python")
    assert (result.exit_code, result.stdout, result.stderr) == (3, "partial", "boom")
    assert result.truncated is False
    assert result.duration_ms >= 0
    assert result.succeeded is False


def test_oversized_output_is_truncated_and_flagged(monkeypatch):
    """超长输出会撑爆工具观测与审计载荷，必须截断并显式标记。"""

    container = FakeContainer(stdout=b"x" * 200, stderr=b"y" * 200)
    _install_fake_docker(monkeypatch, FakeDockerClient(container))

    result = DockerSandbox(_settings(output_limit_chars=50)).run(CODE)

    assert result.truncated is True
    assert len(result.stdout) < 200 and len(result.stderr) < 200
    assert "输出已截断" in result.stdout


def test_log_read_failure_still_returns_the_execution_result(monkeypatch):
    container = FakeContainer(status_code=1, logs_error=RuntimeError("gone"))
    _install_fake_docker(monkeypatch, FakeDockerClient(container))

    result = DockerSandbox(_settings()).run(CODE)

    assert result.stdout == ""
    assert "读取容器输出失败" in result.stderr
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 以下来自成员 D（ADR-023）：可用性探测与容器硬化的补充用例
# ---------------------------------------------------------------------------

import docker


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

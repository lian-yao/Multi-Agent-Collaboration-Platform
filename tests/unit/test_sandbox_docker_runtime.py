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


class FakeDockerClient:
    def __init__(
        self,
        container: FakeContainer | None = None,
        *,
        run_error: Exception | None = None,
        ping_error: Exception | None = None,
    ) -> None:
        self.container = container or FakeContainer()
        self.run_error = run_error
        self.ping_error = ping_error
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
    monkeypatch.setitem(sys.modules, "docker", module)
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
    assert kwargs["tmpfs"] == {"/tmp": "size=16777216"}
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

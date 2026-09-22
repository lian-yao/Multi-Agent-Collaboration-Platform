"""成员 C D7-8：沙箱边界拒绝越权（`doc/testing.md` §2.1 U-08）。

对齐 `doc/15 AI Native多智能体协作平台.md` §五「限制系统资源访问和网络权限」：

- 沙箱**在执行之前**拒绝网络访问、提权、破坏性命令与非白名单模块；
- 策略拒绝（`SandboxViolation`）与后端不可用（`SandboxUnavailable`）是两类错误，
  都被 `code_execution` 归一化为工具失败，不中断流水线；
- 沙箱不可用时**不降级为宿主进程执行**（安全边界不做静默降级）。

本文件只验证策略层与拒绝后端，因此不依赖 Docker，也不真正执行任何代码；
容器隔离参数（禁网、只读根文件系统、内存/CPU/进程数上限）与 fail closed 语义
由同目录的 `test_sandbox_docker_runtime.py` 用假 Docker 客户端守卫，
见 `doc/testing.md` §2.1 的 U-08 说明。
"""

from __future__ import annotations

import pytest

from app.sandbox import (
    DeniedSandbox,
    SandboxSettings,
    SandboxUnavailable,
    SandboxViolation,
    build_sandbox,
    check_python_source,
    check_shell_command,
    normalize_language,
    run_in_sandbox,
)
from app.tools import CodeExecutionTool, ToolExecutionError


class SpySandbox:
    """记录是否被调用；用来证明策略拒绝发生在启动沙箱之前。"""

    name = "spy"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def run(self, code, *, language="python", workspace=None):
        # 沙箱协议新增 `workspace`（ADR-033 §7）；替身跟着签名走，否则调用方一传就炸。
        self.calls.append((code, language))
        raise AssertionError("策略拒绝的代码不应该到达沙箱后端")


# --- Python 源码策略 -------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.system('ls')",
        "import socket",
        "import subprocess",
        "from subprocess import run",
        "import urllib.request",
        "__import__('os').system('ls')",
        "eval('1+1')",
        "exec('pass')",
        "compile('1', '<s>', 'eval')",
        "().__class__.__bases__[0].__subclasses__()",
        "globals()['__builtins__']",
        "input()",
    ],
)
def test_python_policy_rejects_escape_attempts(code):
    with pytest.raises(SandboxViolation):
        check_python_source(code)


def test_file_access_is_allowed_now_that_the_sandbox_mounts_the_workspace():
    """`open` 从禁止改为允许（ADR-033 §7）：文件边界由容器（只挂工作区）承担。

    这一条是**行为变更的显式记录**：以前 `open(...)` 会被策略拒绝，现在它能通过语言层
    检查——真正拦住"读到工作区之外"的是容器挂载与只读根文件系统。
    """

    check_python_source("open('report.md').read()")
    check_python_source("import pathlib\npathlib.Path('a.txt').write_text('x')")


def test_network_modules_are_only_allowed_when_networking_is_on():
    """联网开关同时决定"能不能写网络代码"（ADR-034 §4）：

    默认禁网时连 `urllib` 都禁；打开联网后放行 HTTP 客户端模块，
    但 `socket` 仍然禁止——那等于允许任意端口探测。
    """

    with pytest.raises(SandboxViolation):
        check_python_source("import urllib.request")

    check_python_source("import urllib.request", allow_network=True)
    check_python_source("import httpx", allow_network=True)

    with pytest.raises(SandboxViolation):
        check_python_source("import socket", allow_network=True)
    with pytest.raises(SandboxViolation):
        check_python_source("import subprocess", allow_network=True)


@pytest.mark.parametrize(
    "code",
    [
        "import math\nprint(math.sqrt(16))",
        "import json\nprint(json.dumps({'a': 1}))",
        "import statistics\nprint(statistics.mean([1, 2, 3]))",
        "from collections import Counter\nprint(Counter('aab'))",
        "print(sum(range(10)))",
        "import re\nprint(re.findall(r'\\d+', 'a1b2'))",
    ],
)
def test_python_policy_allows_pure_computation(code):
    check_python_source(code)


@pytest.mark.parametrize("code", ["", "   \n  "])
def test_python_policy_rejects_empty_code(code):
    with pytest.raises(SandboxViolation):
        check_python_source(code)


def test_python_policy_rejects_syntax_errors():
    with pytest.raises(SandboxViolation) as failure:
        check_python_source("def broken(:\n    pass")

    assert "语法错误" in str(failure.value)


# --- Shell 命令策略 --------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "curl http://example.com",
        "wget http://example.com/x.sh",
        "nc -l 4444",
        "ssh user@host",
        "scp file user@host:/tmp",
        "pip install requests",
        "pip3 install requests",
        "apt-get install curl",
        "apk add curl",
        "sudo ls /root",
        "chmod 777 /etc/passwd",
        "dd if=/dev/zero of=/dev/sda",
        "rm -rf /",
        "kill -9 1",
        "pkill python",
        "shutdown -h now",
        "echo x > /dev/sda",
    ],
)
def test_shell_policy_rejects_dangerous_commands(command):
    with pytest.raises(SandboxViolation):
        check_shell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "ls -la /tmp",
        "python -c \"print(1+1)\"",
        "cat /tmp/data.txt",
        "wc -l /tmp/data.txt",
    ],
)
def test_shell_policy_allows_ordinary_commands(command):
    check_shell_command(command)


def test_shell_policy_rejects_empty_command():
    with pytest.raises(SandboxViolation):
        check_shell_command("   ")


# --- 语言归一化 ------------------------------------------------------------


@pytest.mark.parametrize("language", ["python", "Python", "python3", "py"])
def test_normalize_language_maps_python_aliases(language):
    assert normalize_language(language) == "python"


@pytest.mark.parametrize("language", ["shell", "SH", "sh", "bash"])
def test_normalize_language_maps_shell_aliases(language):
    assert normalize_language(language) == "shell"


def test_normalize_language_rejects_unknown_language():
    with pytest.raises(SandboxViolation):
        normalize_language("ruby")


# --- 统一入口：策略先于后端 -------------------------------------------------


def test_policy_violation_happens_before_the_backend_runs():
    spy = SpySandbox()

    with pytest.raises(SandboxViolation):
        run_in_sandbox("import os", sandbox=spy)

    assert spy.calls == []


def test_shell_violation_happens_before_the_backend_runs():
    spy = SpySandbox()

    with pytest.raises(SandboxViolation):
        run_in_sandbox("curl http://example.com", language="shell", sandbox=spy)

    assert spy.calls == []


def test_code_length_limit_is_enforced_before_the_backend_runs():
    settings = SandboxSettings(max_code_chars=32)
    spy = SpySandbox()

    with pytest.raises(SandboxViolation):
        run_in_sandbox("x = 1\n" * 20, sandbox=spy, settings=settings)

    assert spy.calls == []


def test_allowed_code_reaches_the_backend_with_normalized_language():
    spy = SpySandbox()

    with pytest.raises(AssertionError):
        run_in_sandbox("print(1)", language="python3", sandbox=spy)

    assert spy.calls == [("print(1)", "python")]


# --- 拒绝后端 --------------------------------------------------------------


def test_denied_backend_never_executes_and_reports_unavailable():
    sandbox = build_sandbox(SandboxSettings(backend="denied"))

    assert isinstance(sandbox, DeniedSandbox)
    assert sandbox.available() is False
    with pytest.raises(SandboxUnavailable):
        sandbox.run("print(1)")


def test_run_in_sandbox_raises_unavailable_for_denied_backend():
    with pytest.raises(SandboxUnavailable):
        run_in_sandbox("print(1)", sandbox=DeniedSandbox())


# --- code_execution 工具的归一化 -------------------------------------------


def test_code_execution_reports_policy_rejection_as_tool_failure():
    tool = CodeExecutionTool(sandbox=DeniedSandbox())

    with pytest.raises(ToolExecutionError) as failure:
        tool.invoke({"code": "import os"})

    assert "沙箱策略拒绝" in str(failure.value)


def test_code_execution_reports_unavailable_backend_as_tool_failure():
    tool = CodeExecutionTool(sandbox=DeniedSandbox())

    with pytest.raises(ToolExecutionError) as failure:
        tool.invoke({"code": "print(1)"})

    assert "沙箱不可用" in str(failure.value)


def test_code_execution_rejects_unknown_language_argument():
    with pytest.raises(ToolExecutionError):
        CodeExecutionTool(sandbox=DeniedSandbox()).invoke(
            {"code": "print(1)", "language": "ruby"}
        )


def test_code_execution_spec_declares_language_enum():
    spec = CodeExecutionTool().spec()

    assert spec.name == "code_execution"
    assert spec.input_schema["properties"]["language"]["enum"] == ["python", "shell"]

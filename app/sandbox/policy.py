"""沙箱执行前的静态策略校验（成员 C D7-8）。

职责边界：本模块只做**语言层的越权检查**——把明显越界的代码挡在沙箱启动之前，
给出可读的拒绝原因。真正的隔离边界仍由 `app.sandbox.docker_runtime` 的容器提供
（禁用网络、只读根文件系统、资源与进程数上限、执行超时、只挂工作区）。

**文件访问从「禁止」改为「允许」**（ADR-033 §7）：处理工作区里的文件正是沙箱要干的活，
而"只能碰工作区"这件事表达不了在语法层——只挂一个工作区目录、根文件系统只读、非 root、
无能力、无网络，才是真正的约束。所以这里继续拦的是**能力类逃逸**（网络、进程执行、
导入机制、双下划线属性），不再拦 `open` 本身。

对齐事实源：

- `doc/15 AI Native多智能体协作平台.md` §五「工具调用的安全性」：限制系统资源访问和网络权限；
- `doc/testing.md` U-08「沙箱边界拒绝越权」：代码执行工具拒绝网络/危险命令。

采用**模块白名单**而非黑名单：未在 `ALLOWED_MODULES` 中的 import 一律拒绝，
避免遗漏 `socket`/`ctypes` 之类可以绕过网络的模块。
"""

from __future__ import annotations

import ast
import re


class SandboxViolation(ValueError):
    """代码或命令违反沙箱策略（在启动沙箱之前抛出）。"""


ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "collections",
        "csv",
        "datetime",
        "decimal",
        "fractions",
        "functools",
        "itertools",
        "json",
        "io",
        "math",
        "pathlib",
        "glob",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "typing",
        "unicodedata",
    }
)

NETWORK_MODULES: frozenset[str] = frozenset({"urllib", "http", "httpx", "urllib3"})
"""HTTP 客户端模块：**只在沙箱联网打开时**放行（ADR-034 §4）。

默认禁网时连它们一起禁——"没有网却允许写网络代码"只会让失败发生在更晚、更难查的地方。

`socket` **始终禁止**：那等于允许任意端口探测。放行的是 HTTP 客户端这一层，
而沙箱的网络出口本身就只有代理（容器只接 internal 网络，没有默认路由）。
"""

FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "exit",
        "globals",
        "input",
        "locals",
        "quit",
        "vars",
    }
)

FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "__bases__",
        "__builtins__",
        "__class__",
        "__code__",
        "__dict__",
        "__globals__",
        "__mro__",
        "__subclasses__",
    }
)

def check_python_source(code: str, *, allow_network: bool = False) -> None:
    """校验一段 Python 源码，越权时抛 `SandboxViolation`。

    拒绝：语法错误、非白名单 import、危险内建调用、双下划线逃逸属性。
    `allow_network=True`（沙箱联网打开时）额外放行 HTTP 客户端模块。
    """

    if not code.strip():
        raise SandboxViolation("代码为空")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SandboxViolation(f"代码语法错误: {exc.msg}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _check_import_names(
                [alias.name for alias in node.names], allow_network=allow_network
            )
        elif isinstance(node, ast.ImportFrom):
            _check_import_names([node.module or ""], allow_network=allow_network)
        elif isinstance(node, ast.Call):
            _check_call(node)
        elif isinstance(node, ast.Attribute):
            _check_attribute(node)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            raise SandboxViolation(f"禁止引用危险内建: {node.id}")


def check_shell_command(command: str) -> None:
    """校验一条 Shell 命令，越权时抛 `SandboxViolation`。

    Shell 无法做语法级白名单，只能拦截典型的网络访问、提权与破坏性命令；
    兜底仍是容器本身禁网、只读根文件系统与超时。
    """

    if not command.strip():
        raise SandboxViolation("命令为空")
    flattened = " ".join(command.split()).lower()
    for pattern, reason in _FORBIDDEN_SHELL_PATTERNS:
        if re.search(pattern, flattened):
            raise SandboxViolation(f"越权命令被拒绝（{reason}）")


_FORBIDDEN_SHELL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(curl|wget|nc|ncat|telnet|ssh|scp|ftp|socat)\b", "网络访问"),
    (r"\b(pip|pip3)\s+install\b", "运行期安装依赖"),
    # 包管理器只在**同一条命令里出现改动系统的子命令**时拦。原先的 `\b(apt|apt-get)\b`
    # 会把 `ls /etc/apt`、`apt list` 这类读操作连路径一起拦下来——这是修下面那条
    # `/dev/null` 误判时对同族规则排查发现的第二处误判面（**不在** 2026-09-23 那次实测
    # 命中的命令里；`full-upgrade` 这类带前缀的子命令仍能命中 `\bupgrade\b`，
    # install/add/update… 同理）。
    (
        r"\b(apt|apt-get|apk|yum|dnf)\b[^|;&]*\b(install|add|update|upgrade|remove|purge|autoremove)\b",
        "运行期安装系统包",
    ),
    (r"\bsudo\b", "提权"),
    (r"\b(chmod|chown)\b", "修改权限"),
    (r"\b(mkfs|fdisk|dd)\b", "破坏性磁盘操作"),
    (r"\brm\s+-[a-z]*[rf]", "删除文件"),
    (r"\b(shutdown|reboot|kill|pkill)\b", "影响主机进程"),
    # `> /dev/xxx` 写**真实设备节点**是危险的，继续拦；但 `> /dev/null`（含 `2>` / `&>` 写法）
    # 与 `/dev/stdout`、`/dev/stderr` 是"丢弃 / 转发输出"的常规写法，不碰任何设备。
    # 2026-09-23 实测：一条只读的 `findmnt -T /workspace 2>/dev/null` 被整条判成
    # "写入设备文件"，模型只能改用 stat/df 绕过去。
    (r">\s*/dev/(?!null\b|stdout\b|stderr\b)", "写入设备文件"),
)


def _check_import_names(modules: list[str], *, allow_network: bool = False) -> None:
    allowed = ALLOWED_MODULES | NETWORK_MODULES if allow_network else ALLOWED_MODULES
    for module in modules:
        root = module.split(".")[0]
        if not root:
            raise SandboxViolation("禁止相对导入")
        if root not in allowed:
            raise SandboxViolation(f"禁止导入模块: {module}")


def _check_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
        raise SandboxViolation(f"禁止调用危险内建: {node.func.id}")
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"system", "popen", "spawn", "fork", "execv", "execve"}
    ):
        raise SandboxViolation(f"禁止调用进程执行接口: {node.func.attr}")


def _check_attribute(node: ast.Attribute) -> None:
    if node.attr in FORBIDDEN_ATTRIBUTES:
        raise SandboxViolation(f"禁止访问双下划线属性: {node.attr}")


def normalize_language(language: str) -> str:
    """归一化语言标识，仅支持 `python` 与 `shell`。"""

    resolved = language.strip().lower()
    if resolved in {"python", "python3", "py"}:
        return "python"
    if resolved in {"shell", "sh", "bash"}:
        return "shell"
    raise SandboxViolation(f"不支持的语言: {language}")

"""内置工具：计算器、网络搜索、代码执行、只读 SQL（见 `doc/architecture.md` 模块划分）。"""

from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.calculator import CalculatorTool
from app.tools.code_exec import CodeExecutionTool
from app.tools.registry import BuiltinToolRegistry, build_builtin_registry, builtin_tools
from app.tools.search import WebSearchTool
from app.tools.sql import SqlQueryTool, assert_read_only_statement

__all__ = [
    "BuiltinTool",
    "BuiltinToolRegistry",
    "CalculatorTool",
    "CodeExecutionTool",
    "SqlQueryTool",
    "ToolExecutionError",
    "WebSearchTool",
    "assert_read_only_statement",
    "build_builtin_registry",
    "builtin_tools",
]

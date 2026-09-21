"""内置工具注册表（成员 C D7-8）。

实现编排层冻结的 `ToolRegistry` 协议（`app/orchestration/tools.py`，ADR-009）：
`list_tools()` 供流水线动态发现，`call()` 执行一次调用并把失败原因抛出，
由 `ToolCaller` 归一化为 `failed` 记录。

注册表是 MCP 的本地实现之一：`app/mcp/server.py` 用它把同一批工具按 MCP
标准格式暴露出去，`app/mcp/registry.py` 决定流水线走本地还是走 MCP 客户端。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.orchestration.tools import ToolCall, ToolSpec
from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.calculator import CalculatorTool
from app.tools.code_exec import CodeExecutionTool
from app.tools.search import WebSearchTool
from app.tools.sql import SqlQueryTool


def builtin_tools() -> tuple[BuiltinTool, ...]:
    """四个**进程级**内置工具：计算器、网络搜索、代码执行、只读 SQL。

    会话级的「读本次会话附件」两个工具不在这里：它们需要 `session_id`，
    由 `app/tools/session_files.py::session_file_tools()` 按执行临时构造，
    再经 `app/mcp/registry.py::with_session_files()` 拼到注册表上。
    """

    return (
        CalculatorTool(),
        WebSearchTool(),
        CodeExecutionTool(),
        SqlQueryTool(),
    )


class BuiltinToolRegistry:
    """按名字索引的内置工具注册表。"""

    def __init__(self, tools: Sequence[BuiltinTool] | None = None) -> None:
        resolved = list(tools) if tools is not None else list(builtin_tools())
        self._tools = {tool.name: tool for tool in resolved}
        if len(self._tools) != len(resolved):
            raise ValueError("内置工具名重复")

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            self._tools[name].spec() for name in sorted(self._tools)
        )

    def get(self, name: str) -> BuiltinTool | None:
        return self._tools.get(name)

    def call(self, request: ToolCall) -> Any:
        tool = self._tools.get(request.tool_name)
        if tool is None:
            # 模型请求了未注册的工具：原样重试同样找不到（ADR-009 修订 3）。
            raise ToolExecutionError(
                f"未注册的工具: {request.tool_name}", retryable=False
            )
        return tool.invoke(request.arguments)


def build_builtin_registry() -> BuiltinToolRegistry:
    return BuiltinToolRegistry()

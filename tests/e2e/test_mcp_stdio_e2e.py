"""E 系列端到端：MCP **跨进程 stdio** 链路（成员 C D9-10，I-06 缺口）。

`tests/unit/test_mcp_tools.py` 用 `mcp.shared.memory` 把 Server 与 Client 接在同一个
进程里，走的是真实协议往返但**没有进程边界**；真实用户注册的 stdio Server 是
「一个独立子进程 + 两根管道」。两者会失败的地方不一样：

- 子进程能否启动、能否 `import app`、退出时能否被回收；
- 子进程往 stdout 写任何非协议内容都会直接破坏 JSON-RPC 流
  （`app/observability/logging.py` 明确写 stderr，本文件是这条约定的守卫）；
- 握手（`initialize`）与工具目录的序列化跨进程后仍然一致。

因此这里**真的启动 `python -m app.mcp.server`**，走 `MCP_TRANSPORT=stdio` 与
「注册条目」两条生产路径。不依赖 Dapr / PostgreSQL / Redis / 外部模型。
"""

from __future__ import annotations

import os
import sys

import pytest

from app.mcp import McpSettings, McpToolRegistry, build_tool_registry, reset_tool_registry_cache
from app.mcp.client import (
    build_registry_session_factory,
    build_stdio_session_factory,
    discover_registry_server,
)
from app.orchestration.tools import ToolCall

EXPECTED_TOOL_NAMES = ["calculator", "code_execution", "sql_query", "web_search"]
SERVER_MODULE_ARGS = ["-m", "app.mcp.server"]


def _stdio_entry(**overrides: object) -> dict[str, object]:
    """一条 stdio 的 `mcp_server_registry` 条目，指向本仓的 MCP Server 模块。"""

    entry: dict[str, object] = {
        "id": "macp-builtin",
        "name": "内置工具",
        "transport": "stdio",
        "command": sys.executable,
        "args": list(SERVER_MODULE_ARGS),
        "env": {},
        # 子进程要能 `import app`：显式用当前工作目录，不依赖继承。
        "cwd": os.getcwd(),
        "url": None,
        "headers": {},
    }
    entry.update(overrides)
    return entry


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_tool_registry_cache()
    yield
    reset_tool_registry_cache()


def test_stdio_transport_discovers_the_builtin_tools_across_a_process_boundary():
    """`MCP_TRANSPORT=stdio`：流水线经子进程发现内置工具（生产路径之一）。"""

    settings = McpSettings(
        _env_file=None, transport="stdio", server_command=sys.executable
    )
    registry = McpToolRegistry(build_stdio_session_factory(settings))
    try:
        specs = registry.list_tools()
    finally:
        registry.close()

    assert [spec.name for spec in specs] == EXPECTED_TOOL_NAMES
    assert all(spec.input_schema.get("type") == "object" for spec in specs)


def test_stdio_transport_executes_a_tool_in_the_child_process():
    """调用真的落在子进程里：输出经 JSON-RPC 文本回传后被还原成结构化结果。"""

    settings = McpSettings(
        _env_file=None, transport="stdio", server_command=sys.executable
    )
    registry = McpToolRegistry(build_stdio_session_factory(settings))
    try:
        output = registry.call(
            ToolCall(call_id="c1", tool_name="calculator", arguments={"expression": "6*7"})
        )
    finally:
        registry.close()

    assert output == {"expression": "6*7", "value": 42}


def test_registry_entry_discovery_works_over_stdio():
    """`POST /config/mcp/servers/{id}/discover` 的协议侧：真实注册条目 + 真实子进程。

    这条覆盖的是 ADR-017 的「注册 stdio Server → 发现工具」链路，
    与上面两条的区别是参数来自**条目行**（用户配置）而不是 `MCP_*` 环境变量。
    """

    result = discover_registry_server(_stdio_entry(), timeout=60.0)

    assert result["server_info"].get("name") == "macp-tools"
    assert [tool["name"] for tool in result["tools"]] == EXPECTED_TOOL_NAMES
    assert all(tool["input_schema"] for tool in result["tools"])


def test_stdio_backend_registry_is_reachable_through_the_default_entry_point():
    """`build_tool_registry()` 在 `MCP_TRANSPORT=stdio` 下返回可用的注册表。"""

    settings = McpSettings(
        _env_file=None, transport="stdio", server_command=sys.executable
    )
    registry = build_tool_registry(settings)

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES


def test_registry_entry_factory_rejects_unsupported_transport():
    """`ws` 没有客户端实现：显式报错，不静默退回别的传输（避免连错地址）。"""

    from app.mcp.client import McpTransportUnsupported

    with pytest.raises(McpTransportUnsupported):
        build_registry_session_factory(_stdio_entry(transport="ws", url="ws://localhost:9/mcp"))

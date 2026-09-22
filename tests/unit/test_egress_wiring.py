"""出网策略的接线：工具侧与 MCP 远程条目都必须过它（ADR-034 §4）。

策略本身的行为在 `test_egress_policy.py`；这里只钉"有没有接上、失败怎么表达"。
"""

from __future__ import annotations

import pytest

import app.tools.search as search_module
from app.mcp.client import (
    McpTransportUnsupported,
    build_registry_session_factory,
    discover_registry_server,
)
from app.security.egress import EgressDenied
from app.tools.base import ToolExecutionError
from app.tools.config import ToolSettings
from app.tools.search import WebSearchArgs, WebSearchTool


def test_search_tool_default_fetcher_reports_a_policy_denial(monkeypatch):
    """策略拒绝是确定性失败：告诉模型原因并禁止重试同一参数（ADR-009 修订 3）。"""

    def deny(*_args, **_kwargs):
        raise EgressDenied("private_ip", "目标是内网/保留地址，已拦截：127.0.0.1")

    monkeypatch.setattr(search_module, "egress_fetch_json", deny)
    tool = WebSearchTool(settings=ToolSettings(_env_file=None))

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.run(WebSearchArgs(query="anything", max_results=1))

    assert excinfo.value.retryable is False
    assert "private_ip" in str(excinfo.value)


def test_mcp_http_entry_to_a_private_address_is_rejected_before_connecting(monkeypatch):
    """远程 MCP Server 也要过策略；拒绝复用既有的 McpTransportUnsupported 语义。

    校验发生在**打开会话时**而不是构造工厂时：合并工具目录那条路径要零 IO（ADR-026），
    所以构造工厂本身必须始终是廉价的。
    """

    from app.security import egress as egress_module
    from app.security.config import EgressSettings

    policy = egress_module.EgressPolicy(
        EgressSettings(_env_file=None, allowed_ports="443,8080", internal_hosts="")
    )
    monkeypatch.setattr(egress_module, "get_egress_policy", lambda: policy)

    with pytest.raises(McpTransportUnsupported) as excinfo:
        discover_registry_server(
            {"transport": "http", "url": "http://10.0.0.1:8080/mcp"}, timeout=5.0
        )

    assert "出网策略拒绝" in str(excinfo.value)


def test_building_an_mcp_factory_stays_cheap_even_for_a_public_host(monkeypatch):
    """构造工厂不做 DNS：目录是配置的函数，不是网络可用性的函数（ADR-026）。"""

    from app.security import egress as egress_module
    from app.security.config import EgressSettings

    policy = egress_module.EgressPolicy(
        EgressSettings(_env_file=None),
        resolver=lambda host: pytest.fail("构造工厂时不应该解析 DNS"),
    )
    monkeypatch.setattr(egress_module, "get_egress_policy", lambda: policy)

    factory = build_registry_session_factory(
        {"transport": "http", "url": "https://mcp.example.com/mcp"}
    )

    assert callable(factory)

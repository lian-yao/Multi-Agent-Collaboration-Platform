"""`scripts/search_gateway.py`：Bing RSS → DuckDuckGo 契约网关。

证据分三层，全部**不做出网请求**：

1. 纯函数层：RSS 解析与契约组装（畸形输入必须显式失败，不静默返回空结果）；
2. HTTP 层：真起 `ThreadingHTTPServer`，用真 `urllib` 打一次，覆盖 200/400/404/502；
3. 消费层：真 `WebSearchTool` 指向真网关（只把上游取数换成固定 RSS），
   证明「网关输出 → 工具解析」这条契约是成立的——这是本次改动的验收点。
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from app.tools.config import ToolSettings
from app.tools.search import WebSearchTool
from scripts import search_gateway

RSS_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel>
  <title>必应：LangGraph</title>
  <item>
    <title>LangGraph - LangChain 框架</title>
    <link>https://langgraph.com.cn/index.html</link>
    <description>LangGraph 的生态系统说明。</description>
  </item>
  <item>
    <title>LangGraph: Agent Orchestration Framework</title>
    <link>https://www.langchain.com/langgraph</link>
    <description>Build and scale AI workloads.</description>
  </item>
  <item>
    <title>没有链接的条目会被丢弃</title>
    <description>缺 link。</description>
  </item>
  <item>
    <title>无摘要条目</title>
    <link>https://example.com/no-snippet</link>
    <description></description>
  </item>
</channel></rss>
"""


def _get(url: str) -> tuple[int, dict]:
    """返回 (状态码, JSON)；非 2xx 也读 body，便于断言错误契约。"""

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _allow_local_egress(monkeypatch, base: str) -> None:
    """把用例访问的本地网关列进内部服务并放行其端口（ADR-034）。

    生产里对应的是 compose 把 `search-gateway` 写进 `EGRESS_INTERNAL_HOSTS`、
    把 8800 写进 `EGRESS_ALLOWED_PORTS`。测试不读环境变量，所以这里显式装一个策略：
    默认策略只放 443 与公网，`127.0.0.1` 会被正确地拦下来。
    """

    from app.security import egress as egress_module
    from app.security.config import EgressSettings

    port = base.rsplit(":", 1)[1]
    policy = egress_module.EgressPolicy(
        EgressSettings(
            _env_file=None, allowed_ports=f"443,{port}", internal_hosts="127.0.0.1"
        )
    )
    monkeypatch.setattr(egress_module, "get_egress_policy", lambda: policy)


@contextlib.contextmanager
def running_gateway(
    monkeypatch, body: str | None = RSS_BODY, *, fetch=None
) -> Iterator[str]:
    """起一个上游被替换成假取数的真网关，产出它的基础地址。

    默认取数返回固定 RSS；`fetch` 用来换成别的假取数（例如断言「健康检查不打上游」）。
    """

    monkeypatch.setattr(
        search_gateway,
        "fetch_rss",
        fetch or (lambda upstream, query, timeout: body),
    )
    server = search_gateway.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_parse_rss_keeps_document_order_and_skips_items_without_link():
    results = search_gateway.parse_rss(RSS_BODY)

    assert [result["url"] for result in results] == [
        "https://langgraph.com.cn/index.html",
        "https://www.langchain.com/langgraph",
        "https://example.com/no-snippet",
    ]
    assert results[0]["title"] == "LangGraph - LangChain 框架"


def test_parse_rss_rejects_non_rss_payload():
    """上游挂了返回 HTML/空串时，必须是显式失败而不是「零条结果」。"""

    with pytest.raises(search_gateway.UpstreamError):
        search_gateway.parse_rss("<html><body>blocked</body></html>")


def test_to_instant_answer_keeps_every_result_title():
    """第一条结果不能被查询串顶掉标题：工具按 URL 去重且先收 Abstract 条目。"""

    payload = search_gateway.to_instant_answer(
        "LangGraph", search_gateway.parse_rss(RSS_BODY)
    )

    assert payload["Heading"] == "LangGraph"
    assert payload["AbstractText"] == ""
    assert payload["AbstractURL"] == ""
    assert payload["RelatedTopics"] == [
        {
            "FirstURL": "https://langgraph.com.cn/index.html",
            # 标题自带的 " - " 必须让位给「标题 - 摘要」这个约定分隔符，
            # 否则工具的 partition(" - ") 只会还原出「LangGraph」。
            "Text": "LangGraph – LangChain 框架 - LangGraph 的生态系统说明。",
        },
        {
            "FirstURL": "https://www.langchain.com/langgraph",
            "Text": "LangGraph: Agent Orchestration Framework - Build and scale AI workloads.",
        },
        {
            "FirstURL": "https://example.com/no-snippet",
            "Text": "无摘要条目",
        },
    ]


def test_to_instant_answer_without_results_is_an_empty_contract():
    payload = search_gateway.to_instant_answer("没有命中", [])

    assert payload["RelatedTopics"] == []
    assert payload["Heading"] == "没有命中"


# --------------------------------------------------------------------------- #
# HTTP 契约
# --------------------------------------------------------------------------- #


def test_gateway_serves_the_contract_over_http(monkeypatch):
    with running_gateway(monkeypatch, RSS_BODY) as base:
        status, payload = _get(f"{base}/search?q=LangGraph&format=json&no_html=1")

    assert status == 200
    assert payload["Heading"] == "LangGraph"
    assert len(payload["RelatedTopics"]) == 3
    assert payload["RelatedTopics"][0]["FirstURL"] == "https://langgraph.com.cn/index.html"


def test_gateway_rejects_a_missing_query(monkeypatch):
    with running_gateway(monkeypatch, RSS_BODY) as base:
        status, payload = _get(f"{base}/search?format=json")

    assert status == 400
    assert "q" in payload["error"]


def test_gateway_rejects_an_unknown_path(monkeypatch):
    with running_gateway(monkeypatch, RSS_BODY) as base:
        status, payload = _get(f"{base}/tools?q=LangGraph")

    assert status == 404
    assert "未知路径" in payload["error"]


def test_health_probe_does_not_touch_the_upstream(monkeypatch):
    """存活探测不许打上游：上游抖动只该影响搜索，不该把容器标成 unhealthy（ADR-032）。"""

    def explode(*_args, **_kwargs):
        raise AssertionError("健康检查不应访问上游")

    with running_gateway(monkeypatch, fetch=explode) as base:
        status, payload = _get(f"{base}/health")

    assert status == 200
    assert payload == {"status": "ok"}


def test_gateway_reports_upstream_failure_as_502(monkeypatch):
    """上游解析失败不能伪装成「搜到了但没内容」——工具要能按失败重试/降级。"""

    with running_gateway(monkeypatch, "<html>captcha</html>") as base:
        status, payload = _get(f"{base}/search?q=LangGraph")

    assert status == 502
    assert "不是 RSS 文档" in payload["error"]


# --------------------------------------------------------------------------- #
# 消费层：真工具 + 真网关
# --------------------------------------------------------------------------- #


def test_web_search_tool_consumes_the_gateway(monkeypatch):
    with running_gateway(monkeypatch, RSS_BODY) as base:
        _allow_local_egress(monkeypatch, base)
        settings = ToolSettings(
            _env_file=None,
            search_endpoint=f"{base}/search",
            search_timeout_seconds=10.0,
            search_max_results=5,
        )
        output = WebSearchTool(settings=settings).run(
            WebSearchTool.args_model(query="LangGraph", max_results=5)
        )

    assert output["source"] == f"{base}/search"
    assert [result["url"] for result in output["results"]] == [
        "https://langgraph.com.cn/index.html",
        "https://www.langchain.com/langgraph",
        "https://example.com/no-snippet",
    ]
    # 标题必须保住：这是 Agent 判断相关性最主要的信号。
    assert output["results"][0]["title"] == "LangGraph – LangChain 框架"
    assert output["results"][0]["snippet"] == "LangGraph 的生态系统说明。"
    assert output["results"][2]["snippet"] == "无摘要条目"


def test_web_search_tool_honours_max_results(monkeypatch):
    with running_gateway(monkeypatch, RSS_BODY) as base:
        _allow_local_egress(monkeypatch, base)
        settings = ToolSettings(
            _env_file=None,
            search_endpoint=f"{base}/search",
            search_timeout_seconds=10.0,
        )
        output = WebSearchTool(settings=settings).run(
            WebSearchTool.args_model(query="LangGraph", max_results=2)
        )

    assert len(output["results"]) == 2

"""`web_search` 的豆包搜索（火山引擎 Custom）出口：解析、错误分层与配置校验（ADR-037）。

响应形状按**真机实测**的字段钉住（PascalCase 请求体 + `Result.WebResults`），避免以后
上游改字段时悄悄退化成「搜到了但没内容」——那正是 ADR-032 记过的 DuckDuckGo 病。
"""

from __future__ import annotations

import pytest

from app.tools.base import ToolExecutionError
from app.tools.config import DOUBAO_SEARCH_ENDPOINT, ToolSettings
from app.tools.search import WebSearchTool

PAYLOAD = {
    "ResponseMetadata": {"RequestId": "req-1"},
    "Result": {
        "ResultCount": 3,
        "WebResults": [
            {
                "Title": "LangGraph overview",
                "Url": "https://example.com/a",
                "Snippet": "Gain control\nwith LangGraph",
                "Summary": "Gain control with LangGraph",
            },
            {
                "Title": "重复链接",
                "Url": "https://example.com/a",
                "Snippet": "dup",
            },
            {"Title": "", "Url": "https://example.com/b", "Snippet": ""},
            {"Title": "没有链接", "Url": "", "Snippet": "x"},
        ],
    },
}


def _settings(**overrides) -> ToolSettings:
    base = {"search_provider": "volcengine", "search_api_key": "k-test"}
    base.update(overrides)
    return ToolSettings(_env_file=None, **base)


def _tool(poster, **overrides) -> WebSearchTool:
    return WebSearchTool(settings=_settings(**overrides), fetch_post_json=poster)


# --------------------------------------------------------------------------- #
# 配置：端点随 provider 推导
# --------------------------------------------------------------------------- #


def test_volcengine_provider_defaults_to_doubao_endpoint():
    assert _settings().search_endpoint == DOUBAO_SEARCH_ENDPOINT


def test_explicit_endpoint_wins_over_provider_default():
    settings = _settings(search_endpoint="http://search-gateway:8800/search")

    assert settings.search_endpoint == "http://search-gateway:8800/search"


def test_blank_endpoint_falls_back_to_the_provider_default():
    """compose 里 `TOOL_SEARCH_ENDPOINT: ${TOOL_SEARCH_ENDPOINT:-}` 传空是常态：
    空串必须按「没给」处理，否则 provider 的默认端点会被一个空值覆盖掉。"""

    assert _settings(search_endpoint="").search_endpoint == DOUBAO_SEARCH_ENDPOINT
    assert _settings(search_endpoint="   ").search_endpoint == DOUBAO_SEARCH_ENDPOINT
    assert (
        ToolSettings(
            _env_file=None, search_provider="duckduckgo", search_endpoint=""
        ).search_endpoint
        == "https://api.duckduckgo.com/"
    )


def test_duckduckgo_remains_the_default_provider():
    settings = ToolSettings(_env_file=None)

    assert settings.search_provider == "duckduckgo"
    assert settings.search_endpoint == "https://api.duckduckgo.com/"


# --------------------------------------------------------------------------- #
# 取数与解析
# --------------------------------------------------------------------------- #


def test_sends_pascal_case_body_and_parses_web_results():
    calls = []

    def poster(endpoint, payload, timeout, api_key):
        calls.append((endpoint, payload, timeout, api_key))
        return PAYLOAD

    result = _tool(poster).invoke({"query": "LangGraph", "max_results": 5})

    assert result["source"] == DOUBAO_SEARCH_ENDPOINT
    assert [item["url"] for item in result["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["results"][0]["title"] == "LangGraph overview"
    assert result["results"][0]["snippet"] == "Gain control with LangGraph"
    assert calls[0][1] == {"Query": "LangGraph", "SearchType": "web", "Count": 5}
    assert calls[0][3] == "k-test"


def test_count_follows_the_effective_limit():
    calls = []

    def poster(endpoint, payload, timeout, api_key):
        calls.append(payload)
        return PAYLOAD

    limited = _tool(poster).invoke({"query": "x", "max_results": 1})

    assert calls[0]["Count"] == 1
    assert len(limited["results"]) == 1


def test_duckduckgo_provider_still_uses_the_get_path():
    seen = {}

    def getter(endpoint, params, timeout):
        seen["params"] = params
        return {"RelatedTopics": [{"FirstURL": "https://x", "Text": "标题 - 摘要"}]}

    tool = WebSearchTool(
        settings=ToolSettings(_env_file=None, search_provider="duckduckgo"),
        fetch_json=getter,
    )
    result = tool.invoke({"query": "q"})

    assert seen["params"]["q"] == "q"
    assert result["results"] == [{"title": "标题", "url": "https://x", "snippet": "摘要"}]


# --------------------------------------------------------------------------- #
# 错误分层：确定性失败不重试，瞬时失败可重试（ADR-037 §5）
# --------------------------------------------------------------------------- #


def test_business_error_code_is_not_retryable():
    payload = {
        "ResponseMetadata": {
            "Error": {"Code": "10400", "Message": "query or search type is empty"}
        },
        "Result": None,
    }

    with pytest.raises(ToolExecutionError) as excinfo:
        _tool(lambda *_: payload).invoke({"query": "x"})

    assert excinfo.value.retryable is False
    assert "10400" in str(excinfo.value)


def test_missing_api_key_fails_before_any_request():
    """不注入取数函数：缺 key 必须在**发出任何请求之前**被真实适配器拦下。"""

    with pytest.raises(ToolExecutionError) as excinfo:
        WebSearchTool(settings=_settings(search_api_key="")).invoke({"query": "x"})

    assert excinfo.value.retryable is False
    assert "TOOL_SEARCH_API_KEY" in str(excinfo.value)


def test_network_failure_is_retryable():
    def poster(*_args):
        raise TimeoutError("timed out")

    with pytest.raises(ToolExecutionError) as excinfo:
        _tool(poster).invoke({"query": "x"})

    assert excinfo.value.retryable is True
    assert DOUBAO_SEARCH_ENDPOINT in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, False), (403, False), (429, True), (503, True), (400, False)],
)
def test_http_status_maps_to_the_right_retryability(monkeypatch, status, retryable):
    from app.tools import search as search_module

    class Response:
        def __init__(self) -> None:
            self.status = status
            self.body = b"{}"

    monkeypatch.setattr(search_module, "egress_http_post", lambda *a, **k: Response())

    with pytest.raises(ToolExecutionError) as excinfo:
        WebSearchTool(settings=_settings()).invoke({"query": "x"})

    assert excinfo.value.retryable is retryable
    assert str(status) in str(excinfo.value)


def test_missing_result_is_not_retryable():
    with pytest.raises(ToolExecutionError) as excinfo:
        _tool(lambda *_: {"ResponseMetadata": {}}).invoke({"query": "x"})

    assert excinfo.value.retryable is False

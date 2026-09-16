"""网络搜索工具（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3 示例工具集
「网络搜索（调用公开搜索 API）」。

实现要点：

- 默认调用 DuckDuckGo Instant Answer 公开 JSON 接口，无需密钥；
- 通过 `TOOL_SEARCH_ENDPOINT` 可指向自建网关，但必须返回同一 JSON 契约
  （`Abstract`/`RelatedTopics`），响应形状不符时显式失败而不是猜字段；
- HTTP 传输由 `fetch_json` 注入，单元测试用假传输，不请求外部网络
  （`doc/testing.md` §1 测试替身约定）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.config import ToolSettings, get_tool_settings

USER_AGENT = "macp-agent/0.1 (+multi-agent-collaboration-platform)"

JsonFetcher = Callable[[str, dict[str, str], float], Any]


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=300, description="搜索关键词")
    max_results: int = Field(
        default=5, ge=1, le=10, description="返回结果条数上限"
    )


class WebSearchTool(BuiltinTool):
    name = "web_search"
    description = "检索公开资料，返回与关键词相关的标题、链接与摘要。"
    args_model = WebSearchArgs

    def __init__(
        self,
        settings: ToolSettings | None = None,
        *,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self._settings = settings or get_tool_settings()
        self._fetch_json = fetch_json or _http_get_json

    def run(self, args: WebSearchArgs) -> dict[str, Any]:
        endpoint = self._settings.search_endpoint
        params = {
            "q": args.query,
            "format": "json",
            "no_html": "1",
            "no_redirect": "1",
        }
        payload = self._fetch_json(
            endpoint, params, self._settings.search_timeout_seconds
        )
        results = _parse_results(payload)
        limit = min(args.max_results, self._settings.search_max_results)
        return {
            "query": args.query,
            "source": endpoint,
            "results": results[:limit],
        }


def _http_get_json(endpoint: str, params: dict[str, str], timeout: float) -> Any:
    """默认 HTTP 传输：构造带查询串的 GET 并解析 JSON。"""

    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ToolExecutionError(f"搜索服务返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ToolExecutionError(_unreachable_message(endpoint, exc.reason)) from exc
    except TimeoutError as exc:
        raise ToolExecutionError(
            f"搜索服务超时（{timeout}s）：{endpoint}；该工具需要出网访问搜索服务"
        ) from exc
    except OSError as exc:  # URLError 之外的底层 socket 错误
        raise ToolExecutionError(_unreachable_message(endpoint, exc)) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError("搜索服务返回的不是合法 JSON") from exc


def _unreachable_message(endpoint: str, reason: Any) -> str:
    """把「连不上」说清楚。

    无外网的演示环境里这是最常见的失败，只回一句「不可达」会让使用者以为工具坏了；
    这里显式点出「需要出网」以及实际请求的地址，并提示可换 `TOOL_SEARCH_ENDPOINT`。
    """

    return (
        f"搜索服务不可达（{reason}）：{endpoint}；"
        "该工具需要出网访问搜索服务，离线环境可改用 TOOL_SEARCH_ENDPOINT 指向自建网关"
    )


def _parse_results(payload: Any) -> list[dict[str, str]]:
    """按 DuckDuckGo Instant Answer 契约抽取结果条目。"""

    if not isinstance(payload, dict):
        raise ToolExecutionError("搜索服务响应不是 JSON 对象")

    results: list[dict[str, str]] = []
    abstract = payload.get("AbstractText")
    abstract_url = payload.get("AbstractURL")
    if isinstance(abstract, str) and abstract and isinstance(abstract_url, str):
        results.append(
            {
                "title": str(payload.get("Heading") or "摘要"),
                "url": abstract_url,
                "snippet": abstract,
            }
        )

    def walk(topics: Any) -> None:
        if not isinstance(topics, list):
            return
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            if isinstance(topic.get("Topics"), list):
                walk(topic["Topics"])
                continue
            url = topic.get("FirstURL")
            text = topic.get("Text")
            if isinstance(url, str) and isinstance(text, str):
                title, _, snippet = text.partition(" - ")
                results.append(
                    {
                        "title": title.strip() or text,
                        "url": url,
                        "snippet": (snippet or text).strip(),
                    }
                )

    walk(payload.get("RelatedTopics"))

    unique: dict[str, dict[str, str]] = {}
    for result in results:
        unique.setdefault(result["url"], result)
    return list(unique.values())

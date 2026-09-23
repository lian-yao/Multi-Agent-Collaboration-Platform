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
from app.security.egress import (
    EgressConfigError,
    EgressDenied,
    fetch_json as egress_fetch_json,
    http_post as egress_http_post,
)

USER_AGENT = "macp-agent/0.1 (+multi-agent-collaboration-platform)"

JsonFetcher = Callable[[str, dict[str, str], float], Any]
"""DuckDuckGo 契约的 GET 取数：(端点, 查询串, 超时) → JSON。"""

JsonPoster = Callable[[str, dict[str, Any], float, str], Any]
"""豆包契约的 POST 取数：(端点, 请求体, 超时, api_key) → JSON。"""

VOLCENGINE_SEARCH_TYPE = "web"
"""豆包搜索的 `SearchType`。实测 `custom` 会被上游以 10402「invalid search type」拒掉。"""


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
        fetch_post_json: JsonPoster | None = None,
    ) -> None:
        self._settings = settings or get_tool_settings()
        self._fetch_json = fetch_json or _egress_get_json
        self._fetch_post_json = fetch_post_json or _egress_post_json

    def _get(self, endpoint: str, params: dict[str, str]) -> Any:
        """GET 取数；网络层故障归一为**可重试**的 `ToolExecutionError`（ADR-037 §5）。"""

        try:
            return self._fetch_json(
                endpoint, params, self._settings.search_timeout_seconds
            )
        except OSError as exc:
            raise ToolExecutionError(
                _unreachable_message(endpoint, exc), retryable=True
            ) from exc

    def _post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        """豆包 POST 取数；同样的网络层归一。"""

        try:
            return self._fetch_post_json(
                endpoint,
                payload,
                self._settings.search_timeout_seconds,
                self._settings.search_api_key,
            )
        except OSError as exc:
            raise ToolExecutionError(
                _unreachable_message(endpoint, exc), retryable=True
            ) from exc

    def run(self, args: WebSearchArgs) -> dict[str, Any]:
        endpoint = self._settings.search_endpoint
        limit = min(args.max_results, self._settings.search_max_results)
        if self._settings.search_provider == "volcengine":
            payload = self._post(
                endpoint,
                {
                    "Query": args.query,
                    "SearchType": VOLCENGINE_SEARCH_TYPE,
                    "Count": limit,
                },
            )
            results = _parse_volcengine_results(payload)
        else:
            params = {
                "q": args.query,
                "format": "json",
                "no_html": "1",
                "no_redirect": "1",
            }
            results = _parse_results(self._get(endpoint, params))
        return {
            "query": args.query,
            "source": endpoint,
            "results": results[:limit],
        }


def _http_get_json(endpoint: str, params: dict[str, str], timeout: float) -> Any:
    """裸 HTTP 传输（保留给测试与排障）：不带出网策略。"""

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


def _egress_get_json(endpoint: str, params: dict[str, str], timeout: float) -> Any:
    """默认 HTTP 传输：走**出网策略**（ADR-034），内网拦截与域名白/黑名单都在那里。

    策略拒绝是确定性失败——同样的地址再试一次还是会被拒（ADR-009 修订 3），
    所以 `retryable=False`；网络层的瞬时故障仍然是可重试的。
    """

    try:
        return egress_fetch_json(endpoint, params, timeout, purpose="tool")
    except EgressDenied as exc:
        raise ToolExecutionError(f"出网被策略拒绝（{exc.reason}）：{exc.detail}", retryable=False) from exc
    except EgressConfigError as exc:  # 配置本身非法：不要让模型以为是"网络抖动"
        raise ToolExecutionError(f"出网策略配置错误：{exc}", retryable=False) from exc


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


def _egress_post_json(
    endpoint: str, payload: dict[str, Any], timeout: float, api_key: str
) -> Any:
    """豆包搜索的 POST 传输：仍走**出网策略**（ADR-037 §4），只是方法换成 POST。

    错误分层与 GET 一致：策略拒绝 / 配置非法 / 凭证无效 / 业务错误码都是**确定性失败**
    （`retryable=False`，原样重试只会重复失败）；上游 429 / 5xx 与网络层故障仍可重试。
    """

    if not api_key:
        raise ToolExecutionError(
            "未配置豆包搜索 API key（TOOL_SEARCH_API_KEY）：该 provider 需要按量付费凭证",
            retryable=False,
        )

    try:
        response = egress_http_post(
            endpoint,
            payload=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            purpose="tool",
        )
    except EgressDenied as exc:
        raise ToolExecutionError(
            f"出网被策略拒绝（{exc.reason}）：{exc.detail}", retryable=False
        ) from exc
    except EgressConfigError as exc:
        raise ToolExecutionError(f"出网策略配置错误：{exc}", retryable=False) from exc

    if response.status in {401, 403}:
        raise ToolExecutionError(
            f"豆包搜索鉴权失败（HTTP {response.status}）：检查 TOOL_SEARCH_API_KEY 是否有效",
            retryable=False,
        )
    if response.status == 429 or response.status >= 500:
        raise ToolExecutionError(
            f"豆包搜索暂时不可用（HTTP {response.status}）：{endpoint}", retryable=True
        )
    if response.status >= 400:
        raise ToolExecutionError(
            f"豆包搜索返回 HTTP {response.status}：{endpoint}", retryable=False
        )

    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolExecutionError("豆包搜索返回的不是合法 JSON", retryable=False) from exc


def _parse_volcengine_results(payload: Any) -> list[dict[str, str]]:
    """把豆包搜索的 `Result.WebResults` 映射成 `{title,url,snippet}`（ADR-037 §1）。

    业务错误码藏在 HTTP 200 的 `ResponseMetadata.Error` 里；这里**显式失败**而不是当成
    「搜到了但没内容」——那正是 ADR-032 记过的 DuckDuckGo 病。
    """

    if not isinstance(payload, dict):
        raise ToolExecutionError("豆包搜索响应不是 JSON 对象", retryable=False)

    metadata = payload.get("ResponseMetadata")
    error = metadata.get("Error") if isinstance(metadata, dict) else None
    if isinstance(error, dict) and error:
        code = str(error.get("Code") or "")
        message = str(error.get("Message") or "")
        raise ToolExecutionError(f"豆包搜索返回错误 {code}：{message}", retryable=False)

    result = payload.get("Result")
    if not isinstance(result, dict):
        raise ToolExecutionError("豆包搜索响应缺少 Result", retryable=False)
    items = result.get("WebResults")
    if not isinstance(items, list):
        raise ToolExecutionError(
            "豆包搜索响应的 Result.WebResults 不是数组", retryable=False
        )

    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("Url")
        if not isinstance(url, str) or not url:
            continue
        title = item.get("Title")
        snippet = item.get("Snippet") or item.get("Summary") or ""
        results.append(
            {
                "title": _one_line(str(title)) if title else url,
                "url": url,
                "snippet": _one_line(str(snippet)),
            }
        )

    unique: dict[str, dict[str, str]] = {}
    for entry in results:
        unique.setdefault(entry["url"], entry)
    return list(unique.values())


def _one_line(text: str) -> str:
    """上游的 Snippet 带大段换行（实测），压成一行再交给 Agent。"""

    return " ".join(text.split())

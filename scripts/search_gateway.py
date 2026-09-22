"""本地搜索网关：Bing RSS → DuckDuckGo Instant Answer 契约。

背景
----
`app/tools/search.py::WebSearchTool` 默认打 `https://api.duckduckgo.com/`。该域名
在部分网络（含本项目当前开发环境）被阻断：TCP 443 直接超时，工具只得到
`ToolExecutionError: 搜索服务不可达`，Agent 因此拿不到任何资料。

`.env_example` 给出的退路是把 `TOOL_SEARCH_ENDPOINT` 指向一个返回**同一 JSON
契约**（`Abstract` / `RelatedTopics`）的自建网关，本文件就是这条退路的最小实现。

契约（`app/tools/search.py::_parse_results` 消费的字段）
-------------------------------------------------------

- `Heading`：查询串；
- `AbstractText` / `AbstractURL`：留空，理由见下；
- `RelatedTopics[]`：`{"FirstURL": 链接, "Text": "标题 - 摘要"}`，与 DuckDuckGo
  RelatedTopics 的形状一致；工具按 `FirstURL` 去重后再截断到
  `TOOL_SEARCH_MAX_RESULTS`。

设计取舍
--------

- **只用标准库**：`app` 侧一行不改、不加依赖，因此 `pyproject.toml` / `uv.lock` /
  `doc/requirements.txt` 无需同步。
- **只转发、不解释**：不猜 Bing 之外的字段，不做重排与摘要生成；取数或解析失败
  一律显式回 502，不返回空结果冒充「搜到了但没内容」。
- **不填 `AbstractText` / `AbstractURL`**：`_parse_results` 先收 Abstract 条目、再按
  `FirstURL` 去重，把第一条结果填成 Abstract 会让**首条结果的真实标题被查询串顶掉**
  （标题是 Agent 判断相关性最主要的信号）。摘要字段留空、全部结果走
  `RelatedTopics`，N 条结果的标题与链接一条不丢。
- **标题里的 `" - "` 换成 `" – "`**：`_parse_results` 用 `text.partition(" - ")` 从
  `RelatedTopics[].Text` 还原「标题 / 摘要」，而 Bing 标题常自带 `" - "`
  （如「LangGraph - LangChain 框架」）——不换就会被截断成「LangGraph」。
- **只读、无状态**：不落盘、不缓存，避免引入第二份事实源。

用法
----

    uv run python scripts/search_gateway.py --port 8800

然后让工具指向它（宿主机直跑后端时）：

    TOOL_SEARCH_ENDPOINT=http://127.0.0.1:8800/search

容器内访问宿主机时改用 `http://host.docker.internal:8800/search`
（`deploy/compose.yaml` 已配 `extra_hosts`）。

参数：`--host`、`--port`、`--upstream`、`--timeout`。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USER_AGENT = "macp-agent/0.1 (+multi-agent-collaboration-platform)"
DEFAULT_UPSTREAM = "https://cn.bing.com/search"
"""Bing 的 RSS 输出（`?format=rss`）是免密钥且在国内网络可直连的检索入口。"""

SEARCH_PATH = "/search"
"""与 `TOOL_SEARCH_ENDPOINT` 拼查询串的方式一致：`{endpoint}?q=...&format=json`。"""

DISPLAY_SEPARATOR = " – "
"""替换标题内部的 `" - "`；见模块 docstring 的「标题里的 `" - "`」。"""

logger = logging.getLogger("search_gateway")


class UpstreamError(RuntimeError):
    """上游取数或解析失败。调用方据此类回 502，而不是假装「没有结果」。"""


def fetch_rss(upstream: str, query: str, timeout: float) -> str:
    """取上游 RSS 文本；传输层错误统一归一化为 `UpstreamError`。"""

    url = f"{upstream}?{urllib.parse.urlencode({'q': query, 'format': 'rss'})}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise UpstreamError(f"上游返回 HTTP {exc.code}：{upstream}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpstreamError(f"上游不可达（{exc}）：{upstream}") from exc


def parse_rss(body: str) -> list[dict[str, str]]:
    """把 Bing RSS 解析成有序结果列表；缺标题或链接的条目直接丢弃。

    根标签不是 `rss` 的一律当解析失败处理：上游返回拦截页时，body 往往是**合法
    XML/HTML**（`<html>...</html>`），`iter("item")` 会安静地返回空列表，把「被
    拦截」伪装成「零条结果」。合法的空结果集（`<rss>` 存在但没有 `<item>`）仍然
    返回空列表，那是真实语义。
    """

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise UpstreamError(f"上游返回的不是合法 RSS：{exc}") from exc
    if root.tag != "rss":
        raise UpstreamError(f"上游返回的不是 RSS 文档（根标签 {root.tag!r}）")

    results: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        results.append(
            {
                "title": title,
                "url": link,
                "snippet": (item.findtext("description") or "").strip(),
            }
        )
    return results


def to_instant_answer(query: str, results: list[dict[str, str]]) -> dict[str, Any]:
    """按 DuckDuckGo Instant Answer 契约组装响应（只填工具会读的字段）。"""

    return {
        "Heading": query,
        # 刻意留空：见模块 docstring「不填 AbstractText / AbstractURL」。
        "AbstractText": "",
        "AbstractURL": "",
        "RelatedTopics": [
            {
                "FirstURL": result["url"],
                "Text": (
                    f"{_safe_title(result['title'])} - {result['snippet']}"
                    if result["snippet"]
                    else _safe_title(result["title"])
                ),
            }
            for result in results
        ],
    }


def _safe_title(title: str) -> str:
    """标题内部的 `" - "` 换成 `" – "`，保证它是 `Text` 里**第一个**分隔符。"""

    return title.replace(" - ", DISPLAY_SEPARATOR)


class SearchGatewayHandler(BaseHTTPRequestHandler):
    """单端点只读网关；`upstream` / `timeout` 由 `main()` 或测试注入。"""

    upstream = DEFAULT_UPSTREAM
    timeout = 8.0
    server_version = "macp-search-gateway/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path not in {"/", SEARCH_PATH}:
            self._send_json(
                404, {"error": f"未知路径：{parsed.path}；搜索走 {SEARCH_PATH}"}
            )
            return

        query = (urllib.parse.parse_qs(parsed.query).get("q") or [""])[0].strip()
        if not query:
            self._send_json(400, {"error": "缺少查询参数 q"})
            return

        try:
            results = parse_rss(fetch_rss(type(self).upstream, query, type(self).timeout))
        except UpstreamError as exc:
            logger.warning("search_failed q=%s error=%s", query, exc)
            self._send_json(502, {"error": str(exc)})
            return

        logger.info("search_ok q=%s results=%d", query, len(results))
        self._send_json(200, to_instant_answer(query, results))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """默认实现写 stderr；转成 logging，避免与脚本自身的输出混在一起。"""

        logger.info("%s - %s", self.address_string(), format % args)


def build_server(
    host: str, port: int, *, upstream: str = DEFAULT_UPSTREAM, timeout: float = 8.0
) -> ThreadingHTTPServer:
    """构造（不启动）网关；`port=0` 时由系统分配空闲端口，供测试使用。"""

    handler = type(
        "ConfiguredSearchGatewayHandler",
        (SearchGatewayHandler,),
        {"upstream": upstream, "timeout": timeout},
    )
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bing RSS → DuckDuckGo 契约搜索网关")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8800, help="监听端口（默认 8800）")
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM,
        help=f"上游搜索入口（默认 {DEFAULT_UPSTREAM}，需支持 ?format=rss）",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="上游请求超时秒数")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    server = build_server(
        args.host, args.port, upstream=args.upstream, timeout=args.timeout
    )
    host, port = server.server_address[:2]
    logger.info("gateway_listening endpoint=http://%s:%s%s", host, port, SEARCH_PATH)
    logger.info(
        "point the tool at it with TOOL_SEARCH_ENDPOINT=http://%s:%s%s", host, port, SEARCH_PATH
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("gateway_stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

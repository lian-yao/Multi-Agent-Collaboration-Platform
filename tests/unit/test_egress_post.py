"""出网层新增的 POST 通道（ADR-037 §4）：请求体、附加头、重定向降级、私网仍拦。

起一个本地 HTTP 服务（`127.0.0.1`）并把它列进内部服务与端口白名单——既不碰外网，
又覆盖真实 socket 路径与重定向处理（与 `test_egress_fetch.py` 同一套做法）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.security.config import EgressSettings
from app.security.egress import EgressConfigError, EgressDenied, EgressPolicy, http_post


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/redirect307":
            self._send(307, b"", "text/plain", {"Location": "/echo"})
            return
        if self.path == "/redirect303":
            self._send(303, b"", "text/plain", {"Location": "/echo"})
            return
        self._json(
            {
                "method": "POST",
                "path": self.path,
                "body": body.decode("utf-8"),
                "auth": self.headers.get("Authorization", ""),
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        self._json(
            {
                "method": "GET",
                "path": self.path,
                "body": "",
                "auth": self.headers.get("Authorization", ""),
            }
        )

    def _json(self, payload: dict) -> None:
        self._send(200, json.dumps(payload).encode("utf-8"), "application/json")

    def _send(
        self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - 测试里不需要访问日志
        return


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _policy(base: str, **overrides) -> EgressPolicy:
    port = base.rsplit(":", 1)[1]
    settings = {
        "allowed_ports": f"443,{port}",
        "internal_hosts": "127.0.0.1",
        "max_response_bytes": 4096,
    }
    settings.update(overrides)
    return EgressPolicy(EgressSettings(_env_file=None, **settings))


def test_post_sends_json_body_and_extra_headers(server):
    policy = _policy(server)

    response = http_post(
        f"{server}/search_api/web_search",
        payload={"Query": "LangGraph"},
        headers={"Authorization": "Bearer k-test"},
        policy=policy,
    )

    echo = json.loads(response.body)
    assert response.status == 200
    assert echo["method"] == "POST"
    assert json.loads(echo["body"]) == {"Query": "LangGraph"}
    assert echo["auth"] == "Bearer k-test"


def test_post_keeps_method_and_body_across_307(server):
    policy = _policy(server)

    echo = json.loads(
        http_post(f"{server}/redirect307", payload={"Query": "x"}, policy=policy).body
    )

    assert echo["method"] == "POST"
    assert json.loads(echo["body"]) == {"Query": "x"}


def test_post_downgrades_to_get_on_303(server):
    """303 的标准语义是「去 GET 那个资源」——请求体必须丢掉，否则会重复提交。"""

    policy = _policy(server)

    echo = json.loads(
        http_post(f"{server}/redirect303", payload={"Query": "x"}, policy=policy).body
    )

    assert echo["method"] == "GET"
    assert echo["body"] == ""


def test_private_targets_are_still_blocked_for_post(server):
    """POST 不是旁路：把内部服务列表清空后，同一个本地地址立刻按私网拒绝。"""

    policy = _policy(server, internal_hosts="")

    with pytest.raises(EgressDenied) as excinfo:
        http_post(f"{server}/echo", payload={"Query": "x"}, policy=policy)

    assert excinfo.value.reason == "private_ip"


def test_forbidden_headers_cannot_be_overridden(server):
    """附加头不能改 `Host` / `User-Agent` / `Content-Length`——前两个决定钉扎与反爬语义。"""

    policy = _policy(server)

    with pytest.raises(EgressConfigError):
        http_post(
            f"{server}/echo",
            payload={"Query": "x"},
            headers={"Host": "evil.example"},
            policy=policy,
        )

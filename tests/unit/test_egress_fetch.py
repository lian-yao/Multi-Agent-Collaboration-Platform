"""出网取数：钉扎连接、逐跳重校验重定向、响应上限（ADR-034 §2）。

真起一个本地 HTTP 服务（`127.0.0.1`），把它列进内部服务并放行端口——这样既不碰外网，
又能覆盖真实 socket 路径与重定向处理。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.observability.metrics import render_prometheus_metrics
from app.security.config import EgressSettings
from app.security.egress import EgressDenied, EgressPolicy, fetch_json, http_get


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/json"):
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/json")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif self.path == "/big":
            self._send(200, b"x" * 5000, "text/plain")
        else:
            self._send(404, b"{}", "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
        "max_response_bytes": 1024,
    }
    settings.update(overrides)
    return EgressPolicy(EgressSettings(_env_file=None, **settings))


def test_fetch_json_reaches_an_allowed_host(server):
    policy = _policy(server)

    assert fetch_json(f"{server}/json", policy=policy) == {"ok": True}


def test_redirects_are_followed_after_revalidation(server):
    policy = _policy(server)

    assert fetch_json(f"{server}/redirect", policy=policy) == {"ok": True}


def test_redirect_loops_stop_at_the_configured_limit(server):
    policy = _policy(server, max_redirects=3)

    with pytest.raises(EgressDenied) as excinfo:
        http_get(f"{server}/loop", policy=policy)

    assert excinfo.value.reason == "redirect_limit"


def test_oversized_responses_are_truncated_not_buffered_forever(server):
    policy = _policy(server)

    response = http_get(f"{server}/big", policy=policy)

    assert len(response.body) == 1024
    assert response.truncated is True


def test_blocked_targets_never_open_a_socket(server):
    """把内部服务列表清空后，同一个本地地址立刻被判定为私网并拒绝。"""

    policy = _policy(server, internal_hosts="")

    with pytest.raises(EgressDenied) as excinfo:
        fetch_json(f"{server}/json", policy=policy)

    assert excinfo.value.reason == "private_ip"


def test_rejections_show_up_in_the_prometheus_probe(server):
    policy = _policy(server, internal_hosts="")

    with pytest.raises(EgressDenied):
        http_get(f"{server}/json", policy=policy)

    assert b"macp_egress_blocked_total" in render_prometheus_metrics()

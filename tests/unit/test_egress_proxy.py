"""强制出网代理（`scripts/egress_proxy.py`，ADR-034 §4）。

真起代理与真起上游：CONNECT 走一个回显 socket，明文 HTTP 走一个本地 HTTP 服务。
代理是**硬边界**，所以用例的重点是"拒绝发生在代理侧"——即使客户端策略放行，
到了代理这里仍要按代理自己的策略判一次。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.security.config import EgressSettings
from app.security.egress import EgressPolicy
from scripts.egress_proxy import build_server


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"via": "target"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        """回显方法与请求体：用来钉住代理**真的把 POST 转过去**（ADR-037 §4）。"""

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(
            {
                "via": "target",
                "method": "POST",
                "body": raw.decode("utf-8"),
                "auth": self.headers.get("Authorization", ""),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover
        return


@contextlib.contextmanager
def _target():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _echo():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            try:
                data = client.recv(1024)
                client.sendall(b"echo:" + data)
            finally:
                client.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        server.close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _proxy(**settings):
    resolved = {"allowed_ports": "443,80", "internal_hosts": ""}
    resolved.update(settings)
    policy = EgressPolicy(EgressSettings(_env_file=None, **resolved))
    server = build_server("127.0.0.1", 0, policy=policy, timeout=5.0, max_bytes=1024)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], policy
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _plain_get(port: int, target: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _plain_post(
    port: int, target: str, payload: dict, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    try:
        connection.request("POST", target, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _connect(port: int, target: str) -> tuple[str, socket.socket]:
    """手工发一条 CONNECT，返回状态行与已建立的隧道 socket。"""

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
    )
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(1)
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("latin-1"), sock


# --------------------------------------------------------------------------- #
# CONNECT（HTTPS 隧道）
# --------------------------------------------------------------------------- #


def test_connect_to_a_private_host_is_denied_at_the_proxy():
    with _proxy() as (port, policy):
        status_line, sock = _connect(port, "127.0.0.1:443")
        sock.close()

    assert "403" in status_line
    assert policy.blocked.get("private_ip") == 1


def test_connect_tunnels_to_an_allowed_host():
    with _echo() as echo_port, _proxy(
        allowed_ports=f"443,{echo_port}", internal_hosts="127.0.0.1"
    ) as (port, _policy):
        status_line, sock = _connect(port, f"127.0.0.1:{echo_port}")
        sock.sendall(b"ping")
        reply = sock.recv(64)
        sock.close()

    assert "200" in status_line
    assert reply == b"echo:ping", "隧道要真的把字节转过去"


# --------------------------------------------------------------------------- #
# 明文 HTTP（绝对 URI）
# --------------------------------------------------------------------------- #


def test_absolute_uri_request_is_relayed_for_an_allowed_host():
    with _target() as target_port, _proxy(
        allowed_ports=f"443,{target_port}", internal_hosts="127.0.0.1"
    ) as (port, _policy):
        status, body = _plain_get(port, f"http://127.0.0.1:{target_port}/data")

    assert status == 200
    assert body == b'{"via": "target"}'


def test_absolute_uri_request_to_a_private_host_is_denied():
    with _proxy() as (port, policy):
        status, body = _plain_get(port, "http://10.0.0.5/secret")

    assert status == 403
    assert "private_ip" in body.decode("utf-8")
    assert policy.blocked.get("private_ip") == 1


def test_absolute_uri_post_is_relayed_with_body_and_auth_header():
    """豆包搜索是 POST + `Authorization`（ADR-037 §4）：代理必须原样转过去。"""

    with _target() as target_port, _proxy(
        allowed_ports=f"443,{target_port}", internal_hosts="127.0.0.1"
    ) as (port, _policy):
        status, body = _plain_post(
            port,
            f"http://127.0.0.1:{target_port}/search_api/web_search",
            {"Query": "LangGraph"},
            headers={"Authorization": "Bearer k-test"},
        )

    echo = json.loads(body)
    assert status == 200
    assert echo["method"] == "POST"
    assert json.loads(echo["body"]) == {"Query": "LangGraph"}
    assert echo["auth"] == "Bearer k-test"


def test_absolute_uri_post_to_a_private_host_is_denied():
    """代理是硬边界：POST 同样要在代理侧被拦下（不是只拦 GET）。"""

    with _proxy() as (port, policy):
        status, body = _plain_post(port, "http://10.0.0.5/secret", {"Query": "x"})

    assert status == 403
    assert "private_ip" in body.decode("utf-8")
    assert policy.blocked.get("private_ip") == 1


def test_relative_uri_is_rejected_with_400():
    """相对路径说明调用方没把它当代理用；静默当成自建服务会更难查。"""

    with _proxy() as (port, _policy):
        status, _body = _plain_get(port, "/health")

    assert status == 400

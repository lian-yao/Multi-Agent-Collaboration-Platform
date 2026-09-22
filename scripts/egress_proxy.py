"""强制出网代理：把「谁在出网」从应用层约定变成网络层事实（ADR-034 §4）。

为什么需要一个代理，而不是继续在应用层判定：

- 应用层挡得住我们自己的代码，挡不住任何一处直连（新工具、第三方库、MCP/模型 SDK
  内部的 httpx 连接）；
- 具体到本项目：MCP 的 streamable-http/sse 客户端与模型 SDK 都自建连接、不接受自定义
  transport，所以**没法在它们内部做 IP 钉扎**。把请求交给代理之后，SDK 只认识
  `HTTP_PROXY`/`HTTPS_PROXY`，而"解析域名 + 判私网 + 钉住 IP 去连接"这三件事整段落在这里。

用法（容器里由 compose 拉起）：

    python scripts/egress_proxy.py --host 0.0.0.0 --port 8888

它复用 `app.security.egress` 的判定与钉扎实现：本进程的 `EGRESS_PROXY_URL` 必须留空，
这样策略走的是"自己解析 + 钉扎"那条路径（代理再转发给别的代理等于绕了一圈）。

**内部服务列表在本进程应留空**：代理只该被用来访问公网。若把 `redis`、`postgres` 这类
内部服务写进 `EGRESS_INTERNAL_HOSTS`，它们就变成"任何能连到代理的容器都能探测的目标"——
而内部服务本来就该由客户端直连（`NO_PROXY` 排除）。
"""

from __future__ import annotations

import argparse
import logging
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 与 `scripts/verify_ollama.py` 同一写法：脚本以文件路径运行时，cwd 不会自动进
# `sys.path`，`import app...` 会直接失败。显式把项目根加进来。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.security.egress import (
    EgressConfigError,
    EgressDenied,
    EgressPolicy,
    get_egress_policy,
    http_get,
)

logger = logging.getLogger("egress_proxy")

RELAY_CHUNK_BYTES = 65536
DENIED_STATUS = 403
UPSTREAM_ERROR_STATUS = 502


class EgressProxyHandler(BaseHTTPRequestHandler):
    """两种形态：HTTPS 走 `CONNECT` 隧道，明文 HTTP 走绝对 URI 转发。"""

    protocol_version = "HTTP/1.1"
    server_version = "macp-egress-proxy/0.1"

    policy: EgressPolicy = None  # type: ignore[assignment]  # 由 build_server 注入
    timeout = 15.0
    max_bytes = 1_000_000
    tunnel_idle_seconds = 60.0

    # -- HTTPS：CONNECT 隧道 ------------------------------------------------- #

    def do_CONNECT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        host, _, port_text = self.path.partition(":")
        try:
            port = int(port_text or "443")
        except ValueError:
            self._send_error(400, "CONNECT 目标端口不是数字")
            return
        url = f"https://{host}:{port}/"
        try:
            target = type(self).policy.evaluate(url, purpose="proxy")
        except EgressDenied as exc:
            logger.warning("connect_denied path=%s reason=%s", self.path, exc.reason)
            self._send_error(DENIED_STATUS, f"被出网策略拒绝（{exc.reason}）：{exc.detail}")
            return

        try:
            upstream = socket.create_connection(
                (target.addresses[0], port), timeout=type(self).timeout
            )
        except OSError as exc:
            logger.warning("connect_failed path=%s error=%s", self.path, exc)
            self._send_error(UPSTREAM_ERROR_STATUS, f"上游连接失败：{exc}")
            return

        self.send_response(200, "Connection established")
        self.end_headers()
        try:
            self._relay(upstream)
        finally:
            upstream.close()

    def _relay(self, upstream: socket.socket) -> None:
        """双向转发，直到任一侧关闭或空闲超时。"""

        downstream: Any = self.connection
        sockets = [downstream, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], type(self).tunnel_idle_seconds)
            if not readable:
                logger.info("tunnel_idle_close path=%s", self.path)
                return
            for source in readable:
                try:
                    data = source.recv(RELAY_CHUNK_BYTES)
                except OSError:
                    return
                if not data:
                    return
                other = upstream if source is downstream else downstream
                try:
                    other.sendall(data)
                except OSError:
                    return

    # -- 明文 HTTP：绝对 URI 转发 ------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.lower().startswith(("http://", "https://")):
            # 代理语义要求绝对 URI；相对路径说明调用方没把它当代理用。
            self._send_error(400, "代理只接受绝对 URI 的请求")
            return
        try:
            response = http_get(
                self.path,
                purpose="proxy",
                policy=type(self).policy,
                timeout=type(self).timeout,
                max_bytes=type(self).max_bytes,
            )
        except EgressDenied as exc:
            logger.warning("request_denied url=%s reason=%s", self.path, exc.reason)
            self._send_error(DENIED_STATUS, f"被出网策略拒绝（{exc.reason}）：{exc.detail}")
            return

        self.send_response(response.status)
        for key, value in response.headers.items():
            if key in {"connection", "transfer-encoding", "content-length"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def _send_error(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def build_server(
    host: str,
    port: int,
    *,
    policy: EgressPolicy,
    timeout: float = 15.0,
    max_bytes: int = 1_000_000,
) -> ThreadingHTTPServer:
    """构造（不启动）代理；`port=0` 时由系统分配空闲端口，供测试使用。"""

    handler = type(
        "ConfiguredEgressProxyHandler",
        (EgressProxyHandler,),
        {"policy": policy, "timeout": timeout, "max_bytes": max_bytes},
    )
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="强制出网代理（ADR-034）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8888, help="监听端口")
    parser.add_argument("--timeout", type=float, default=15.0, help="上游超时秒数")
    parser.add_argument("--max-bytes", type=int, default=1_000_000, help="单次响应上限")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        policy = get_egress_policy()
    except EgressConfigError as exc:
        logger.error("egress_proxy.invalid_config error=%s", exc)
        return 2
    if policy.settings.proxy_url:
        logger.error(
            "egress_proxy.proxy_loop EGRESS_PROXY_URL=%s：代理进程自己不能再配代理",
            policy.settings.proxy_url,
        )
        return 2

    server = build_server(
        args.host,
        args.port,
        policy=policy,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )
    host, port = server.server_address[:2]
    logger.info("egress_proxy.listening endpoint=http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("egress_proxy.stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

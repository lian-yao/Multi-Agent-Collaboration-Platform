"""Provider 远端模型发现（`doc/api.md` §5.10、ADR-017 §4）。

职责边界：

- 本模块只负责「按协议族探测远端模型清单」这一次出站请求；
- 落库与幂等由 `app/core/model_registry.py` 的批量导入负责；
- HTTP 契约与错误码归一化由 `app/api/main.py` 负责。

设计要点：

1. **服务端执行**：由 API 进程发起，浏览器不直连 Provider —— 否则会撞 CORS
   并把 `api_key` 暴露给浏览器网络层。
2. **零新增运行时依赖**：用标准库 `urllib.request`，不引入 `httpx`（后者当前只在
   dev 依赖组，见 ADR-017 §7）。
3. **可注入**：`fetcher` 参数让测试完全离线，不需要起任何 HTTP 服务。
4. **凭据不外泄**：错误信息只含主机名与状态码，不含 `api_key`、不含 query。
5. **响应体有上限**：默认 2 MiB，避免被超大响应打爆内存。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

ANTHROPIC_VERSION = "2023-06-01"

__all__ = [
    "DiscoveredModel",
    "ModelDiscoveryError",
    "PROVIDER_DISCOVERY_SUPPORTED_API_TYPES",
    "discover_models",
    "default_json_fetcher",
]


class ModelDiscoveryError(RuntimeError):
    """远端发现失败（API 层据此返回 502 `PROVIDER_DISCOVERY_FAILED`）。

    `message` 必须已经脱敏：不含凭据、不含完整 URL 的 query。
    """


@dataclass(frozen=True)
class DiscoveredModel:
    """远端返回的一个模型条目。"""

    id: str
    name: str
    owned_by: str | None = None


Fetcher = Callable[..., Any]
"""取 JSON 的可注入实现，签名为 `fetch(url, headers, *, timeout, max_bytes) -> Any`。

测试用替身需要接受同样的关键字参数（可直接 `**kwargs` 吞掉）。
"""

PROVIDER_DISCOVERY_SUPPORTED_API_TYPES = frozenset(
    {"openai-compatible", "openai-responses", "anthropic", "gemini"}
)
"""支持自动发现的协议族；`amazon-bedrock` 需要签名且无统一清单端点，故不支持。"""


def default_json_fetcher(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Any:
    """取 JSON 并解析；非 2xx、超时、JSON 损坏都归一化为 `ModelDiscoveryError`。"""

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ModelDiscoveryError(
            f"远端返回 HTTP {exc.code}（{_origin(url)}）"
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelDiscoveryError(f"无法连接 {_origin(url)}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ModelDiscoveryError(f"连接 {_origin(url)} 超时") from exc
    except OSError as exc:
        raise ModelDiscoveryError(f"连接 {_origin(url)} 失败：{exc}") from exc

    if len(raw) > max_bytes:
        raise ModelDiscoveryError("远端响应体积超过上限，拒绝解析")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModelDiscoveryError("远端响应不是合法 JSON") from exc


def _origin(url: str) -> str:
    """只保留 scheme://host:port，用于错误信息脱敏。"""

    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def _candidate_urls(api_type: str, preset_type: str, base_url: str) -> list[str]:
    """按协议族给出探测路径的候选（按顺序尝试，首个成功即用）。"""

    if preset_type == "ollama":
        return [f"{base_url}/api/tags"]
    if api_type in {"openai-compatible", "openai-responses"}:
        urls = [f"{base_url}/models"]
        if not base_url.endswith("/v1"):
            urls.append(f"{base_url}/v1/models")
        else:
            # 用户已写到 /v1，再补一个去掉 /v1 的变体，兼容少数网关。
            urls.append(f"{base_url[:-3]}/models")
        return urls
    if api_type == "anthropic":
        return [f"{base_url}/v1/models"]
    if api_type == "gemini":
        base = base_url if base_url.endswith("/v1beta") else f"{base_url}/v1beta"
        return [f"{base}/models"]
    return []


def _build_headers(
    api_type: str,
    preset_type: str,
    api_key: str | None,
    custom_headers: dict[str, str] | None,
) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    key = (api_key or "").strip()
    if key:
        if api_type == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        elif api_type == "gemini":
            headers["x-goog-api-key"] = key
        elif preset_type != "ollama":
            headers["Authorization"] = f"Bearer {key}"
    for name, value in (custom_headers or {}).items():
        headers[str(name)] = str(value)
    return headers


def _extract_models(payload: Any) -> list[DiscoveredModel]:
    """把各家响应归一化成 `DiscoveredModel` 列表。

    兼容三种常见形态：OpenAI 的 `{"data": [...]}`、Gemini 的 `{"models": [...]}`、
    以及少数网关直接返回顶层数组。
    """

    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                entries = value
                break
        else:
            entries = []
    else:
        entries = []

    models: list[DiscoveredModel] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                models.append(DiscoveredModel(id=name, name=name))
            continue
        if not isinstance(entry, dict):
            continue
        # Gemini 用 `name: "models/gemini-1.5-pro"`，调用时要剥掉前缀。
        raw_id = entry.get("id") or entry.get("name") or entry.get("model")
        if not isinstance(raw_id, str):
            continue
        identifier = raw_id.strip()
        if identifier.startswith("models/"):
            identifier = identifier[len("models/") :]
        if not identifier:
            continue
        display = entry.get("display_name") or entry.get("displayName") or identifier
        owned_by = entry.get("owned_by") or entry.get("ownedBy")
        models.append(
            DiscoveredModel(
                id=identifier,
                name=str(display),
                owned_by=str(owned_by) if isinstance(owned_by, str) else None,
            )
        )
    return models


def discover_models(
    provider: dict[str, Any],
    *,
    fetcher: Fetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[DiscoveredModel], str]:
    """探测 Provider 的可用模型，返回 `(models, source_url)`。

    `provider` 是 `llm_providers` 行的 dict（需要 `api_type` / `preset_type` /
    `base_url` / `api_key` / `custom_headers`）。候选端点按序尝试，全部失败时抛出
    最后一次的 `ModelDiscoveryError`，并附上已尝试的脱敏地址列表。
    """

    api_type = str(provider.get("api_type") or "openai-compatible")
    preset_type = str(provider.get("preset_type") or "openai-compatible")
    base_url = _normalize_base_url(str(provider.get("base_url") or ""))

    if not base_url:
        raise ModelDiscoveryError("该 Provider 未配置 base_url，无法发现模型")
    if not base_url.startswith(("http://", "https://")):
        raise ModelDiscoveryError("base_url 必须是 http(s) 地址")
    if api_type not in PROVIDER_DISCOVERY_SUPPORTED_API_TYPES:
        raise ModelDiscoveryError(
            f"协议族 {api_type} 不支持自动发现模型，请手工录入模型名"
        )

    candidates = _candidate_urls(api_type, preset_type, base_url)
    if not candidates:
        raise ModelDiscoveryError(
            f"协议族 {api_type} 不支持自动发现模型，请手工录入模型名"
        )

    headers = _build_headers(
        api_type, preset_type, provider.get("api_key"), provider.get("custom_headers")
    )
    fetch = fetcher or default_json_fetcher

    attempted: list[str] = []
    last_error: ModelDiscoveryError | None = None
    for url in candidates:
        attempted.append(_origin(url) + _path(url))
        try:
            payload = fetch(url, headers, timeout=timeout, max_bytes=max_bytes)
        except ModelDiscoveryError as exc:
            last_error = exc
            continue
        models = _extract_models(payload)
        if models:
            return _dedupe(models), url
        last_error = ModelDiscoveryError("远端返回的模型清单为空")

    detail = f"；已尝试 {'、'.join(attempted)}" if attempted else ""
    reason = str(last_error) if last_error else "未知原因"
    raise ModelDiscoveryError(f"{reason}{detail}")


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def _dedupe(models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """按 id 去重并保持远端返回顺序。"""

    seen: set[str] = set()
    unique: list[DiscoveredModel] = []
    for model in models:
        if model.id in seen:
            continue
        seen.add(model.id)
        unique.append(model)
    return unique

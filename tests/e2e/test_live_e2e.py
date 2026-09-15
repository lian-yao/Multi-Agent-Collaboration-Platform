"""E 系列端到端验收：跑在**真实 compose 环境**上（成员 C D9-10）。

对齐 `doc/testing.md` §1/§2.3：端到端用例的验收环境是完整 compose（真实 Dapr +
PostgreSQL + Redis + Ollama），本文件是该环境的验收入口，覆盖：

- E-05（健康检查部分）：`/health`、`/api/v1/agents`、`/api/v1/providers`；
- E-01/E-02：创建会话 → 发消息 → 轮询 Workflow → 三步依次完成并生成报告，
  阶段完成情况取自 PostgreSQL 回写的 `workflow_runs.checkpoint`；
- I-06（真实库落库部分）：`tool_calls` 表存在时 `/workflows/{id}/tool-calls` 可读；
- E-04（API 侧）：暂停会话后新消息被 409 拒绝，恢复后可继续。

成员 D 的 D9-10 在此基础上补 Web 侧两条（同一文件、同一环境变量开关）：

- E-05（Web 侧）：`frontend` 容器可访问、SPA 入口引用的构建产物可获取、
  nginx 把 `/api` 反代到 `backend`，且工作台首屏调用的只读目录接口经反代可用；
- E-04（Web 侧）：复刻 `App.tsx` 的会话管理调用序列（`POST /sessions` 新建任务 →
  `POST /pause` → `POST /resume`，以及暂停期间提交被 409 拒绝），全部经 nginx 反代。
  **不覆盖**发消息后的三步流水线：那需要可用的模型提供方与凭据，
  浏览器里的渲染效果也仍需人工按 `doc/deployment.md` 的核对清单确认。

运行前提（`deploy/start.ps1` 或 `docker compose up -d --wait` 已起全栈）：

```bash
MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s
```

默认**跳过**，因此 `uv run pytest` 在无容器环境下仍然是全绿的可回归用例集
（无容器回归网见 `test_pipeline_e2e.py`）。
提供方：默认已是 OpenAI 兼容 API（ADR-014，缺凭据 fail-fast），需先配好凭据；
本文件最初测于本地 Ollama（Qwen2.5-Coder 7B，首次调用含模型加载 8-12s、
之后单条三步流水线 2-4s），该提供方下报告会退化成工具调用 JSON 文本（ADR-016 F-02），
因此 E-01/E-02 的验收口径以 API 提供方为准。
超时可用 `MACP_E2E_TIMEOUT`（秒，默认 300）覆盖，环境地址用
`MACP_E2E_BASE_URL`（默认 `http://localhost:8000`）；
Web 侧用例走 nginx 反代，地址用 `MACP_E2E_FRONTEND_URL`（默认 `http://localhost:5173`）。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
import pytest

BASE_URL = os.getenv("MACP_E2E_BASE_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("MACP_E2E_FRONTEND_URL", "http://localhost:5173")
POLL_TIMEOUT_SECONDS = float(os.getenv("MACP_E2E_TIMEOUT", "300"))
POLL_INTERVAL_SECONDS = float(os.getenv("MACP_E2E_POLL_INTERVAL", "2"))
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STAGES = ("collect", "analyze", "report")
ROLES = ["collector", "analyst", "reporter"]

pytestmark = pytest.mark.skipif(
    os.getenv("MACP_E2E_LIVE", "").strip().lower() not in {"1", "true", "yes"},
    reason="需要完整 compose 环境（deploy/start.ps1）；用 MACP_E2E_LIVE=1 启用",
)


@pytest.fixture(scope="module")
def live_client() -> httpx.Client:
    """真实环境客户端；不设置 read timeout，由用例自身控制轮询上限。"""

    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        yield client


@pytest.fixture(scope="module")
def web_client() -> httpx.Client:
    """浏览器实际访问的入口：nginx 托管的 Web UI 与 `/api` 反向代理。

    与 `live_client` 的区别只在端口：Web UI 与 API 同源（`deploy/docker/nginx.conf`），
    因此这两条用例同时也是「前端容器 + 反代」这一段的可用性证据。
    """

    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    with httpx.Client(base_url=FRONTEND_URL, timeout=timeout) as client:
        yield client


def _create_session(client: httpx.Client, user_id: str = "c-e2e-live") -> str:
    response = client.post("/api/v1/sessions", json={"user_id": user_id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _send_message(client: httpx.Client, session_id: str, content: str) -> str:
    response = client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": content}
    )
    assert response.status_code == 202, response.text
    return response.json()["workflow_id"]


def _wait_for_terminal(
    client: httpx.Client, workflow_id: str
) -> tuple[dict[str, Any], float]:
    """轮询 `GET /workflows/{id}` 直到终态，返回 (Workflow, 耗时秒)。"""

    started = time.perf_counter()
    deadline = started + POLL_TIMEOUT_SECONDS
    last: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        response = client.get(f"/api/v1/workflows/{workflow_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in TERMINAL_STATUSES:
            return last, round(time.perf_counter() - started, 1)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"Workflow {workflow_id} 未在 {POLL_TIMEOUT_SECONDS}s 内到达终态：{last}"
    )


def test_live_health_and_agent_catalog(live_client: httpx.Client) -> None:
    """E-05：整套 compose 起完后，健康检查与只读目录接口可用。"""

    health = live_client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    agents = live_client.get("/api/v1/agents")
    assert agents.status_code == 200, agents.text
    roles = [item["role"] for item in agents.json()["items"]]
    assert roles == ["collector", "analyst", "reporter"]

    providers = live_client.get("/api/v1/providers")
    assert providers.status_code == 200, providers.text
    assert providers.json()["items"], "真实环境应至少暴露一个已配置 Provider"


def test_live_e01_e02_three_stage_pipeline(live_client: httpx.Client) -> None:
    """E-01/E-02：真实 Dapr + PostgreSQL 上三步依次完成并回写报告消息。"""

    session_id = _create_session(live_client)
    workflow_id = _send_message(
        live_client, session_id, "请分析多智能体协作平台的核心要点并生成一份简报"
    )

    workflow, elapsed = _wait_for_terminal(live_client, workflow_id)
    print(f"\n[E-02] workflow={workflow_id} status={workflow['status']} 耗时={elapsed}s")
    assert workflow["status"] == "completed", workflow
    assert workflow["completed_at"] is not None

    # 三步完成情况来自 PostgreSQL 回写的 checkpoint（finalize 活动写入）。
    # 三个阶段全部完成后 current_step 归零（没有下一个待执行步骤）。
    checkpoint = workflow.get("checkpoint") or {}
    print(f"[E-02] checkpoint={checkpoint}")
    assert checkpoint.get("completed_steps") == list(STAGES)
    assert checkpoint.get("current_step") is None

    messages = live_client.get(f"/api/v1/sessions/{session_id}/messages").json()
    assert messages["total"] == 2, messages
    user_message, report = messages["items"]
    assert user_message["role"] == "user"
    assert user_message["status"] == "completed"
    assert report["role"] == "assistant"
    assert report["status"] == "completed"
    assert report["content"].strip(), "报告消息不应为空"
    # 排除「配置回落成确定性假模型」导致的空跑：真实模型的报告不会等于假模型输出。
    assert report["content"] != f"report: 请分析多智能体协作平台的核心要点并生成一份简报"
    print(f"[E-02] 报告消息长度={len(report['content'])} 字符")
    print(f"[E-02] 报告内容={report['content'][:400]}")


def _looks_like_bare_tool_call(content: str) -> bool:
    """判断阶段输出是否是「模型把工具调用当纯文本吐出来」的载荷。"""

    text = content.strip()
    if not text.startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and "name" in payload and "arguments" in payload


def test_live_report_is_a_report_not_a_tool_call_payload(
    live_client: httpx.Client,
) -> None:
    """E-01/E-02 的通过标准「生成结构化报告」。

    原本是 strict xfail：2026-09-15 在 Ollama `qwen2.5-coder:7b` 下，报告消息内容是
    `{"name": "web_search", "arguments": {"query": "…", "max_results": 5}}` 这样的裸
    JSON——模型把工具调用写进了 `content` 而不是 `tool_calls`，工具从未真正执行
    （缺口 F-02）。默认提供方改为 OpenAI 兼容 API 后已复测通过：`deepseek-flash`
    两阶段各产生一次 `calculator` 调用，报告正文引用返回值 `42`（ADR-016 F-02）。
    因此这里改成正式断言——**它按 API 提供方验收 E-01/E-02**；若在 Ollama 提供方下
    跑，本用例会失败，那正是 F-02 记录的现象。
    """

    session_id = _create_session(live_client)
    workflow_id = _send_message(
        live_client, session_id, "请分析多智能体协作平台的核心要点并生成一份简报"
    )
    assert _wait_for_terminal(live_client, workflow_id)[0]["status"] == "completed"

    messages = live_client.get(f"/api/v1/sessions/{session_id}/messages").json()
    report = messages["items"][-1]["content"]
    print(f"\n[E-01/E-02] 报告内容抽样={report[:200]}")
    assert not _looks_like_bare_tool_call(report), (
        "报告消息仍是工具调用 JSON 文本（F-02 现象）：模型未输出结构化 tool_calls"
    )


def test_live_tool_calls_are_readable_from_postgresql(
    live_client: httpx.Client,
) -> None:
    """I-06（真实库部分）：`tool_calls` 表可读且按 Workflow 过滤。

    真实模型是否真的发起工具调用取决于提供方与模型（Ollama qwen2.5-coder:7b 下不发起，
    API 提供方下发起，见 ADR-016 F-02 的关闭依据），因此这里断言的是**落库链路可用**：
    表存在时 available、分页自洽、返回行都属于本次 Workflow；确有行时状态必须是终态。
    """

    session_id = _create_session(live_client)
    workflow_id = _send_message(live_client, session_id, "统计一段文本的词频")

    assert _wait_for_terminal(live_client, workflow_id)[0]["status"] == "completed"

    response = live_client.get(f"/api/v1/workflows/{workflow_id}/tool-calls")
    assert response.status_code == 200, response.text
    payload = response.json()
    print(
        f"\n[I-06] availability={payload['availability']} total={payload['total']}"
    )
    assert payload["availability"] == "available", (
        "真实 compose 环境应已建 tool_calls 表；not_integrated 说明建表或连接有缺口"
    )
    assert payload["total"] == len(payload["items"])
    for item in payload["items"]:
        assert item["workflow_run_id"] == workflow_id
        assert item["status"] in {"succeeded", "failed"}


def test_live_pause_blocks_messages_and_resume_recovers(
    live_client: httpx.Client,
) -> None:
    """E-04（API 侧）：会话暂停后拒绝新消息（409），恢复后重新可用。"""

    session_id = _create_session(live_client)
    workflow_id = _send_message(live_client, session_id, "先跑起来再暂停")

    paused = live_client.post(f"/api/v1/sessions/{session_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["session"]["status"] == "paused"

    blocked = live_client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": "暂停期间的消息"}
    )
    assert blocked.status_code == 409, blocked.text
    # 错误体形状见 `app/api/main.py` 的 ApiError 异常处理器：{code, message, request_id}。
    assert blocked.json()["code"] == "SESSION_PAUSED"

    resumed = live_client.post(f"/api/v1/sessions/{session_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["session"]["status"] == "active"

    # 恢复后原 Workflow 继续执行（E-03 的续跑语义在真实 Dapr 上的最小验证）。
    workflow, elapsed = _wait_for_terminal(live_client, workflow_id)
    print(f"\n[E-04] 恢复后 workflow={workflow_id} status={workflow['status']} 耗时={elapsed}s")
    assert workflow["status"] == "completed"


def test_live_frontend_serves_ui_and_proxies_api(web_client: httpx.Client) -> None:
    """E-05（Web 侧）：`frontend` 容器可访问，且 nginx 把 `/api` 反代到 backend。

    `start.ps1` 的 `Wait-HttpHealth "Frontend"` 只证明容器起来了；页面能否真正加载
    还取决于构建产物是否随镜像同步、反代是否通。工作台首屏就要调 `/agents` 与
    `/providers`，因此这两条一起验。
    """

    index = web_client.get("/")
    assert index.status_code == 200, index.text
    assert '<div id="root"></div>' in index.text
    assert "<title>Agent 协作工作台</title>" in index.text

    # SPA 入口引用的构建产物必须真的取得到：dist 没随镜像更新时页面白屏，
    # 而 index.html 本身仍然返回 200，只看首页会漏掉这种情况。
    entry = re.search(r'src="(/assets/[^"]+\.js)"', index.text)
    assert entry, f"index.html 未引用构建产物：{index.text}"
    bundle = web_client.get(entry.group(1))
    assert bundle.status_code == 200, bundle.text
    assert len(bundle.content) > 0

    agents = web_client.get("/api/v1/agents")
    assert agents.status_code == 200, agents.text
    assert [item["role"] for item in agents.json()["items"]] == ROLES

    providers = web_client.get("/api/v1/providers")
    assert providers.status_code == 200, providers.text
    assert providers.json()["items"], "真实环境应至少暴露一个已配置 Provider"

    # 「工具与配置」页的三块数据（Provider 表单 / 工具目录 / 全局指标采样）都要经反代可用；
    # availability 必须由 backend 自己报，不能靠前端把「未接入」当「零条记录」显示。
    provider_config = web_client.get("/api/v1/config/provider")
    assert provider_config.status_code == 200, provider_config.text
    assert "api_key" not in provider_config.json(), "Provider 配置响应不得回传密钥"

    tools = web_client.get("/api/v1/tools")
    assert tools.status_code == 200, tools.text
    assert tools.json()["availability"] == "available", tools.json()
    assert len(tools.json()["items"]) == 4, tools.json()

    metrics = web_client.get("/api/v1/metrics")
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["availability"] == "available", metrics.json()


def test_live_web_ui_session_lifecycle_through_proxy(web_client: httpx.Client) -> None:
    """E-04（Web 侧，不依赖模型）：复刻 `App.tsx` 的会话管理调用序列。

    对应界面上的「新建任务」按钮与顶栏「暂停 / 恢复」按钮，全部经 nginx 反代；
    覆盖创建会话、空消息列表、暂停、暂停期提交被拒、恢复、回读六步。
    **不含**发送任务后的三步流水线与渲染结果——前者需要可用的模型提供方与凭据，
    后者需要人工按 `doc/deployment.md` 的核对清单在浏览器里确认。
    """

    created = web_client.post("/api/v1/sessions", json={"user_id": "d-e2e-web"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert created.json()["status"] == "active"

    empty = web_client.get(f"/api/v1/sessions/{session_id}/messages")
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0

    paused = web_client.post(f"/api/v1/sessions/{session_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["session"]["status"] == "paused"
    # 没有运行中的 Workflow 时只改会话状态，不回带 Workflow（app/api/main.py:412）。
    assert paused.json()["workflow"] is None

    # 输入框在会话暂停时被 disable；用户绕过界面直接提交时后端仍必须拒绝。
    blocked = web_client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": "暂停期间的消息"}
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "SESSION_PAUSED"

    resumed = web_client.post(f"/api/v1/sessions/{session_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["session"]["status"] == "active"

    reread = web_client.get(f"/api/v1/sessions/{session_id}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["status"] == "active"

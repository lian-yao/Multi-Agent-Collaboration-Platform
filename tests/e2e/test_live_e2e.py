"""E 系列端到端验收：跑在**真实 compose 环境**上（成员 C D9-10）。

对齐 `doc/testing.md` §1/§2.3：端到端用例的验收环境是完整 compose（真实 Dapr +
PostgreSQL + Redis + Ollama），本文件是该环境的验收入口，覆盖：

- E-05（健康检查部分）：`/health`、`/api/v1/agents`、`/api/v1/providers`；
- E-01/E-02：创建会话 → 发消息 → 轮询 Workflow → 三步依次完成并生成报告，
  阶段完成情况取自 PostgreSQL 回写的 `workflow_runs.checkpoint`；
- I-06（真实库落库部分）：`tool_calls` 表存在时 `/workflows/{id}/tool-calls` 可读；
- E-04（API 侧）：暂停会话后新消息被 409 拒绝，恢复后可继续。

运行前提（`deploy/start.ps1` 或 `docker compose up -d --wait` 已起全栈）：

```bash
MACP_E2E_LIVE=1 uv run pytest tests/e2e/test_live_e2e.py -q -s
```

默认**跳过**，因此 `uv run pytest` 在无容器环境下仍然是全绿的可回归用例集
（无容器回归网见 `test_pipeline_e2e.py`）。
真实模型为本地 Ollama（Qwen2.5-Coder 7B）：首次调用需加载模型（约 8-12s），
之后单条三步流水线实测 2-4s；超时可用 `MACP_E2E_TIMEOUT`（秒，默认 300）覆盖，
环境地址用 `MACP_E2E_BASE_URL`（默认 `http://localhost:8000`）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest

BASE_URL = os.getenv("MACP_E2E_BASE_URL", "http://localhost:8000")
POLL_TIMEOUT_SECONDS = float(os.getenv("MACP_E2E_TIMEOUT", "300"))
POLL_INTERVAL_SECONDS = float(os.getenv("MACP_E2E_POLL_INTERVAL", "2"))
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STAGES = ("collect", "analyze", "report")

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


@pytest.mark.xfail(
    strict=True,
    reason="已知缺口 F-02（ADR-013）：真实模型不产出结构化 tool_calls，"
    "阶段结论退化成工具调用 JSON 文本；修复后本用例会 XPASS，需改为正式断言",
)
def test_live_report_is_a_report_not_a_tool_call_payload(
    live_client: httpx.Client,
) -> None:
    """E-01/E-02 的通过标准「生成结构化报告」在真实模型下**尚未达成**。

    实测（2026-09-15，qwen2.5-coder:7b）：报告消息内容为
    `{"name": "web_search", "arguments": {"query": "…", "max_results": 5}}`——
    模型把工具调用写进了 `content` 而不是 `tool_calls`，因此
    `event=stage.finish ... tool_calls=0`，工具从未真正执行，最终报告是这串 JSON。
    证据与处置（不改代码、不换模型，仅上报）见 ADR-013 F-02。
    """

    session_id = _create_session(live_client)
    workflow_id = _send_message(
        live_client, session_id, "请分析多智能体协作平台的核心要点并生成一份简报"
    )
    assert _wait_for_terminal(live_client, workflow_id)[0]["status"] == "completed"

    messages = live_client.get(f"/api/v1/sessions/{session_id}/messages").json()
    report = messages["items"][-1]["content"]
    print(f"\n[F-02] 报告内容抽样={report[:200]}")
    assert not _looks_like_bare_tool_call(report), (
        "报告消息仍是工具调用 JSON 文本（F-02）：模型未输出结构化 tool_calls"
    )


def test_live_tool_calls_are_readable_from_postgresql(
    live_client: httpx.Client,
) -> None:
    """I-06（真实库部分）：`tool_calls` 表可读且按 Workflow 过滤。

    真实模型是否真的发起工具调用由模型能力决定（见 ADR-013 记录的缺口），
    因此这里断言的是**落库链路可用**：表存在时 available、分页自洽、
    返回行都属于本次 Workflow；确有行时状态必须是终态。
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

"""E 系列端到端回归（成员 C D9-10）：API → Workflow → 编排 → 工具 → 可观测。

对齐 `doc/testing.md` §2.3 的 E-01/E-02：创建会话 → 提交消息 → 轮询 Workflow →
拿到结构化回答与最终报告；`doc/15 AI Native多智能体协作平台.md` §六 的三步演示链路。

替身边界见 `conftest.py`：只把 Dapr 运行时与 PostgreSQL 落库换成进程内实现，
API 路由、Workflow/活动代码、三步 LangGraph 流水线、MCP 注册表与 4 个内置工具、
可观测采集都是真代码。真实 Dapr + PostgreSQL 上的验收见 `test_live_e2e.py`。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.api.main import app
from app.observability.metrics import (
    METRIC_STAGE_RUNS,
    METRIC_TOOL_CALLS,
    METRIC_WORKFLOW_RUNS,
)
from app.orchestration import pipeline_graph

# `E2EEnvironment` 由 tests/e2e/conftest.py 的同名 fixture 提供；这里只作类型标注。
STAGES = ("collect", "analyze", "report")
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class FailingStageModel(BaseChatModel):
    """假模型：每次调用都失败，用于验证失败链路的终态回写。"""

    @property
    def _llm_type(self) -> str:
        return "failing-stage-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        raise RuntimeError("模型服务不可用")


def _start_session(client: TestClient, content: str) -> tuple[str, str]:
    session_id = client.post("/api/v1/sessions", json={"user_id": "e2e"}).json()["id"]
    accepted = client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": content}
    )
    assert accepted.status_code == 202
    return session_id, accepted.json()["workflow_id"]


def _wait_for_terminal(
    client: TestClient, workflow_id: str, timeout: float = 30.0
) -> dict[str, Any]:
    """轮询 Workflow 直到终态——与前端 `doc/api.md` §1 的轮询语义一致。"""

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/workflows/{workflow_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in TERMINAL_STATUSES:
            return last
        time.sleep(0.02)
    raise AssertionError(f"Workflow 未在 {timeout}s 内到达终态，最后状态: {last}")


def test_e01_e02_full_pipeline_returns_assistant_report(e2e_env) -> None:
    """E-01/E-02：一次消息触发三步协作，终态返回结构化报告消息。"""

    client = TestClient(app)
    session_id, workflow_id = _start_session(client, "分析一篇 Agent 技术文章并生成报告")

    workflow = _wait_for_terminal(client, workflow_id)
    assert workflow["status"] == "completed"
    assert workflow["completed_at"] is not None

    messages = client.get(f"/api/v1/sessions/{session_id}/messages").json()
    assert messages["total"] == 2
    user_message, report = messages["items"]
    assert user_message["role"] == "user"
    assert user_message["status"] == "completed"
    assert report["role"] == "assistant"
    assert report["status"] == "completed"
    assert report["content"] == e2e_env.stage_payload(workflow_id, "report")["content"]
    assert "阶段结论" in report["content"]


def test_pipeline_runs_three_stages_in_order_with_real_tools(
    e2e_env,
) -> None:
    """三步依次完成，且每个阶段都真实调用了内置工具（工具输出回填给模型）。"""

    client = TestClient(app)
    _session_id, workflow_id = _start_session(client, "计算并分析")

    workflow = _wait_for_terminal(client, workflow_id)
    assert workflow["status"] == "completed"

    # 子 Workflow 实例 ID 与阶段顺序由真实 Workflow 代码生成（doc/dapr-integration.md §6）。
    assert e2e_env.driver.child_instances == [
        f"{workflow_id}:{stage}" for stage in STAGES
    ]
    assert e2e_env.driver.activities == ["run_stage_activity"] * 3 + ["finalize_activity"]

    # 阶段载荷按契约携带 tool_calls（doc/data-model.md §3）；模型每阶段请求不同参数。
    for index, stage in enumerate(STAGES):
        payload = e2e_env.stage_payload(workflow_id, stage)
        assert payload["step"] == stage
        assert payload["status"] == "completed"
        assert [call["tool_name"] for call in payload["tool_calls"]] == ["calculator"]
        assert payload["tool_calls"][0]["status"] == "succeeded"
        assert payload["tool_calls"][0]["input"] == {
            "expression": e2e_env.model.expressions[index]
        }

    # 注册表是成员 C 的真实接入点：四个进程级内置工具，加上两个**会话级**附件工具。
    # 后两个只在绑定会话的执行里存在（`app/tools/session_files.py` + ADR-025），
    # 所以不会出现在 `GET /tools` 的静态目录里（doc/api.md §5.3）——这里顺带钉住
    # 「执行时确实绑给了模型」这件事。
    bound = {tool["function"]["name"] for tool in e2e_env.model.bound_tools}
    assert bound == {
        "calculator",
        "code_execution",
        "sql_query",
        "web_search",
        "list_session_files",
        "read_session_file",
    }

    # 工具观察结果确实回填给了模型（每个阶段的第二次调用都带 ToolMessage）。
    assert len(e2e_env.model.calls) == 6
    assert all(
        any(type(message).__name__ == "ToolMessage" for message in call)
        for call in e2e_env.model.calls[1::2]
    )

    # 审计链路（tool_calls 表替身）按 running→succeeded 落库：每个阶段各落一条
    # （调用 ID 派生键含阶段，见 `test_each_stage_call_is_audited_with_its_own_arguments`）。
    rows = list(e2e_env.audit.rows.values())
    assert len(rows) == 3
    assert all(row["status"] == "succeeded" for row in rows)
    assert {row["tool_name"] for row in rows} == {"calculator"}
    assert all(row["workflow_run_id"] == workflow_id for row in rows)


def test_each_stage_call_is_audited_with_its_own_arguments(e2e_env) -> None:
    """三个阶段用**不同**参数请求同一种工具时，各自真正执行并各落一条审计（F-01）。

    缺陷成因（2026-09-20 修复前）：`app/orchestration/tools.py` 的 `ToolCaller` 每阶段
    重建、`index` 都从 0 起算，而调用 ID 只按 `scope + index + tool_name` 派生，
    于是三个阶段的 ID 完全相同，`app/core/tool_audit.execute_tool_call` 按该 ID 幂等，
    后两个阶段直接返回第一阶段 succeeded 的缓存（实测：analyze 请求 `12*(3+5)`、
    report 请求 `12*(3+6)`，两者都拿到 `{"expression": "12*(3+4)", "value": 84}`，
    三次调用只落 1 行审计）。

    修复：阶段名进入调用 ID 派生键（`tool_call_id(..., stage=...)`），与
    `app/core/tool_audit.py` 模块文档「call_id 取自 workflow_id + stage + tool_name」
    的约定一致；同阶段重放仍得到同一个 ID，幂等语义不变。
    """

    client = TestClient(app)
    _session_id, workflow_id = _start_session(client, "跨阶段调用同一种工具")

    assert _wait_for_terminal(client, workflow_id)["status"] == "completed"

    assert [request["expression"] for request in e2e_env.model.requests] == [
        "12*(3+4)",
        "12*(3+5)",
        "12*(3+6)",
    ]
    expected = [
        {"expression": "12*(3+4)", "value": 84},
        {"expression": "12*(3+5)", "value": 96},
        {"expression": "12*(3+6)", "value": 108},
    ]
    outputs = [
        e2e_env.stage_payload(workflow_id, stage)["tool_calls"][0]["output"]
        for stage in STAGES
    ]
    assert outputs == expected
    assert outputs[1] != outputs[0] and outputs[2] != outputs[0]

    rows = list(e2e_env.audit.rows.values())
    assert len(rows) == 3
    assert [row["input"] for row in rows] == [
        {"expression": expression} for expression in ("12*(3+4)", "12*(3+5)", "12*(3+6)")
    ]
    assert [row["output"] for row in rows] == expected
    assert len({row["id"] for row in rows}) == 3


def test_tool_failure_is_recorded_and_pipeline_continues(
    e2e_env,
) -> None:
    """工具失败归一化为 failed 记录，不中断整条流水线（ADR-009）。"""

    e2e_env.model.tool_name = "not_a_registered_tool"
    client = TestClient(app)
    _session_id, workflow_id = _start_session(client, "请求一个不存在的工具")

    workflow = _wait_for_terminal(client, workflow_id)
    assert workflow["status"] == "completed"

    payload = e2e_env.stage_payload(workflow_id, "collect")
    record = payload["tool_calls"][0]
    assert record["tool_name"] == "not_a_registered_tool"
    assert record["status"] == "failed"
    assert "ToolExecutionError" in record["error"]
    assert {row["status"] for row in e2e_env.audit.rows.values()} == {"failed"}


def test_stage_failure_writes_failed_terminal_state(
    e2e_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型不可用时终态回写 failed，且不落半成品报告（ADR-008）。"""

    monkeypatch.setattr(
        pipeline_graph, "build_chat_model", lambda *a, **k: FailingStageModel()
    )
    client = TestClient(app)
    session_id, workflow_id = _start_session(client, "模型不可用")

    workflow = _wait_for_terminal(client, workflow_id)
    assert workflow["status"] == "failed"

    messages = client.get(f"/api/v1/sessions/{session_id}/messages").json()
    assert [item["role"] for item in messages["items"]] == ["user"]
    assert messages["items"][0]["status"] == "failed"

    # 失败也计入任务完成率的分母（doc/15 ... §六「指标收集」）。
    assert _statuses(e2e_env, METRIC_WORKFLOW_RUNS) == {"failed"}


def _statuses(e2e_env, metric_name: str) -> set[str]:
    return {
        label["status"]
        for label in e2e_env.metrics.labels(metric_name)
        if "status" in label
    }


def test_observability_records_stage_tool_and_workflow_metrics(
    e2e_env,
) -> None:
    """可观测：阶段耗时、工具调用与 Workflow 终态都带 workflow_id 标签落采样。"""

    client = TestClient(app)
    _session_id, workflow_id = _start_session(client, "可观测链路")

    assert _wait_for_terminal(client, workflow_id)["status"] == "completed"

    stage_labels = e2e_env.metrics.labels(METRIC_STAGE_RUNS)
    assert [label["stage"] for label in stage_labels] == list(STAGES)
    assert {label["workflow_id"] for label in stage_labels} == {workflow_id}
    assert {label["role"] for label in stage_labels} == {
        "collector",
        "analyst",
        "reporter",
    }

    tool_labels = e2e_env.metrics.labels(METRIC_TOOL_CALLS)
    assert {label["tool_name"] for label in tool_labels} == {"calculator"}
    assert {label["status"] for label in tool_labels} == {"succeeded"}

    assert _statuses(e2e_env, METRIC_WORKFLOW_RUNS) == {"completed"}


def test_second_message_inherits_session_history(e2e_env) -> None:
    """同一会话的第二轮执行带上一轮问答作为上下文（E-01 多轮，F-06 / ADR-019）。

    写点：受理消息时写用户消息、终态回写时写助手报告；
    读点：阶段活动按 `session_id` 读会话记忆并注入提示词，同时剔除本次执行自己的消息。
    """

    from app.memory import MessageRole
    from app.memory.runtime import conversation_memory

    # 假模型按阶段数消耗脚本，这里给两轮各准备三个阶段的表达式。
    e2e_env.model.expressions = [
        "12*(3+4)",
        "12*(3+5)",
        "12*(3+6)",
        "12*(3+7)",
        "12*(3+8)",
        "12*(3+9)",
    ]

    client = TestClient(app)
    session_id = client.post("/api/v1/sessions", json={"user_id": "e2e"}).json()["id"]

    first = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "第一轮：整理 Agent 技术要点"},
    )
    assert first.status_code == 202
    assert _wait_for_terminal(client, first.json()["workflow_id"])["status"] == "completed"

    calls_before = len(e2e_env.model.calls)
    second = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "第二轮：在上一轮基础上补充风险"},
    )
    assert second.status_code == 202
    second_workflow = second.json()["workflow_id"]
    assert _wait_for_terminal(client, second_workflow)["status"] == "completed"

    # 第二轮的 collector 提示词带上了第一轮的用户问题与助手报告正文。
    collector_prompt = e2e_env.model.calls[calls_before][1].content
    assert "第一轮：整理 Agent 技术要点" in collector_prompt
    assert "阶段结论" in collector_prompt
    # 本轮自己的消息不算历史（任务本身已在提示词里），只出现一次。
    assert collector_prompt.count("第二轮：在上一轮基础上补充风险") == 1

    # 会话记忆按轮次累积：user / assistant × 2。
    stored = conversation_memory().list_messages(session_id)
    assert [message.role for message in stored] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]

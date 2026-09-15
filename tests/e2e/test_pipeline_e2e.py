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

    # 注册表是成员 C 的真实接入点，四个内置工具都被发现并绑定给模型。
    bound = {tool["function"]["name"] for tool in e2e_env.model.bound_tools}
    assert bound == {"calculator", "code_execution", "sql_query", "web_search"}

    # 工具观察结果确实回填给了模型（每个阶段的第二次调用都带 ToolMessage）。
    assert len(e2e_env.model.calls) == 6
    assert all(
        any(type(message).__name__ == "ToolMessage" for message in call)
        for call in e2e_env.model.calls[1::2]
    )

    # 审计链路（tool_calls 表替身）按 running→succeeded 落库。三个阶段共用同一条
    # 记录——审计主键按 Workflow 级 scope 派生，跨阶段同工具即同一主键，详见
    # `test_audit_key_collapses_distinct_calls_across_stages`（已知缺陷，成员 A/B 侧）。
    assert len(e2e_env.audit.rows) == 1
    row = next(iter(e2e_env.audit.rows.values()))
    assert row["status"] == "succeeded"
    assert row["tool_name"] == "calculator"
    assert row["workflow_run_id"] == workflow_id


def test_audit_key_collapses_distinct_calls_across_stages(e2e_env) -> None:
    """已知缺陷（待成员 A/B 决策，成员 C 侧 F-01 证据化上报）：

    三个阶段用**不同**参数请求同一种工具时，后两个阶段不会真正执行工具，
    拿到的是第一阶段输出的缓存：

    - `app/orchestration/tools.py` 的 `ToolCaller` 每阶段重建，`index` 都从 0 起算；
    - `app/workflows/pipeline.py` 传入的 `tool_scope` 是 Workflow 级 ID
      （`workflow_run_id or workflow_id or run_id`），阶段名不参与；
    - 因此 `tool_call_id(index, tool_name, scope)` 在三个阶段完全相同，
      `app/core/tool_audit.execute_tool_call` 按该 ID 幂等，直接返回首个 succeeded 的缓存。

    后果：参数被忽略、工具只执行一次，与 `app/core/tool_audit.py` 模块文档
    「call_id 取自 workflow_id + stage + tool_name」的约定不符。真实模型下即
    「分析师/报告员拿到收集者的检索结果」的错误链路。

    A/B 修复后（例如 `tool_scope=f"{workflow_id}:{stage}"`）本用例会失败，
    届时应改为断言 3 条记录且各阶段 output 与自身 input 对应。
    """

    client = TestClient(app)
    _session_id, workflow_id = _start_session(client, "跨阶段调用同一种工具")

    assert _wait_for_terminal(client, workflow_id)["status"] == "completed"

    assert [request["expression"] for request in e2e_env.model.requests] == [
        "12*(3+4)",
        "12*(3+5)",
        "12*(3+6)",
    ]
    first = {"expression": "12*(3+4)", "value": 12 * (3 + 4)}
    outputs = [
        e2e_env.stage_payload(workflow_id, stage)["tool_calls"][0]["output"]
        for stage in STAGES
    ]
    assert outputs == [first, first, first]
    assert len(e2e_env.audit.rows) == 1
    assert next(iter(e2e_env.audit.rows.values()))["input"] == {
        "expression": "12*(3+4)"
    }


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

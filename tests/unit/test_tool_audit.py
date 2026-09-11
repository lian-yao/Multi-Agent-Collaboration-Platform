from __future__ import annotations

from typing import Any

import pytest

from app.core import tool_audit


class FakeToolCallStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.sequence: list[str] = []

    def get(self, call_id: str) -> dict[str, Any] | None:
        self.sequence.append("get")
        row = self.rows.get(str(call_id))
        return dict(row) if row else None

    def create(
        self,
        *,
        call_id: str,
        run_id: str,
        workflow_run_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.sequence.append("create")
        row = {
            "id": str(call_id),
            "run_id": str(run_id),
            "workflow_run_id": (
                str(workflow_run_id) if workflow_run_id else None
            ),
            "tool_name": tool_name,
            "input": tool_input,
            "output": None,
            "status": "running",
            "error": None,
        }
        self.rows[str(call_id)] = row
        return dict(row)

    def mark_running(self, call_id: str) -> dict[str, Any]:
        self.sequence.append("mark_running")
        row = self.rows[str(call_id)]
        row["status"] = "running"
        row["output"] = None
        row["error"] = None
        return dict(row)

    def complete(self, call_id: str, *, output: Any) -> dict[str, Any]:
        self.sequence.append("complete")
        row = self.rows[str(call_id)]
        row["status"] = "succeeded"
        row["output"] = output
        row["error"] = None
        return dict(row)

    def fail(self, call_id: str, *, error: str) -> dict[str, Any]:
        self.sequence.append("fail")
        row = self.rows[str(call_id)]
        row["status"] = "failed"
        row["output"] = None
        row["error"] = error
        return dict(row)


@pytest.fixture
def audit_store(monkeypatch):
    store = FakeToolCallStore()
    monkeypatch.setattr(tool_audit.checkpoint, "get_tool_call", store.get)
    monkeypatch.setattr(tool_audit.checkpoint, "create_tool_call", store.create)
    monkeypatch.setattr(
        tool_audit.checkpoint,
        "mark_tool_call_running",
        store.mark_running,
    )
    monkeypatch.setattr(
        tool_audit.checkpoint,
        "complete_tool_call",
        store.complete,
    )
    monkeypatch.setattr(tool_audit.checkpoint, "fail_tool_call", store.fail)
    return store


def test_execute_tool_call_writes_running_then_succeeded(audit_store):
    result = tool_audit.execute_tool_call(
        run_id="run-1",
        workflow_run_id="wf-1",
        tool_name="calculator",
        tool_input={"expression": "1+1"},
        call_id="call-1",
        tool=lambda payload: {"value": 2},
    )

    assert result == {"value": 2}
    assert audit_store.sequence == ["get", "create", "complete"]
    assert audit_store.rows["call-1"]["status"] == "succeeded"
    assert audit_store.rows["call-1"]["output"] == {"value": 2}


def test_succeeded_call_returns_cached_output_without_execution(audit_store):
    audit_store.rows["call-1"] = {
        "id": "call-1",
        "run_id": "run-1",
        "workflow_run_id": "wf-1",
        "tool_name": "calculator",
        "input": {"expression": "1+1"},
        "output": {"value": 2},
        "status": "succeeded",
        "error": None,
    }
    called = False

    def tool(_payload):
        nonlocal called
        called = True
        return {"value": 999}

    result = tool_audit.execute_tool_call(
        run_id="run-1",
        workflow_run_id="wf-1",
        tool_name="calculator",
        tool_input={"expression": "1+1"},
        call_id="call-1",
        tool=tool,
    )

    assert result == {"value": 2}
    assert called is False
    assert audit_store.sequence == ["get"]


def test_failed_tool_call_is_persisted_and_reraised(audit_store):
    with pytest.raises(RuntimeError, match="boom"):
        tool_audit.execute_tool_call(
            run_id="run-1",
            tool_name="calculator",
            tool_input={"expression": "bad"},
            call_id="call-1",
            tool=lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")),
        )

    assert audit_store.rows["call-1"]["status"] == "failed"
    assert audit_store.rows["call-1"]["error"] == "RuntimeError: boom"


def test_failed_call_can_retry_with_same_id(audit_store):
    audit_store.rows["call-1"] = {
        "id": "call-1",
        "run_id": "run-1",
        "workflow_run_id": None,
        "tool_name": "calculator",
        "input": {"expression": "1+1"},
        "output": None,
        "status": "failed",
        "error": "previous failure",
    }

    result = tool_audit.execute_tool_call(
        run_id="run-1",
        tool_name="calculator",
        tool_input={"expression": "1+1"},
        call_id="call-1",
        tool=lambda _payload: {"value": 2},
    )

    assert result == {"value": 2}
    assert audit_store.sequence == [
        "get",
        "mark_running",
        "complete",
    ]


def test_derived_call_id_is_stable_and_input_sensitive():
    first = tool_audit.derive_tool_call_id("wf-1", "collect", "calculator")
    second = tool_audit.derive_tool_call_id("wf-1", "collect", "calculator")
    third = tool_audit.derive_tool_call_id("wf-1", "analyze", "calculator")

    assert first == second
    assert first != third


@pytest.mark.asyncio
async def test_async_tool_call_is_audited(audit_store):
    async def tool(payload):
        return {"echo": payload["value"]}

    result = await tool_audit.execute_tool_call_async(
        run_id="run-1",
        tool_name="echo",
        tool_input={"value": "hello"},
        call_id="call-async",
        tool=tool,
    )

    assert result == {"echo": "hello"}
    assert audit_store.rows["call-async"]["status"] == "succeeded"

def test_running_call_is_rejected_for_concurrent_replay(audit_store):
    audit_store.rows["call-1"] = {
        "id": "call-1",
        "run_id": "run-1",
        "workflow_run_id": None,
        "tool_name": "calculator",
        "input": {"expression": "1+1"},
        "output": None,
        "status": "running",
        "error": None,
    }

    with pytest.raises(tool_audit.ToolCallInProgressError):
        tool_audit.execute_tool_call(
            run_id="run-1",
            tool_name="calculator",
            tool_input={"expression": "1+1"},
            call_id="call-1",
            tool=lambda _payload: {"value": 2},
        )

    assert audit_store.sequence == ["get"]
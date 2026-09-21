"""执行台卡片弹窗的数据源契约（`doc/api.md` §5.17）。

三个语义必须分得开：Workflow 不存在是 404；阶段确实没有轨迹是 **200 + 逐条 `reason`**；
状态存储读不到是 503。把后两者混起来，用户就分不清「任务还没跑到那一步」和
「Dapr 没起来」，而这两件事的下一步动作完全不同。
"""

import pytest
from fastapi.testclient import TestClient
from grpc import RpcError

import app.api.main as api_main
from app.api.store import InMemoryApiStore


@pytest.fixture
def client(monkeypatch):
    store = InMemoryApiStore()
    store.create_workflow("w1", session_id="s1", agent_run_id="r1")
    monkeypatch.setattr(api_main, "api_store", store)
    return TestClient(api_main.app)


def test_unknown_workflow_is_404(client):
    response = client.get("/api/v1/workflows/missing/stages")
    assert response.status_code == 404
    assert response.json()["code"] == "WORKFLOW_NOT_FOUND"


def test_static_trace_is_served_as_is(client, monkeypatch):
    def fake_read(workflow_id, *, checkpoint=None, read_step=None):
        assert checkpoint is None  # 尚无检查点的执行也要能读
        return {
            "workflow_id": workflow_id,
            "mode": "static",
            "task": "统计退货率",
            "availability": "available",
            "reason": None,
            "items": [
                {
                    "stage": "collect",
                    "role": "collector",
                    "input": None,
                    "input_from": None,
                    "output": "已收集原始数据",
                    "tool_calls": [
                        {
                            "call_id": "c1",
                            "tool_name": "calculator",
                            "status": "succeeded",
                            "input": {"expression": "1+1"},
                            "output": {"result": 2},
                            "error": None,
                        }
                    ],
                    "truncated": False,
                    "reason": None,
                }
            ],
        }

    monkeypatch.setattr(api_main, "read_stage_traces", fake_read)
    body = client.get("/api/v1/workflows/w1/stages").json()

    assert body["mode"] == "static"
    assert body["task"] == "统计退货率"
    assert body["items"][0]["tool_calls"][0]["output"] == {"result": 2}


def test_dapr_transport_error_is_mapped_to_503():
    """钉住错误映射：Dapr 的传输异常必须在 503 分支里。

    `DaprGrpcError` 不在 `OSError` 家族里，只按「网络异常」的直觉写 except，
    「sidecar 没起来」就会漏成 500——用户看到的是「服务器内部错误」，
    排查方向直接跑偏。
    """

    from dapr.clients.exceptions import DaprGrpcError

    assert issubclass(DaprGrpcError, RpcError)
    assert DaprGrpcError in api_main.STATE_STORE_ERRORS
    assert OSError in api_main.STATE_STORE_ERRORS


def test_unreadable_state_store_is_503_not_empty(client, monkeypatch):
    """状态存储连不上时**不能**返回空轨迹：那是两件不同的事。"""

    def broken(*_args, **_kwargs):
        raise ConnectionError("failed to connect to all addresses at 127.0.0.1:50001")

    monkeypatch.setattr(api_main, "read_stage_traces", broken)
    response = client.get("/api/v1/workflows/w1/stages")

    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"
    assert "dapr-sidecar" in response.json()["message"]

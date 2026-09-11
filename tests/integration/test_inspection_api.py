from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, Integer, JSON, MetaData, String, Table, create_engine
from sqlalchemy.pool import StaticPool

import app.api.main as api_main
from app.api.inspection import InspectionStore
from app.api.store import InMemoryApiStore
from app.config import AgentSettings


@pytest.fixture
def inspection(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    reader = InspectionStore(lambda: engine)
    store = InMemoryApiStore()
    store.create_workflow("w1", session_id="s1", agent_run_id="r1")
    store.create_workflow("w2", session_id="s2", agent_run_id="r2")
    monkeypatch.setattr(api_main, "inspection_store", reader)
    monkeypatch.setattr(api_main, "api_store", store)
    yield TestClient(api_main.app), engine, reader
    engine.dispose()


@pytest.mark.parametrize("provider,model", [("ollama", "local-test"), ("openai", "remote-test")])
def test_provider_and_agent_agree(monkeypatch, provider, model):
    settings = AgentSettings(_env_file=None, llm_provider=provider, ollama_model="local-test",
                             openai_model="remote-test", temperature=0.7,
                             ollama_base_url="http://user:secret@localhost:11434/api?key=secret#secret")
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    client = TestClient(api_main.app)
    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    assert "secret" not in response.text
    assert response.json()["items"][0]["model"] == model
    for agent in client.get("/api/v1/agents").json()["items"]:
        assert agent["model"] == model
        assert agent["provider"] == provider
        assert agent["temperature"] == 0.7
        assert client.get('/api/v1/agents/' + agent["id"]).json() == agent
    missing = client.get("/api/v1/agents/missing", headers={"X-Request-ID": "contract-test"})
    assert missing.status_code == 404
    assert missing.json()["request_id"] == "contract-test"


def test_missing_model(monkeypatch):
    monkeypatch.setattr(api_main, "get_settings", lambda: AgentSettings(_env_file=None, llm_provider="openai", openai_model=""))
    assert TestClient(api_main.app).get("/api/v1/providers").json()["items"][0]["status"] == "missing_model"


@pytest.mark.parametrize("path", ["/api/v1/tools", "/api/v1/metrics", "/api/v1/workflows/w1/tool-calls"])
def test_missing_source_and_pagination(inspection, path):
    client, _, _ = inspection
    assert client.get(path).json() == dict(items=[], page=1, page_size=20, total=0, availability="not_integrated")
    assert client.get(path + "?page=0").status_code == 422
    assert client.get(path + "?page_size=101").status_code == 422


def test_unknown_workflow(inspection):
    client, _, _ = inspection
    assert client.get("/api/v1/workflows/missing/tool-calls").status_code == 404
    assert client.get("/api/v1/metrics?workflow_id=missing").status_code == 404


def test_catalog_adapter(inspection):
    client, _, reader = inspection
    reader.tool_catalog = lambda: [dict(name=name, description="test", input_schema={}, status="available") for name in ["b", "a"]]
    result = client.get("/api/v1/tools?page_size=1&page=2").json()
    assert result["total"] == 2
    assert result["availability"] == "available"
    assert result["items"][0]["name"] == "b"


def test_sql_records_filter_order_and_zero(inspection):
    client, engine, _ = inspection
    metadata = MetaData()
    calls = Table("tool_calls", metadata, Column("id", String, primary_key=True),
                  Column("run_id", String), Column("workflow_run_id", String),
                  Column("tool_name", String), Column("input", JSON), Column("output", JSON),
                  Column("status", String), Column("error", String),
                  Column("created_at", DateTime), Column("updated_at", DateTime))
    metrics = Table("metrics", metadata, Column("id", Integer, primary_key=True),
                    Column("metric_name", String), Column("value", Integer),
                    Column("labels", JSON), Column("recorded_at", DateTime))
    metadata.create_all(engine)
    assert client.get("/api/v1/metrics").json()["availability"] == "available"
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for ident, run, workflow in [("1", "r1", "w1"), ("2", "r1", None), ("3", "r2", "w2"), ("4", "r1", "w2")]:
            conn.execute(calls.insert().values(id=ident, run_id=run, workflow_run_id=workflow,
                         tool_name="calculator", input={"expression": "1+1"}, output={"result": 2},
                         status="succeeded", error=None, created_at=now, updated_at=now))
        for ident, workflow, value in [(1, "w1", 10), (2, "w1", 0), (3, "w2", 99)]:
            conn.execute(metrics.insert().values(id=ident, metric_name="total_tokens", value=value,
                         labels={"workflow_id": workflow}, recorded_at=now))
    result = client.get("/api/v1/workflows/w1/tool-calls?page_size=1&page=2").json()
    assert result["total"] == 2
    assert result["items"][0]["id"] == "2"
    result = client.get("/api/v1/metrics?workflow_id=w1&page_size=1").json()
    assert result["total"] == 2
    assert result["items"][0]["value"] == 0
    assert client.get("/api/v1/metrics?workflow_id=w1&page=3&page_size=1").json()["items"] == []


def test_source_failure_is_not_empty(inspection):
    client, _, reader = inspection
    def broken():
        raise OSError("secret connection string")
    reader.engine_factory = broken
    response = client.get("/api/v1/metrics")
    assert response.status_code == 503
    assert response.json()["code"] == "DATA_SOURCE_UNAVAILABLE"
    assert "secret" not in response.text

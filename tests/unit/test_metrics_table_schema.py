"""`metrics` 表 DDL 契约（doc/data-model.md §3，成员 B）。

观测采样按列名反射写入、只读接口按列名读取，所以表结构必须与文档一致；
这里用 PostgreSQL 方言编译模型，不依赖真实数据库。
"""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.checkpoint import Base


def _ddl(table_name: str) -> str:
    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))


def test_metrics_table_matches_data_model():
    table = Base.metadata.tables["metrics"]
    assert list(table.columns.keys()) == [
        "id",
        "metric_name",
        "value",
        "labels",
        "recorded_at",
    ]

    ddl = _ddl("metrics")
    assert "id BIGSERIAL NOT NULL" in ddl
    assert "metric_name VARCHAR(100) NOT NULL" in ddl
    assert "value FLOAT NOT NULL" in ddl
    assert "labels JSONB NOT NULL" in ddl
    assert "recorded_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert table.primary_key.columns.keys() == ["id"]


def test_metrics_index_is_name_time_desc():
    table = Base.metadata.tables["metrics"]
    indexes = {index.name: index for index in table.indexes}
    assert set(indexes) == {"idx_metrics_name_time"}

    index = indexes["idx_metrics_name_time"]
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "CREATE INDEX idx_metrics_name_time ON metrics (metric_name, recorded_at DESC)" in ddl


def test_metrics_table_is_created_by_schema_init_metadata():
    """`init_checkpoint_schema()` 用 `Base.metadata.create_all` 建表。"""

    assert "metrics" in Base.metadata.tables
    assert {"sessions", "agent_runs", "messages", "workflow_runs", "tool_calls", "metrics"} <= set(
        Base.metadata.tables
    )

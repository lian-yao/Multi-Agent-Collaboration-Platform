"""补列迁移：`create_all` 不会给**已存在**的表补列。

升级一个跑过旧版本的环境时，`agent_configs` / `provider_configs` 还是老列定义，
新增的 `llm_model_id` / `top_p` / `max_output_tokens` / `reasoning_type` /
`default_llm_model_id` 必须显式补上，否则注册表配置一写就报 `UndefinedColumn`。
ADR-033 阶段 2 同理给 `workspaces` 补了 `updated_by`（提档要记"谁提的"）。

这里只验证语句生成与执行接线（不连真实数据库）：语句必须是幂等的
`ADD COLUMN IF NOT EXISTS`，且只在 PostgreSQL 上生成。
"""

from __future__ import annotations

from app.core import checkpoint


class RecordingConnection:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def exec_driver_sql(self, statement: str) -> None:
        self.log.append(statement)


class RecordingEngine:
    """最小引擎替身：只需要 `dialect.name` 与 `begin()` 上下文。"""

    def __init__(self, dialect_name: str, log: list[str]) -> None:
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self._log = log

    def begin(self):
        engine = self

        class _Context:
            def __enter__(self):
                return RecordingConnection(engine._log)

            def __exit__(self, *exc_info):
                return False

        return _Context()


def test_migration_covers_every_column_added_by_adr_017():
    statements = checkpoint._registry_migration_statements("postgresql")
    joined = "\n".join(statements)

    for column in (
        "llm_model_id",
        "top_p",
        "max_output_tokens",
        "reasoning_type",
        "default_llm_model_id",
    ):
        assert column in joined

    assert "ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS llm_model_id VARCHAR(80)" in statements
    assert (
        "ALTER TABLE provider_configs ADD COLUMN IF NOT EXISTS "
        "default_llm_model_id VARCHAR(80)" in statements
    )


def test_migration_covers_the_workspace_actor_column():
    """阶段 2 给 `workspaces` 补 `updated_by`：阶段 1 建过表的环境必须能升上来。"""

    statements = checkpoint._registry_migration_statements("postgresql")

    assert (
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS updated_by VARCHAR(100)"
        in statements
    )


def test_migration_statements_are_idempotent():
    """每条都必须**幂等**：启动时每次都跑，重复执行不能报错。

    迁移现在有两类，各有各的幂等写法——补列用 `ADD COLUMN IF NOT EXISTS`，
    重建索引用 `CREATE ... IF NOT EXISTS`（外加 `DROP CONSTRAINT IF EXISTS` 先清旧的）。
    所以这里按类别断言，而不是要求所有语句都长成补列的样子。
    """

    statements = checkpoint._registry_migration_statements("postgresql")

    assert statements
    for statement in statements:
        assert (
            "ADD COLUMN IF NOT EXISTS" in statement
            or "CREATE UNIQUE INDEX IF NOT EXISTS" in statement
            or "DROP CONSTRAINT IF EXISTS" in statement
        ), f"这条迁移不幂等：{statement}"


def test_migration_switches_workspace_uniqueness_to_per_session():
    """2026-09-23：工作区去重从「全局唯一」收窄到「会话内唯一」——已有库要能升上来。"""

    statements = checkpoint._registry_migration_statements("postgresql")

    assert "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS ux_workspaces_path" in statements
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_session_path "
        "ON workspaces (session_id, path)" in statements
    )


def test_migration_is_postgresql_only():
    assert checkpoint._registry_migration_statements("sqlite") == []
    assert checkpoint._registry_migration_statements("mysql") == []


def test_apply_migration_executes_statements_in_order():
    log: list[str] = []

    applied = checkpoint._apply_registry_migrations(RecordingEngine("postgresql", log))

    assert applied == log
    assert len(log) == len(checkpoint._registry_migration_statements("postgresql"))


def test_apply_migration_is_a_noop_off_postgresql():
    log: list[str] = []

    applied = checkpoint._apply_registry_migrations(RecordingEngine("sqlite", log))

    assert applied == []
    assert log == []

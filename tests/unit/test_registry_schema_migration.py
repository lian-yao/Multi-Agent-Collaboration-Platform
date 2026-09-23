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


_IDEMPOTENT_MARKERS = (
    "ADD COLUMN IF NOT EXISTS",
    "CREATE UNIQUE INDEX IF NOT EXISTS",
    "DROP CONSTRAINT IF EXISTS",
    "DROP INDEX IF EXISTS",
)

_DEDUPE_PREFIX = "DELETE FROM workspaces older USING workspaces newer"


def test_migration_statements_are_idempotent():
    """每条都必须**幂等**：启动时每次都跑，重复执行不能报错。

    迁移现在有三类写法——补列 / 建索引 / 清旧结构都用 `IF [NOT] EXISTS`，另有一条**收敛型
    数据迁移**（把"同一会话里更早的登记"删掉）没有 `IF NOT EXISTS` 可写，但它第二次执行时
    已经无可删的行，效果同样收敛。这里按类别点名，而不是放宽成"什么都行"。
    """

    statements = checkpoint._registry_migration_statements("postgresql")

    assert statements
    for statement in statements:
        if any(marker in statement for marker in _IDEMPOTENT_MARKERS):
            continue
        assert statement.startswith(_DEDUPE_PREFIX), f"这条迁移既不幂等、也不是已点名的收敛型：{statement}"


def test_migration_switches_workspace_uniqueness_to_one_per_session():
    """2026-09-23 两次修正的迁移都要在，且顺序正确——已有库必须能自己升上来。

    历史：先是「全局唯一」→「会话内路径唯一」→「**一个会话一条**」。老库上可能残留前两者的
    结构（一个是约束、一个是索引），都要清掉；而建「会话唯一」索引之前**必须**先按会话收敛
    历史数据（保留最新那条），否则同一会话有多条绑定的库会直接建索引失败。
    """

    statements = checkpoint._registry_migration_statements("postgresql")
    per_session = "CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_session ON workspaces (session_id)"

    assert "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS ux_workspaces_path" in statements
    assert "DROP INDEX IF EXISTS ux_workspaces_session_path" in statements
    assert per_session in statements
    dedupe = [item for item in statements if item.startswith(_DEDUPE_PREFIX)]
    assert dedupe, "建「一个会话一条」的唯一索引之前必须先收敛历史数据"
    assert statements.index(dedupe[0]) < statements.index(per_session), "收敛必须排在建索引之前"


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

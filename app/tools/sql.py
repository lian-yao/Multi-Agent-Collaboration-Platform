"""只读 SQL 查询工具（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3 示例工具集
「数据库查询（只读SQL查询）」；`doc/testing.md` U-06 要求「SQL 只读校验」返回值正确。

只读由两层保证：

1. **语句层**：只放行单条 `SELECT` / `WITH` 语句，拒绝写操作关键字与多语句拼接；
2. **连接层**：事务显式置为只读（PostgreSQL `SET TRANSACTION READ ONLY`、
   SQLite `PRAGMA query_only = ON`），未知方言直接失败（fail closed）。
   语句层的字符串检查只用于快速给出可读错误，权威约束在连接层。

返回行数同样有两处约束：入参 `limit` 留空时取 `TOOL_SQL_DEFAULT_LIMIT`，
两者都被 `MAX_ROWS` 夹住，并且查询按 `limit + 1` 取行以判断是否被截断
（`truncated` 字段），而不是静默少返回。
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from app.core.storage import get_storage_settings
from app.tools.base import BuiltinTool, ToolExecutionError
from app.tools.config import ToolSettings, get_tool_settings

READ_ONLY_PREFIXES = ("select", "with")
READ_ONLY_DIALECTS = frozenset({"postgresql", "sqlite"})

MAX_ROWS = 500
"""单次查询返回行数的硬上限；`TOOL_SQL_DEFAULT_LIMIT` 也受它约束。"""

_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "merge",
    "upsert",
    "replace",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "comment",
    "copy",
    "call",
    "do",
    "set",
    "reset",
    "vacuum",
    "analyze",
    "reindex",
    "cluster",
    "attach",
    "detach",
    "pragma",
    "into",
    "lock",
)
_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SqlQueryArgs(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=2000,
        description="单条只读 SELECT/WITH 查询语句",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_ROWS,
        description="返回行数上限；留空使用平台配置的默认值",
    )


class SqlQueryTool(BuiltinTool):
    name = "sql_query"
    description = (
        "执行单条只读 SQL（SELECT/WITH）并把结果作为行列表返回；"
        "写操作、多语句与未知数据库类型都会被拒绝。"
    )
    args_model = SqlQueryArgs

    def __init__(
        self,
        settings: ToolSettings | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        self._settings = settings or get_tool_settings()
        self._engine = engine

    def run(self, args: SqlQueryArgs) -> dict[str, Any]:
        statement = assert_read_only_statement(args.query)
        engine = self._engine or _engine_for(self._resolve_dsn())
        limit = _resolve_limit(args.limit, self._settings.sql_default_limit)
        try:
            rows, columns, truncated = _execute_read_only(
                engine, statement, limit, self._settings.sql_timeout_seconds
            )
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"SQL 执行失败: {type(exc).__name__}: {exc}") from exc
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    def _resolve_dsn(self) -> str:
        if self._settings.sql_dsn:
            return self._settings.sql_dsn
        return get_storage_settings().database_url


def _resolve_limit(requested: int | None, default: int) -> int:
    """模型没给 `limit` 时取 `TOOL_SQL_DEFAULT_LIMIT`，并统一夹到 `MAX_ROWS`。

    配置值可能是环境变量给的越界值，所以这里再夹一次：`MAX_ROWS` 是硬上限，
    不是「默认值」。
    """

    if requested is not None:
        return requested
    return max(1, min(int(default), MAX_ROWS))


def assert_read_only_statement(sql: str) -> str:
    """校验语句只读并返回去除注释、字符串字面量与结尾分号的规范化语句。

    这是**快速失败的可读检查**，不是权威边界：真正阻止写操作的是
    `_enable_read_only` 打开的事务级只读模式。
    """

    stripped = _strip_comments(sql).strip()
    if not stripped:
        raise ToolExecutionError("SQL 语句为空")
    body = stripped.rstrip(";").strip()
    if not body:
        raise ToolExecutionError("SQL 语句为空")
    if ";" in body:
        raise ToolExecutionError("只允许单条语句，禁止分号拼接多语句")
    if not body.lower().startswith(READ_ONLY_PREFIXES):
        raise ToolExecutionError("只允许 SELECT / WITH 查询")
    matched = _KEYWORD_PATTERN.search(_strip_literals(body))
    if matched:
        raise ToolExecutionError(
            f"只读查询不允许出现关键字: {matched.group(1).upper()}"
        )
    return body


def _strip_comments(sql: str) -> str:
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def _strip_literals(sql: str) -> str:
    """把单引号字符串替换成占位符，避免字面量里的词被当成 SQL 关键字。"""

    return _STRING_LITERAL.sub("''", sql)


def _execute_read_only(
    engine: Engine,
    statement: str,
    limit: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    with engine.connect() as connection:
        _enable_read_only(connection, timeout_seconds)
        result = connection.execute(text(statement))
        columns = list(result.keys())
        fetched = result.fetchmany(limit + 1)
    truncated = len(fetched) > limit
    rows = [_serialize_row(row, columns) for row in fetched[:limit]]
    return rows, columns, truncated


def _enable_read_only(connection: Connection, timeout_seconds: int) -> None:
    dialect = connection.dialect.name
    if dialect not in READ_ONLY_DIALECTS:
        raise ToolExecutionError(f"只读模式不支持该数据库类型: {dialect}")
    if dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(
            f"SET LOCAL statement_timeout = {int(timeout_seconds) * 1000}"
        )
    else:
        connection.exec_driver_sql("PRAGMA query_only = ON")


def _serialize_row(row: Any, columns: list[str]) -> dict[str, Any]:
    return {
        column: _serialize_value(value)
        for column, value in zip(columns, tuple(row))
    }


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    return str(value)


@lru_cache
def _engine_for(dsn: str) -> Engine:
    """按 DSN 缓存引擎，避免每次调用重新建连接池。"""

    return create_engine(dsn, pool_pre_ping=True)

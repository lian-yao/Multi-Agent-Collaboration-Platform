"""消息列表回读附件的列契约（ADR-024 的代价 · 成员 B）。

附件表里 `data` 是所有类型都留档的原件（最坏 5 MB/份、4 份/消息）。消息列表是
**整页回读**，如果按 ORM 实体整行取，一次翻页就要把几十 MB 字节从库搬到进程里，
再在 `_attachment_meta` 里丢掉——纯粹的白搬。

这里不依赖真实数据库：把查询语句按 PostgreSQL 方言编译出来，断言 SELECT 列表里
**没有 `attachments.data`**，但 `has_original` 仍然在（它由 `data IS NOT NULL`
在库侧算，不需要把 `data` 取回进程）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app.core import checkpoint


def _compiled(message_ids: list[uuid.UUID]) -> str:
    statement = checkpoint._attachment_meta_statement(message_ids)
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_readback_selects_metadata_columns_only():
    """SELECT 列表里不能有裸的 `data` / `text_content`。

    注意判据是「作为**取值列**出现」而不是「字符串里出现 `attachments.data`」：
    `has_original` 就是 `attachments.data IS NOT NULL`，那是条件而不是搬运字节。
    所以这里查 `selected_columns`，不看 SQL 文本。
    """

    statement = checkpoint._attachment_meta_statement([uuid.uuid4()])
    keys = list(statement.selected_columns.keys())

    assert keys == [
        "id",
        "session_id",
        "message_id",
        "name",
        "mime",
        "size_bytes",
        "kind",
        "status",
        "error",
        "created_at",
        "has_original",
    ]
    assert "data" not in keys
    assert "text_content" not in keys


def test_readback_computes_has_original_in_the_database():
    """`has_original` 必须由库侧表达式给出，否则界面会丢掉下载入口。"""

    sql = _compiled([uuid.uuid4()])

    assert "attachments.data IS NOT NULL AS has_original" in sql


def test_readback_still_filters_by_message_and_orders_by_time():
    sql = _compiled([uuid.uuid4(), uuid.uuid4()])

    assert "attachments.message_id IN" in sql
    assert "ORDER BY attachments.created_at" in sql


def test_meta_falls_back_to_the_loaded_row_for_single_row_reads():
    """单行读取（`create_attachment` / `get_attachment_content`）仍按 ORM 对象推导。"""

    class _Row:
        id = uuid.uuid4()
        session_id = None
        message_id = None
        name = "a.txt"
        mime = "text/plain"
        size_bytes = 3
        kind = "text"
        status = "ready"
        error = None
        created_at = None
        data = b"abc"

    meta = checkpoint._attachment_meta(_Row())
    assert meta["has_original"] is True
    assert meta["name"] == "a.txt"
    assert meta["message_id"] is None

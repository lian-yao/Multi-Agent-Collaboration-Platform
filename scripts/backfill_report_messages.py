"""补写历史 Workflow 的报告消息（ADR-008）。

D3-D6 期间终态活动只回写状态，报告正文留在 Dapr State Store，``messages`` 表里
只有用户消息。本脚本把仍可读取的报告补写成 ``messages(role=assistant)``：使用与
在线路径相同的派生 ID 和幂等写入，可重复执行。

默认连接 compose 栈暴露在宿主机的 Redis(6380) 与 PostgreSQL(5433)，即
``app.core.storage.StorageSettings`` 的默认值：

    uv run python scripts/backfill_report_messages.py            # 预演，只打印计划
    uv run python scripts/backfill_report_messages.py --apply    # 实际写入
"""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

from sqlalchemy import select

from app.core.checkpoint import Message, WorkflowRun, upsert_message
from app.core.storage import get_session_factory, get_storage_settings
from app.orchestration.pipeline import deserialize_pipeline_state
from app.workflows.pipeline import report_content, report_message_id
from app.workflows.state import workflow_state_key

REPORT_STEP = "report"
# Dapr 的 Redis state store 把载荷存成 hash：``data`` 放序列化内容，``version`` 放版本。
DAPR_STATE_DATA_FIELD = "data"


def report_from_payload(payload: dict[str, Any]) -> str | None:
    """从 State Store 记录中取出报告正文；结构不符或没有正文时返回 ``None``。"""

    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    try:
        state = deserialize_pipeline_state(result)
    except (ValueError, TypeError):
        return None
    return report_content(state)


def load_report(workflow_id: str) -> str | None:
    """从 State Store 读取该 Workflow 的 report 阶段快照。

    宿主机没有 Dapr sidecar，因此直连 Redis：hash 形态按 Dapr 布局取 ``data``
    字段；若组件写入的是字符串型 key，则退回 ``GET``。
    """

    import redis

    client = redis.Redis.from_url(get_storage_settings().redis_url)
    key = workflow_state_key(workflow_id, REPORT_STEP)
    key_type = client.type(key)
    if isinstance(key_type, bytes):
        key_type = key_type.decode()
    if key_type == "hash":
        raw = client.hget(key, DAPR_STATE_DATA_FIELD)
    elif key_type == "string":
        raw = client.get(key)
    else:
        raw = None
    if not raw:
        return None
    return report_from_payload(json.loads(raw))


def completed_workflows() -> list[dict[str, Any]]:
    """按创建时间返回全部 completed 的 Workflow 业务行。"""

    with get_session_factory()() as session:
        rows = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status == "completed")
            .order_by(WorkflowRun.created_at)
        ).all()
        return [
            {
                "id": str(row.id),
                "session_id": str(row.session_id) if row.session_id else None,
                "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
            }
            for row in rows
        ]


def message_exists(message_id: str) -> bool:
    with get_session_factory()() as session:
        return session.get(Message, uuid.UUID(message_id)) is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="补写历史报告消息")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入数据库；缺省只预演并打印计划。",
    )
    args = parser.parse_args()

    planned = existing = missing = 0
    for row in completed_workflows():
        workflow_id = row["id"]
        session_id = row["session_id"]
        message_id = report_message_id(workflow_id)

        if message_exists(message_id):
            existing += 1
            print(f"[skip] {workflow_id} 已有报告消息")
            continue

        report = load_report(workflow_id) if session_id else None
        if not report:
            missing += 1
            print(f"[miss] {workflow_id} 无会话或 State Store 已无报告快照，跳过")
            continue

        if args.apply:
            upsert_message(
                session_id,
                message_id=message_id,
                content=report,
                role="assistant",
                status="completed",
                agent_run_id=row["agent_run_id"],
            )
            print(f"[write] {workflow_id} -> 会话 {session_id}，{len(report)} 字")
        else:
            print(f"[plan ] {workflow_id} -> 会话 {session_id}，{len(report)} 字")
        planned += 1

    print(
        f"\n合计：待补写 {planned}，已有报告消息 {existing}，无法补写 {missing}"
    )
    if not args.apply and planned:
        print("确认无误后加 --apply 实际写入。")


if __name__ == "__main__":
    main()

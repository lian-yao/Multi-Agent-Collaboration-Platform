"""Read-only D7-D8 adapters; table ownership stays with B/C (api.md §5)."""
from typing import Any, Callable

from sqlalchemy import MetaData, Table, and_, func, inspect, or_, select
from sqlalchemy.engine import Engine

from app.core.storage import get_engine


class InspectionStore:
    def __init__(
        self,
        engine_factory: Callable[[], Engine] = get_engine,
        tool_catalog: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.engine_factory = engine_factory
        self.tool_catalog = tool_catalog

    @staticmethod
    def unavailable(page: int, page_size: int) -> dict[str, Any]:
        return dict(items=[], total=0, page=page, page_size=page_size,
                    availability="not_integrated")

    def tools(self, page: int, page_size: int) -> dict[str, Any]:
        if self.tool_catalog is None:
            return self.unavailable(page, page_size)
        rows = sorted(self.tool_catalog(), key=lambda row: row["name"])
        start = (page - 1) * page_size
        return dict(items=rows[start:start + page_size], total=len(rows),
                    page=page, page_size=page_size, availability="available")

    def tool_calls(self, workflow: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        return self._read("tool_calls", page, page_size, workflow=workflow)

    def metrics(self, page: int, page_size: int, workflow_id: str | None = None) -> dict[str, Any]:
        return self._read("metrics", page, page_size, workflow_id=workflow_id)

    def _read(
        self, name: str, page: int, page_size: int, *,
        workflow: dict[str, Any] | None = None, workflow_id: str | None = None,
    ) -> dict[str, Any]:
        # Reflect existing tables only. Never create schema or mask connection failures.
        with self.engine_factory().connect() as connection:
            if not inspect(connection).has_table(name):
                return self.unavailable(page, page_size)
            table = Table(name, MetaData(), autoload_with=connection)
            query = select(table)
            if name == "tool_calls":
                assert workflow is not None
                condition = table.c.workflow_run_id == workflow["id"]
                if workflow.get("agent_run_id"):
                    condition = or_(condition, and_(
                        table.c.workflow_run_id.is_(None),
                        table.c.run_id == workflow["agent_run_id"],
                    ))
                query = query.where(condition)
                order = (table.c.created_at.asc(), table.c.id.asc())
            else:
                if workflow_id is not None:
                    query = query.where(table.c.labels["workflow_id"].as_string() == workflow_id)
                order = (table.c.recorded_at.desc(), table.c.id.desc())
            total = connection.scalar(select(func.count()).select_from(query.subquery()))
            rows = connection.execute(
                query.order_by(*order).offset((page - 1) * page_size).limit(page_size)
            ).mappings().all()
            items = [dict(row) for row in rows]
            if name == "tool_calls":
                for row in items:
                    for key in ("id", "run_id", "workflow_run_id"):
                        if row[key] is not None:
                            row[key] = str(row[key])
            return dict(items=items, total=int(total or 0), page=page,
                        page_size=page_size, availability="available")

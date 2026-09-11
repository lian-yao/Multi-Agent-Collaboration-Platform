"""Tool-call audit hooks backed by the ``tool_calls`` table.

The helper follows the audit order in ``doc/data-model.md``:

1. insert ``running`` before invoking the tool;
2. update ``succeeded`` or ``failed`` after invocation;
3. reuse a deterministic ``call_id`` on replay and return the cached result.

Dapr activities can be replayed, so callers should derive ``call_id`` from the
business execution key (for example ``workflow_id + stage + tool_name``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core import checkpoint


class ToolCallInProgressError(RuntimeError):
    """Raised when another worker is already executing the same call ID."""


def derive_tool_call_id(*parts: Any) -> str:
    """Return a stable UUID for an idempotent tool-call audit key."""

    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, digest))


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _begin_call(
    *,
    run_id: str | uuid.UUID,
    workflow_run_id: str | uuid.UUID | None,
    tool_name: str,
    tool_input: dict[str, Any],
    call_id: str | uuid.UUID | None,
) -> tuple[str, Any, bool]:
    """Return ``(call_id, cached_output, cache_hit)`` for one audited call."""

    resolved_call_id = (
        str(call_id)
        if call_id is not None
        else derive_tool_call_id(run_id, workflow_run_id, tool_name, tool_input)
    )
    existing = checkpoint.get_tool_call(resolved_call_id)
    if existing is None:
        checkpoint.create_tool_call(
            call_id=resolved_call_id,
            run_id=run_id,
            workflow_run_id=workflow_run_id,
            tool_name=tool_name,
            tool_input=_jsonable(tool_input),
        )
        return resolved_call_id, None, False
    if existing["status"] == "succeeded":
        return resolved_call_id, existing["output"], True
    if existing["status"] == "running":
        raise ToolCallInProgressError(
            f"tool call {resolved_call_id} is already running"
        )
    checkpoint.mark_tool_call_running(resolved_call_id)
    return resolved_call_id, None, False


def execute_tool_call(
    *,
    run_id: str | uuid.UUID,
    tool_name: str,
    tool_input: dict[str, Any],
    tool: Callable[[dict[str, Any]], Any],
    call_id: str | uuid.UUID | None = None,
    workflow_run_id: str | uuid.UUID | None = None,
) -> Any:
    """Execute a sync tool call with idempotent audit persistence."""

    resolved_call_id, cached_output, cache_hit = _begin_call(
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        tool_name=tool_name,
        tool_input=tool_input,
        call_id=call_id,
    )
    if cache_hit:
        return cached_output

    try:
        output = tool(tool_input)
    except Exception as exc:
        checkpoint.fail_tool_call(
            resolved_call_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    checkpoint.complete_tool_call(resolved_call_id, output=_jsonable(output))
    return output


async def execute_tool_call_async(
    *,
    run_id: str | uuid.UUID,
    tool_name: str,
    tool_input: dict[str, Any],
    tool: Callable[[dict[str, Any]], Awaitable[Any]],
    call_id: str | uuid.UUID | None = None,
    workflow_run_id: str | uuid.UUID | None = None,
) -> Any:
    """Async counterpart of :func:`execute_tool_call`."""

    resolved_call_id, cached_output, cache_hit = _begin_call(
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        tool_name=tool_name,
        tool_input=tool_input,
        call_id=call_id,
    )
    if cache_hit:
        return cached_output

    try:
        output = await tool(tool_input)
    except Exception as exc:
        checkpoint.fail_tool_call(
            resolved_call_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    checkpoint.complete_tool_call(resolved_call_id, output=_jsonable(output))
    return output
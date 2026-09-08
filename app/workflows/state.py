import json
from typing import Any

from app.core.dapr import STATE_STORE_KEY_PREFIX, STATE_STORE_NAME


def workflow_state_key(workflow_id: str, step: str) -> str:
    return f"{STATE_STORE_KEY_PREFIX}:workflow:{workflow_id}:{step}"


def save_step_result(
    workflow_id: str, step: str, result: dict[str, Any]
) -> None:
    from dapr.clients import DaprClient

    payload = json.dumps(
        {"workflow_id": workflow_id, "step": step, "result": result},
        default=str,
        separators=(",", ":"),
    )
    with DaprClient() as client:
        client.save_state(
            store_name=STATE_STORE_NAME,
            key=workflow_state_key(workflow_id, step),
            value=payload,
        )


def read_step_result(
    workflow_id: str, step: str
) -> dict[str, Any] | None:
    from dapr.clients import DaprClient

    with DaprClient() as client:
        response = client.get_state(
            store_name=STATE_STORE_NAME,
            key=workflow_state_key(workflow_id, step),
        )
    if not response.data:
        return None
    return json.loads(response.data.decode("utf-8"))
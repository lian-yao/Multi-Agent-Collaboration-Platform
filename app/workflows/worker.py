import logging
import os
import threading
import time

import uvicorn

from app.core.checkpoint import init_checkpoint_schema
from app.observability.logging import configure_logging
from app.workflows.service import get_workflow_service

logger = logging.getLogger("app.workflows.worker")


def start_runtime_in_background() -> threading.Thread:
    def _start_with_retry() -> None:
        while True:
            try:
                get_workflow_service().start()
                logger.info("Dapr Workflow runtime started")
                return
            except Exception as exc:  # sidecar may not be ready yet
                logger.warning("Dapr Workflow runtime unavailable, retrying: %s", exc)
                time.sleep(2)

    thread = threading.Thread(target=_start_with_retry, daemon=True)
    thread.start()
    return thread


def main() -> None:
    configure_logging()
    init_checkpoint_schema()
    start_runtime_in_background()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.api.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

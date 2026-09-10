import threading

import dapr.ext.workflow as wf

from app.core.checkpoint import update_workflow_run
from app.workflows.pipeline import (
    SUBTASK_WORKFLOW_NAME,
    WORKFLOW_NAME,
    WorkflowTask,
    agent_pipeline_workflow,
    agent_subtask_workflow,
    analyze_activity,
    collect_activity,
    finalize_activity,
    report_activity,
    run_stage_activity,
)


class WorkflowService:
    """Registers and schedules the Dapr workflows used by the backend app."""

    def __init__(
        self,
        runtime: wf.WorkflowRuntime | None = None,
        client: wf.DaprWorkflowClient | None = None,
    ) -> None:
        self._runtime = runtime or wf.WorkflowRuntime()
        self._client = client or wf.DaprWorkflowClient()
        self._registered = False
        self._started = False

    def register(self) -> None:
        if self._registered:
            return
        self._runtime.register_workflow(agent_pipeline_workflow, name=WORKFLOW_NAME)
        self._runtime.register_workflow(
            agent_subtask_workflow,
            name=SUBTASK_WORKFLOW_NAME,
        )
        self._runtime.register_activity(run_stage_activity)
        self._runtime.register_activity(collect_activity)
        self._runtime.register_activity(analyze_activity)
        self._runtime.register_activity(report_activity)
        self._runtime.register_activity(finalize_activity)
        self._registered = True

    def start(self) -> None:
        self.register()
        if self._started:
            return
        self._runtime.start()
        self._started = True

    def shutdown(self) -> None:
        if self._started:
            self._runtime.shutdown()
            self._started = False
        self._client.close()

    def schedule(self, task: WorkflowTask) -> str:
        instance_id = str(task.workflow_id)
        self._client.schedule_new_workflow(
            WORKFLOW_NAME,
            input=task.asdict(),
            instance_id=instance_id,
        )
        update_workflow_run(
            instance_id,
            status="running",
            instance_id=instance_id,
        )
        return instance_id

    def pause(self, workflow_id: str) -> None:
        self._client.pause_workflow(workflow_id)

    def resume(self, workflow_id: str) -> None:
        self._client.resume_workflow(workflow_id)

    def get_state(self, workflow_id: str) -> wf.WorkflowState | None:
        return self._client.get_workflow_state(workflow_id)

    def wait_for_completion(self, workflow_id: str, timeout: int = 60) -> wf.WorkflowState | None:
        return self._client.wait_for_workflow_completion(
            workflow_id, timeout_in_seconds=timeout
        )


_service: WorkflowService | None = None
_service_lock = threading.Lock()


def get_workflow_service() -> WorkflowService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = WorkflowService()
    return _service
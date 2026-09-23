import threading

import dapr.ext.workflow as wf

from app.core.checkpoint import update_workflow_run
from app.orchestration.dynamic_graph import ORCHESTRATION_MODES, resolve_orchestration_mode
from app.workflows.dynamic import (
    DYNAMIC_SUBTASK_WORKFLOW_NAME,
    DYNAMIC_WORKFLOW_NAME,
    agent_dynamic_workflow,
    dynamic_checkpoint_activity,
    dynamic_plan_activity,
    dynamic_step_activity,
    dynamic_subtask_workflow,
    dynamic_synthesize_activity,
    dynamic_validate_activity,
    intake_activity,
)
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
    rewrite_activity,
    run_stage_activity,
)


def resolve_workflow_name(mode: str | None = None) -> str:
    """按生效的编排模式返回要调度的工作流名。

    优先级：单次执行的 `mode` → 服务端 `AGENT_ORCHESTRATION_MODE` → `static`。
    非法值一律退回 `static`：宁可按固定流程跑完，也不要调度一个不存在的工作流。
    """

    resolved = mode if mode in ORCHESTRATION_MODES else resolve_orchestration_mode()
    return DYNAMIC_WORKFLOW_NAME if resolved == "dynamic" else WORKFLOW_NAME


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
        # 动态编排链路与静态链路并列注册；未显式启用 dynamic 时永远不会被调度（ADR-019）。
        self._runtime.register_workflow(agent_dynamic_workflow, name=DYNAMIC_WORKFLOW_NAME)
        self._runtime.register_workflow(
            dynamic_subtask_workflow,
            name=DYNAMIC_SUBTASK_WORKFLOW_NAME,
        )
        self._runtime.register_activity(run_stage_activity)
        self._runtime.register_activity(collect_activity)
        self._runtime.register_activity(analyze_activity)
        self._runtime.register_activity(report_activity)
        self._runtime.register_activity(finalize_activity)
        # 问题改写（ADR-037）：两条链路共用的前置步骤。
        self._runtime.register_activity(rewrite_activity)
        self._runtime.register_activity(dynamic_plan_activity)
        self._runtime.register_activity(dynamic_step_activity)
        # ADR-038：动态链路新增的三个活动——intake（改写+意图）、合成、校验，
        # 以及父工作流刷 checkpoint 摘要用的轻量活动。
        self._runtime.register_activity(intake_activity)
        self._runtime.register_activity(dynamic_synthesize_activity)
        self._runtime.register_activity(dynamic_validate_activity)
        self._runtime.register_activity(dynamic_checkpoint_activity)
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
            resolve_workflow_name(task.orchestration_mode),
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

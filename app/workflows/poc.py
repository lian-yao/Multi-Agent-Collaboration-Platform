import argparse
import json
import uuid

import dapr.ext.workflow as wf

from app.core.checkpoint import (
    create_workflow_run,
    init_checkpoint_schema,
    update_workflow_run,
)
from app.orchestration.pipeline import (
    deserialize_pipeline_state,
    pipeline_checkpoint_summary,
)
from app.workflows.pipeline import WORKFLOW_NAME, WorkflowTask


def schedule(
    hold_seconds: int,
    wait: bool,
    workflow_id: str | None = None,
    use_fake_model: bool = True,
) -> None:
    if workflow_id is None:
        init_checkpoint_schema()
        workflow_id = str(uuid.uuid4())
        task = WorkflowTask(
            workflow_id=workflow_id,
            task="分析技术文章并生成报告",
            session_id="demo-session",
            hold_seconds=hold_seconds,
            use_fake_model=use_fake_model,
        )
        create_workflow_run(
            workflow_id=workflow_id,
            session_id=None,
            agent_run_id=None,
        )
        client = wf.DaprWorkflowClient()
        try:
            client.schedule_new_workflow(
                WORKFLOW_NAME,
                input=task.asdict(),
                instance_id=workflow_id,
            )
            update_workflow_run(workflow_id, status="running", instance_id=workflow_id)
            print(f"scheduled workflow_id={workflow_id} instance_id={workflow_id}")
        finally:
            client.close()
        if not wait:
            return
    else:
        print(f"waiting existing workflow_id={workflow_id}")

    client = wf.DaprWorkflowClient()
    try:
        state = client.wait_for_workflow_completion(
            workflow_id, timeout_in_seconds=180
        )
        if state is None:
            update_workflow_run(workflow_id, status="failed", error="state not found")
            raise SystemExit("workflow state not found")
        workflow_result = json.loads(state.serialized_output or "null")
        final_state = deserialize_pipeline_state(workflow_result["state"])
        update_workflow_run(
            workflow_id,
            status="completed",
            checkpoint=pipeline_checkpoint_summary(final_state),
        )
        print(f"workflow_status={state.runtime_status.name}")
        print(
            json.dumps(
                workflow_result["output"],
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument(
        "--use-roles",
        action="store_true",
        help="Call role prompts instead of the deterministic fake model.",
    )
    args = parser.parse_args()
    schedule(
        hold_seconds=args.hold_seconds,
        wait=not args.no_wait,
        workflow_id=args.workflow_id,
        use_fake_model=not args.use_roles,
    )


if __name__ == "__main__":
    main()
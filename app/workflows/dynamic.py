"""动态编排的 Dapr 持久化链路（ADR-019）。

与 ``app.workflows.pipeline`` 的固定三步链路**并列**注册，互不影响；由
``WorkflowService.schedule`` 按生效的编排模式选择工作流名：

- ``static`` → ``agent_pipeline``（既有链路，默认）；
- ``dynamic`` → ``agent_dynamic``（本模块）。

与静态链路的差异，以及为什么这样拆：

- 静态链路每个阶段一个**固定阶段名**的子 Workflow（``{workflow_id}:{stage}``）；
  动态链路的步骤名由规划节点在运行期产出，因此子 Workflow 实例 ID 用
  ``{workflow_id}:dyn:{step_id}``——同样满足「同一次执行重放得到相同实例 ID」，
  断点续跑语义与静态链路一致。
- **规划本身也是一个活动**。规划结果随之进入 Dapr 状态存储，重放时不会重新调用
  规划模型——否则恢复一次就会得到一份新计划，续跑无从谈起。
- 步骤之间是显式依赖关系，某步失败只把依赖它的步骤记为 ``skipped``，
  与之无关的步骤照常执行；只有当整次执行没有任何可用交付物时，工作流才以
  ``failed`` 终止（与业务终态保持一致，见 ADR-016 F-05）。
- 本档步骤**串行**执行；波内并行（fan-out/聚合）见 `doc/orchestration.md` 档 3。

``use_fake_model=True`` 时使用确定性假计划与假步骤输出，供故障恢复演练与
无可用模型的环境使用，口径与静态链路的 ``fake_stage_result`` 一致。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import dapr.ext.workflow as wf

from app.attachments import load_payloads
from app.config import AgentSettings, get_settings
from app.core.agent_config import (
    dispatchable_agents,
    get_agent_registry,
    resolve_agent_settings,
    resolve_agent_tools,
)
from app.core.checkpoint import update_workflow_run
from app.core.tool_audit import AuditedToolRegistry
from app.orchestration.dynamic_graph import (
    PLAN_SOURCE_FALLBACK,
    DynamicPipelineState,
    DynamicPlan,
    PlanStep,
    PlanStepStatus,
    StepOutcome,
    dynamic_checkpoint_summary,
    fallback_plan,
    finalize_state,
    generate_plan,
    resolve_max_plan_steps,
    run_plan_step,
)
from app.orchestration.llm import build_chat_model
from app.orchestration.long_term import USER_MEMORY_ID, preference_block
from app.orchestration.rewrite import PLATFORM_AGENT_ID
from app.orchestration.pipeline import PipelineStatus
from app.orchestration.tools import ToolCaller, default_tool_registry, restricted_registry
from app.orchestration.tools import session_scoped_registry
from app.workflows.pipeline import finalize_activity, rewrite_activity, session_history
from app.workflows.state import save_step_result

DYNAMIC_WORKFLOW_NAME = "agent_dynamic"
DYNAMIC_SUBTASK_WORKFLOW_NAME = "agent_dynamic_subtask"

SUBTASK_RETRY_POLICY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=1),
    max_number_of_attempts=3,
    backoff_coefficient=2,
    max_retry_interval=timedelta(seconds=10),
)

PLANNER_AGENT_ID = PLATFORM_AGENT_ID
"""规划节点的模型解析 id：与问题改写（ADR-037）共用同一个平台节点 id。"""
"""规划节点的配置解析标识。

`resolve_agent_settings` 按 id 读 `agent_configs`；当前没有 `planner` 行，
因此解析结果就是 ADR-017 的默认路由（注册表 → legacy 列 → 环境）。
**这是有意为之**：以后若要让规划用更便宜的模型，只需加一行 `planner` 覆盖，
不必改代码。
"""


def _planner_settings() -> AgentSettings:
    """解析规划节点配置；任何解析失败都退回环境配置，规划不该因配置查询而失败。"""

    try:
        return resolve_agent_settings(PLANNER_AGENT_ID)
    except Exception:
        return get_settings()


def _role_settings(role: str) -> AgentSettings:
    try:
        return resolve_agent_settings(role)
    except Exception:
        return get_settings()


def _dispatch_candidates() -> list[dict[str, Any]] | None:
    """planner 候选集：角色目录里 ``enabled=True`` 的条目（ADR-036）。

    目录为空/读取失败时返回 ``None``——``generate_plan`` 会回退内置三角色候选，
    动态编排不能因为目录这一层不可用就失去候选。
    """

    rows = dispatchable_agents()
    if not rows:
        return None
    # 只挑 planner 需要的字段：system_prompt 不进提示词（人设是执行期的事）。
    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "description": row.get("description"),
        }
        for row in rows
    ]


def _role_catalog(role: str) -> dict[str, dict[str, Any]] | None:
    """单角色目录映射，供 ``run_plan_step`` 解析人设；读取失败返回 ``None``。"""

    try:
        entry = get_agent_registry(role)
    except Exception:
        return None
    return {role: entry} if entry else None


def _role_tools(role: str) -> list[str] | None:
    """该角色被授权的工具名；``None`` 表示未配置（不限制）。

    读取失败回退「未配置」而不是「空清单」：存储不可达时收紧成零工具，会让一次
    基础设施抖动表现成「所有 Agent 突然变傻」，而这个故障看起来像模型问题。
    与 `_role_settings` 同构（都在读不到时退回「什么都不覆盖」）。
    """

    try:
        return resolve_agent_tools(role)
    except Exception:
        return None


def fake_step_outcome(step: PlanStep, task: str) -> StepOutcome:
    """确定性假步骤输出，供恢复演练与无模型环境使用。"""

    return StepOutcome(
        step_id=step.id,
        role=step.role,
        instruction=step.instruction,
        status=PlanStepStatus.COMPLETED,
        content=(
            f"dynamic {step.id} [{step.role}] task={task} "
            f"deps={','.join(step.depends_on) or '-'}"
        ),
    )


def dynamic_plan_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """规划活动：产出协作计划并交给 Dapr 持久化。

    计划产出后**立即**把 `checkpoint.plan` 与 `plan_rationale` 写回 `workflow_runs`
    （状态保持 ``running``），前端才能在「分配」环节就渲染出真实计划步骤，而不是
    等到任务结束才一次性看到（`doc/api.md` §5.22「计划即落盘」）。
    """

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    if task.get("use_fake_model"):
        plan = fallback_plan("演练模式：使用确定性假模型，直接采用固定三步计划。")
    else:
        settings = _planner_settings()
        # 规划也要看得到上一轮：用户只回「重试」时，没有历史连"重试什么"都判断不了。
        history = session_history(task.get("session_id"), task.get("agent_run_id"))
        plan = generate_plan(
            task["task"],
            build_chat_model(settings),
            resolve_max_plan_steps(settings),
            workflow_id=workflow_id,
            history=history,
            preferences=preference_block(USER_MEMORY_ID),
            candidates=_dispatch_candidates(),
        )

    # 计划即落盘：写一份「已规划、尚未执行」的 checkpoint，让轮询能立刻看到计划与理由。
    # 首步同时占住 `current_step`（§5.22「`current_step` 指现在轮到谁」）：前端只认指针
    # 相等的那一步——它才默认摊开、也只有它承接实时工具调用。不写这一笔，动态链路这两条
    # 判定（`RunActivity.tsx` 的 `isOpen` / `isLiveStep`）永远为假。
    first_step = plan.steps[0].id if plan.steps else None
    update_workflow_run(
        workflow_id,
        current_step=first_step,
        checkpoint={
            "mode": "dynamic",
            "status": "running",
            "plan_source": plan.source,
            "plan_rationale": plan.rationale,
            "current_step": first_step,
            "completed_steps": [],
            "plan": [
                {
                    "id": step.id,
                    "role": step.role,
                    "instruction": step.instruction,
                    "depends_on": list(step.depends_on),
                    "status": PlanStepStatus.PENDING.value,
                }
                for step in plan.steps
            ],
        },
    )
    return {"workflow_id": workflow_id, "plan": plan.model_dump(mode="json")}


def dynamic_step_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """步骤活动：执行一个计划步骤，返回该步结果。"""

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    step = PlanStep.model_validate(activity_input["step"])
    results = {
        step_id: StepOutcome.model_validate(payload)
        for step_id, payload in (activity_input.get("results") or {}).items()
    }

    if task.get("use_fake_model"):
        outcome = fake_step_outcome(step, task["task"])
    else:
        registry = default_tool_registry()
        # ADR-035：按**本步骤分配到的角色**收窄工具集。动态链路没有静态那套
        # 「阶段 → 角色」的固定映射，角色来自计划本身（`step.role`），白名单跟着计划走；
        # 同一个角色在两次计划里拿到的是同一份授权，换角色才换工具。
        registry = restricted_registry(registry, _role_tools(step.role))
        # **会话级工具必须在这里挂**（工作区文件工具 ADR-033、会话文件工具 ADR-025）：
        # 静态链路的阶段活动一直是这么做的，动态链路此前漏了这一步——2026-09-23 实测的
        # 现象就是「本会话工具列表里没有任何文件类工具」，连带着 `code_execution` 也拿到
        # 不带工作区挂载的那个实例（沙箱里 `/workspace` 不存在、cwd 退到 `/tmp`）。
        # 顺序与静态链路（pipeline.py 的阶段活动）一致：先按角色白名单收窄，再挂会话级
        # 工具——会话文件工具是「读你自己上传的附件」的兜底能力，一并收窄会让配了白名单
        # 的角色读不了自己的附件。审计包在最外层，读文件同样落进 `tool_calls`。
        registry = session_scoped_registry(registry, task.get("session_id"))
        run_id = task.get("agent_run_id")
        if registry is not None and run_id:
            registry = AuditedToolRegistry(
                registry, run_id=run_id, workflow_run_id=workflow_id
            )
        caller = ToolCaller(registry, scope=workflow_id) if registry is not None else None
        # 只有根步骤（depends_on 为空）需要附件：它们才是直接拿到用户原始任务的步骤。
        # 非根步骤在这里就跳过取数，省掉一次无用的库读——图片附件按 id 取回是实打实的 I/O。
        attachments = (
            tuple(load_payloads([str(value) for value in task.get("attachment_ids") or []]))
            if not step.depends_on
            else ()
        )
        outcome = run_plan_step(
            step,
            task["task"],
            results,
            build_chat_model(_role_settings(step.role)),
            caller,
            workflow_id,
            attachments,
            session_history(task.get("session_id"), task.get("agent_run_id")),
            preference_block(USER_MEMORY_ID),
            _role_catalog(step.role),
        )

    # 每步完成即落盘（`doc/api.md` §5.22「每步完成即推进」）：步骤载荷进状态存储
    # （key `dyn:{step_id}`，§5.17 据此还原该步的输入/产出/工具调用）。
    # checkpoint 进度由父工作流在拿到完整计划后统一推进（那里才有全量 plan）。
    _persist_step_outcome(workflow_id, step, outcome)

    return {"workflow_id": workflow_id, "outcome": outcome.model_dump(mode="json")}


def _persist_step_outcome(
    workflow_id: str,
    step: PlanStep,
    outcome: StepOutcome,
) -> None:
    """把一个计划步骤的结果落盘到状态存储（供 §5.17 轨迹读侧还原）。

    步骤载荷直接序列化 ``StepOutcome``（含 content / tool_calls / instruction），
    上游输入由 §5.17 读侧按 ``depends_on`` 从已完成步骤的 ``content`` 拼出——不在这里
    冗余存一份，避免两份数据各自漂移。
    """

    save_step_result(workflow_id, f"dyn:{step.id}", outcome.model_dump(mode="json"))


def _serialized_results(results: dict[str, StepOutcome]) -> dict[str, Any]:
    return {step_id: outcome.model_dump(mode="json") for step_id, outcome in results.items()}


def _skipped(step: PlanStep) -> StepOutcome:
    return StepOutcome(
        step_id=step.id,
        role=step.role,
        instruction=step.instruction,
        status=PlanStepStatus.SKIPPED,
        error="上游步骤未成功完成，已跳过。",
    )


def dynamic_progress_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """把「已执行到哪一步」写回 ``workflow_runs.checkpoint``，供前端轮询逐步刷新。

    与 `dynamic_checkpoint_summary` 同形（含完整 plan + plan_rationale），差别只在
    ``status`` 恒为 ``running``、``completed_steps`` 只含**已完成**步骤——这样轮询既
    能看到完整计划，也能看到每个步骤依次变绿（`doc/api.md` §5.22「每步完成即推进」）。

    同时把 ``current_step`` 推到**下一个还没跑的步骤**（§5.22「``current_step`` 指现在
    轮到谁」）：口径与静态链路一致——静态也是在上一步落盘时就把指针写成下一步，而不是
    等下一步开跑。前端只认指针相等的那一步（默认摊开 + 承接实时工具调用），指针缺席时
    那两条判定永远为假。

    作为独立活动调度（而非在父工作流体内直接写库）：进度回写是 I/O，放活动里才能
    被 Dapr 持久化、且父工作流重放时不会重复执行落库副作用。
    """

    workflow_id = str(
        activity_input.get("workflow_id") or ctx.workflow_id
    )
    plan = DynamicPlan.model_validate(activity_input["plan"])
    results = {
        step_id: StepOutcome.model_validate(payload)
        for step_id, payload in (activity_input.get("results") or {}).items()
    }
    # 下一个还没出结果的步骤。全部跑完为 None（终态归零）；跳过的步骤在父工作流里已写进
    # results，因此不会被算作「下一个」。
    pending_step = next(
        (step.id for step in plan.steps if step.id not in results), None
    )

    update_workflow_run(
        workflow_id,
        current_step=pending_step,
        checkpoint={
            "mode": "dynamic",
            "status": "running",
            "plan_source": plan.source,
            "plan_rationale": plan.rationale,
            "current_step": pending_step,
            "completed_steps": [
                step_id
                for step_id, res in results.items()
                if res.status is PlanStepStatus.COMPLETED
            ],
            "plan": [
                {
                    "id": step.id,
                    "role": step.role,
                    "instruction": step.instruction,
                    "depends_on": list(step.depends_on),
                    "status": (
                        results[step.id].status.value
                        if step.id in results
                        else PlanStepStatus.PENDING.value
                    ),
                }
                for step in plan.steps
            ],
        },
    )
    return {"workflow_id": workflow_id}


def dynamic_subtask_workflow(
    ctx: wf.DaprWorkflowContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """单个可恢复子任务：一个持久化边界内执行一个计划步骤。"""

    step = PlanStep.model_validate(activity_input["step"])
    ctx.set_custom_status(f"dyn:{step.id}")
    outcome = yield ctx.call_activity(dynamic_step_activity, input=activity_input)
    return outcome


def agent_dynamic_workflow(
    ctx: wf.DaprWorkflowContext,
    task: dict[str, Any],
) -> dict[str, Any]:
    """动态编排父工作流：规划一次，然后按计划顺序（跳过依赖失败的步骤）逐步执行。"""

    workflow_id = str(task.get("workflow_id") or ctx.instance_id)
    terminal_input: dict[str, Any] = {
        "workflow_id": workflow_id,
        "session_id": task.get("session_id"),
        "agent_run_id": task.get("agent_run_id"),
        "message_id": task.get("message_id"),
    }

    try:
        # 前置步骤：先改写再规划（ADR-037）。规划是"谁参与、按什么顺序"，改写是"这句话到底
        # 要什么"——没有改写，规划拿到「重试」这种输入连要重试什么都判断不了。
        rewritten = yield ctx.call_activity(
            rewrite_activity, input={"task": task, "workflow_id": workflow_id}
        )
        task = {**task, "task": rewritten["task"]}
        ctx.set_custom_status("plan")
        planned = yield ctx.call_activity(
            dynamic_plan_activity,
            input={"task": task, "workflow_id": workflow_id},
        )
        plan = DynamicPlan.model_validate(planned["plan"])

        results: dict[str, StepOutcome] = {}
        for step in plan.steps:
            if not all(
                results.get(dep) is not None
                and results[dep].status is PlanStepStatus.COMPLETED
                for dep in step.depends_on
            ):
                # 跳过是瞬时判定，不产生进度回写：跳过步骤的终态由 finalize 的
                # `dynamic_checkpoint_summary` 一次性写入，运行中无需逐条推进。
                results[step.id] = _skipped(step)
                continue
            ctx.set_custom_status(f"dyn:{step.id}")
            completed = yield ctx.call_child_workflow(
                dynamic_subtask_workflow,
                input={
                    "workflow_id": workflow_id,
                    "task": task,
                    "step": step.model_dump(mode="json"),
                    "results": _serialized_results(results),
                },
                instance_id=f"{workflow_id}:dyn:{step.id}",
                retry_policy=SUBTASK_RETRY_POLICY,
            )
            results[step.id] = StepOutcome.model_validate(completed["outcome"])
            yield ctx.call_activity(
                dynamic_progress_activity,
                input={
                    "workflow_id": workflow_id,
                    "plan": plan.model_dump(mode="json"),
                    "results": _serialized_results(results),
                },
            )

        settled = finalize_state(
            DynamicPipelineState(
                task=task["task"],
                # 改写结果随状态进 checkpoint（ADR-037）：不写进去，这一步在界面上就无从审计。
                rewritten_task=rewritten["task"],
                rewrite_source=rewritten["source"],
                status=PipelineStatus.RUNNING,
                plan=plan.steps,
                plan_source=plan.source,
                plan_rationale=plan.rationale,
                results=results,
            ),
            workflow_id,
        )
    except Exception as exc:
        ctx.set_custom_status("failed")
        yield ctx.call_activity(
            finalize_activity,
            input={**terminal_input, "status": "failed", "error": str(exc)},
        )
        raise

    ctx.set_custom_status(
        "completed" if settled.status is PipelineStatus.COMPLETED else "failed"
    )
    yield ctx.call_activity(
        finalize_activity,
        input={
            **terminal_input,
            "status": settled.status.value,
            "checkpoint": dynamic_checkpoint_summary(settled),
            "report": settled.final_output,
            "error": settled.error,
        },
    )
    if settled.status is not PipelineStatus.COMPLETED:
        # 业务终态与 Dapr 实例终态保持一致（ADR-016 F-05）：没有交付物即视为失败，
        # 否则会出现「工作流 completed 但消息 failed」的错位。
        raise RuntimeError(settled.error or "动态编排未产出最终交付物")

    return {
        "output": settled.final_output,
        "state": settled.model_dump(mode="json"),
    }


__all__ = [
    "DYNAMIC_SUBTASK_WORKFLOW_NAME",
    "DYNAMIC_WORKFLOW_NAME",
    "PLANNER_AGENT_ID",
    "PLAN_SOURCE_FALLBACK",
    "agent_dynamic_workflow",
    "dynamic_plan_activity",
    "dynamic_progress_activity",
    "dynamic_step_activity",
    "dynamic_subtask_workflow",
    "fake_step_outcome",
]

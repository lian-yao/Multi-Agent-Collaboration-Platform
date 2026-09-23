"""动态编排的 Dapr 持久化链路（ADR-019 / ADR-038）。

与 ``app.workflows.pipeline`` 的固定三步链路**并列**注册，互不影响；由
``WorkflowService.schedule`` 按生效的编排模式选择工作流名：

- ``static`` → ``agent_pipeline``（既有链路，默认）；
- ``dynamic`` → ``agent_dynamic``（本模块）。

一次动态执行的形状：

    intake_activity（改写 + 意图，一次平台调用）
      ├─ 单 Agent 直答：1 个子工作流 → 收尾（跳过编排、合成、校验）
      └─ 多 Agent：dynamic_plan_activity → 波内并行子工作流（when_all）
                   → 合成活动 → 校验活动 →（不达标则重编排一轮）→ 收尾

关键设计（都要与 LangGraph 进程内镜像保持一致）：

1. **波次并行用 `when_all`**：同一波里彼此无依赖的步骤一次性派发，全部返回后才进下一波；
   并发上限由 `AGENT_MAX_PARALLEL_WORKERS` 控制（同波超过上限时按计划顺序切批）。
2. **子工作流实例 ID 带轮次**：`{workflow_id}:dyn:r{round}:{step_id}`，满足「同一次执行
   重放得到相同实例 ID」；轮次进 ID 是因为重编排的第二轮会有同名的 `s1`，
   不带轮次会与第一轮的实例冲突。
3. **重试落在子工作流内的活动调用上**：`call_activity(..., retry_policy=...)` 按该步配置的
   尝试次数重试；活动最终失败时，子工作流把它**收敛成 `failed` 业务结果**返回——
   这样 `when_all` 不会因为一个分支失败就提前炸掉，同波其它分支的产出也不会丢。
4. **每步载荷落状态存储**（key 带轮次），父工作流在每批**前后**各刷一次
   `workflow_runs.checkpoint` 摘要。摘要是**波粒度**的（父工作流单点写，避免并行子
   工作流互相覆盖），逐步的输入/产出/工具调用则在各自的 key 里（`/stages` 读它们）。
5. **旧计划容忍**：升级瞬间在途的执行会重放旧计划（没有 `retry` / `timeout_seconds` /
   `expected_output`），一律按缺省并发与缺省重试执行，不需要版本号拦截。

`use_fake_model=True` 时使用确定性假计划与假步骤输出，供故障恢复演练与
无可用模型的环境使用，口径与静态链路的 `fake_stage_result` 一致。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterator

import dapr.ext.workflow as wf
from dapr.ext.workflow import when_all

from app.attachments import load_payloads
from app.config import AgentSettings, get_settings
from app.core.agent_config import resolve_agent_settings
from app.core.checkpoint import update_workflow_run
from app.core.tool_audit import AuditedToolRegistry
from app.orchestration.dynamic_graph import (
    BUDGET_SKIP_REASON,
    INTENT_NODE_ID,
    PLAN_NODE_ID,
    PLAN_SOURCE_FALLBACK,
    ROUTE_MULTI,
    ROUTE_SINGLE,
    DynamicPipelineState,
    DynamicPlan,
    PlanStep,
    PlanStepStatus,
    StepOutcome,
    apply_skips,
    budget_exhausted,
    dynamic_checkpoint_summary,
    dynamic_state_label,
    dynamic_subtask_instance_id,
    effective_attempts,
    effective_timeout,
    failure_triples,
    fallback_plan,
    finalize_state,
    generate_plan,
    pending_batch,
    resolve_max_parallel_workers,
    resolve_max_plan_rounds,
    resolve_max_plan_steps,
    resolve_token_budget,
    run_plan_step,
    single_agent_plan,
    tokens_used,
)
from app.orchestration.intake import IntentResult, intake_task
from app.orchestration.llm import build_chat_model
from app.orchestration.long_term import USER_MEMORY_ID, preference_block
from app.orchestration.pipeline import PipelineStatus
from app.orchestration.rewrite import PLATFORM_AGENT_ID, resolve_platform_settings
from app.orchestration.synthesis import (
    SYNTHESIZE_NODE_ID,
    VALIDATE_NODE_ID,
    ValidationResult,
    run_synthesis,
    run_validation,
)
from app.orchestration.tools import (
    ToolCaller,
    default_tool_registry,
    session_scoped_registry,
)
from app.workflows.pipeline import (
    _attachment_names,
    finalize_activity,
    session_history,
)
from app.workflows.state import save_step_result

DYNAMIC_WORKFLOW_NAME = "agent_dynamic"
DYNAMIC_SUBTASK_WORKFLOW_NAME = "agent_dynamic_subtask"

STATE_TRACE_PREVIOUS_LIMIT = 16_000
"""落进状态存储的「上游输入」字符上限（只影响轨迹副本，不影响模型输入）。"""

PLANNER_AGENT_ID = PLATFORM_AGENT_ID
"""规划 / 意图 / 校验共用的平台节点配置标识。

`resolve_agent_settings` 按 id 读 `agent_configs`；当前没有 `planner` 行，
因此解析结果就是 ADR-017 的默认路由（注册表 → legacy 列 → 环境）。
**这是有意为之**：以后若要让规划用更便宜的模型，只需加一行 `planner` 覆盖，
不必改代码。
"""

SUBTASK_FIRST_RETRY_INTERVAL = timedelta(seconds=1)
SUBTASK_MAX_RETRY_INTERVAL = timedelta(seconds=10)
SUBTASK_BACKOFF_COEFFICIENT = 2


def retry_backoff(attempt: int) -> timedelta:
    """第 `attempt` 次尝试（1 起，指刚失败的那一次）之后的等待时长。

    与原先挂在 `RetryPolicy` 上的参数同口径（首次 1s、系数 2、上限 10s）。**重试改成
    子工作流里的显式循环**之后，退避要自己发：`ctx.create_timer` 是持久化的，
    重放时不会重复等待。
    """

    seconds = SUBTASK_FIRST_RETRY_INTERVAL.total_seconds() * (
        SUBTASK_BACKOFF_COEFFICIENT ** max(0, attempt - 1)
    )
    return timedelta(
        seconds=min(seconds, SUBTASK_MAX_RETRY_INTERVAL.total_seconds())
    )


class StepAttemptFailed(RuntimeError):
    """一次子任务尝试失败。

    抛出它有两个作用：让 Dapr 的重试策略生效（活动抛错才重试），以及在重试耗尽后
    由子工作流捕获、转成业务上的 `failed` 结果。
    """


def resolve_subtask_attempts(step: PlanStep, activity_input: dict[str, Any]) -> int:
    """该步的最大尝试次数（含首次）：优先子工作流输入，其次计划，最后服务端配置。"""

    raw = activity_input.get("max_attempts")
    if raw:
        return max(1, int(raw))
    return effective_attempts(step, get_settings())


def _planner_settings() -> AgentSettings:
    """解析平台节点配置；任何解析失败都退回环境配置，规划不该因配置查询而失败。"""

    return resolve_platform_settings()


def _role_settings(role: str) -> AgentSettings:
    try:
        return resolve_agent_settings(role)
    except Exception:
        return get_settings()


def fake_step_outcome(step: PlanStep, task: str) -> StepOutcome:
    """确定性假步骤输出，供恢复演练与无模型环境使用。"""

    return StepOutcome(
        step_id=step.id,
        role=step.role,
        instruction=step.instruction,
        status=PlanStepStatus.COMPLETED,
        content=(
            f"dynamic {step.id} [{step.role.value}] task={task} "
            f"deps={','.join(step.depends_on) or '-'}"
        ),
    )


def _node_payload(
    node_id: str,
    *,
    status: str,
    content: str = "",
    previous: dict[str, Any] | None = None,
    tool_calls: Any = (),
    error: str | None = None,
    attempt: int | None = None,
    attempts: int | None = None,
    tokens: int | None = None,
) -> dict[str, Any]:
    """状态存储里的单节点载荷。

    字段刻意与静态链路的阶段载荷对齐（`step` / `status` / `content` / `previous` /
    `tool_calls`），这样 `/stages` 的截断与工具调用归一化可以原样复用，不需要为
    动态链路再写一套渲染。`error` 是动态链路多出来的一个字段（失败步骤要说得清原因）。
    """

    calls: list[dict[str, Any]] = []
    for record in tool_calls or ():
        calls.append(
            record if isinstance(record, dict) else record.model_dump(mode="json")
        )
    return {
        "step": node_id,
        "status": status,
        "content": content,
        "previous": previous,
        "tool_calls": calls,
        "error": error,
        # 执行明细（ADR-038 §8）：第几次尝试、实际尝试了几次、花了多少 Token。
        # 取不到就不写这两个键（写 None 会被读侧当成"有值但为空"）。
        **({"attempt": attempt} if attempt is not None else {}),
        **({"attempts": attempts} if attempts is not None else {}),
        **({"tokens": tokens} if tokens is not None else {}),
    }


def _upstream_payload(
    step: PlanStep,
    results: dict[str, StepOutcome],
) -> dict[str, Any] | None:
    """该步实际读到的上游输入（用于轨迹展示，超长按上限截断）。"""

    blocks = [
        f"【{dep} · {results[dep].role.value}】\n{results[dep].content}"
        for dep in step.depends_on
        if dep in results
    ]
    if not blocks:
        return None
    joined = "\n\n".join(blocks)
    if len(joined) > STATE_TRACE_PREVIOUS_LIMIT:
        joined = joined[:STATE_TRACE_PREVIOUS_LIMIT]
    return {"step": ",".join(step.depends_on), "content": joined}


def _tool_registry_for(task: dict[str, Any], workflow_id: str):
    """挂会话级工具并包审计层，口径与静态链路的阶段活动一致。"""

    registry = default_tool_registry()
    if registry is None:
        # 解析不到注册表（例如无 MCP 实现）时不调工具，与静态图口径一致。
        return None
    registry = session_scoped_registry(registry, task.get("session_id"))
    run_id = task.get("agent_run_id")
    if registry is not None and run_id:
        registry = AuditedToolRegistry(registry, run_id=run_id, workflow_run_id=workflow_id)
    return registry


def _tool_caller(registry: Any, workflow_id: str, stage: str) -> ToolCaller | None:
    """注册表缺失时不建调用器：`ToolCaller` 构造就会问目录，传 None 会直接崩。"""

    return ToolCaller(registry, scope=workflow_id, stage=stage) if registry is not None else None


def intake_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """intake 活动：改写 + 意图识别（一次平台调用，ADR-038 §1）。"""

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    if task.get("use_fake_model"):
        text = task["task"]
        return {
            "workflow_id": workflow_id,
            "task": text,
            "source": "original",
            "original_chars": len(text),
            "task_chars": len(text),
            "intent": None,
            "intent_source": "fallback",
        }
    # intake 也要看得到上一轮：用户只回「重试」时，没有历史连"重试什么"都判断不了。
    history = session_history(task.get("session_id"), task.get("agent_run_id"))
    return {
        "workflow_id": workflow_id,
        **intake_task(
            task.get("task") or "",
            history=history,
            preferences=preference_block(USER_MEMORY_ID),
            attachment_names=_attachment_names(task),
            workflow_id=workflow_id,
        ),
    }


def _plan_trace_text(plan: DynamicPlan) -> str:
    """计划节点的轨迹正文：让人一眼看出安排了什么、为什么这样安排。"""

    lines = [
        f"- {step.id}（{step.role.value}）依赖 {','.join(step.depends_on) or '无'}："
        f"{step.instruction}"
        for step in plan.steps
    ]
    rationale = plan.rationale or "（无说明）"
    return f"理由：{rationale}\n计划来源：{plan.source}\n" + "\n".join(lines)


def dynamic_plan_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """规划活动：产出协作计划并交给 Dapr 持久化。"""

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    round_number = int(activity_input.get("round") or 1)
    raw_intent = activity_input.get("intent")
    intent = IntentResult.model_validate(raw_intent) if raw_intent else None
    defects = [str(item) for item in activity_input.get("defects") or []]

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
            intent=intent,
            defects=defects,
        )
    save_step_result(
        workflow_id,
        dynamic_state_label(PLAN_NODE_ID, round_number),
        _node_payload(
            PLAN_NODE_ID,
            status="completed",
            content=_plan_trace_text(plan),
        ),
    )
    return {
        "workflow_id": workflow_id,
        "round": round_number,
        "plan": plan.model_dump(mode="json"),
    }


def dynamic_step_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """步骤活动：执行一个计划步骤，落盘该步轨迹，返回该步结果。

    **失败时抛错**（而不是把失败当返回值）：重试策略挂在活动调用上，只有抛错才会重试。
    重试耗尽后由子工作流捕获并转成业务上的 `failed` 结果——这样同波其它分支不受影响。
    """

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    round_number = int(activity_input.get("round") or 1)
    attempt = max(1, int(activity_input.get("attempt") or 1))
    step = PlanStep.model_validate(activity_input["step"])
    results = {
        step_id: StepOutcome.model_validate(payload)
        for step_id, payload in (activity_input.get("results") or {}).items()
    }
    settings = get_settings()

    if task.get("use_fake_model"):
        outcome = fake_step_outcome(step, task["task"])
    else:
        registry = _tool_registry_for(task, workflow_id)
        caller = _tool_caller(registry, workflow_id, f"dyn:{step.id}")
        # 只有根步骤需要附件：它们才是直接拿到用户原始任务的步骤。非根步骤这里跳过取数，
        # 省掉一次无用（且可能很贵）的库读。
        attachments = (
            tuple(load_payloads([str(value) for value in task.get("attachment_ids") or []]))
            if not step.depends_on
            else ()
        )
        outcome = run_plan_step(
            step,
            task["task"],
            results,
            None,
            caller,
            workflow_id,
            attachments,
            session_history(task.get("session_id"), task.get("agent_run_id")),
            preference_block(USER_MEMORY_ID),
            settings=_role_settings(step.role.value),
            timeout_seconds=effective_timeout(step, settings),
        )

    save_step_result(
        workflow_id,
        dynamic_state_label(step.id, round_number),
        _node_payload(
            step.id,
            status=outcome.status.value,
            content=outcome.content,
            previous=_upstream_payload(step, results),
            tool_calls=outcome.tool_calls,
            error=outcome.error,
            attempt=attempt,
            attempts=outcome.attempts,
            tokens=outcome.tokens,
        ),
    )
    if outcome.status is PlanStepStatus.FAILED:
        raise StepAttemptFailed(outcome.error or f"步骤 {step.id} 执行失败")
    # 成功：把「第几次尝试成功的」记下来——`attempts` 是审计字段，
    # 「配了 2 次重试」与「真的重试了 1 次才成功」不是一回事。
    settled = outcome.model_copy(update={"attempts": attempt})
    return {"workflow_id": workflow_id, "outcome": settled.model_dump(mode="json")}


def _state_probe(
    task: dict[str, Any],
    plan: DynamicPlan,
    results: dict[str, StepOutcome],
    round_number: int,
) -> DynamicPipelineState:
    """构造一个只用于算「失败/跳过清单」的轻量状态。"""

    return DynamicPipelineState(
        task=task["task"],
        round=round_number,
        plan=plan.steps,
        results=results,
        status=PipelineStatus.RUNNING,
    )


def dynamic_synthesize_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """合成活动：把本轮所有可用子任务产出合成面向用户的交付物。"""

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    round_number = int(activity_input.get("round") or 1)
    plan = DynamicPlan.model_validate(activity_input["plan"])
    results = {
        step_id: StepOutcome.model_validate(payload)
        for step_id, payload in (activity_input.get("results") or {}).items()
    }
    raw_intent = activity_input.get("intent")
    intent = IntentResult.model_validate(raw_intent) if raw_intent else None

    failed, skipped = failure_triples(_state_probe(task, plan, results, round_number))
    contents = {
        step.id: results[step.id].content
        for step in plan.steps
        if step.id in results and results[step.id].status is PlanStepStatus.COMPLETED
    }
    status = "failed"
    error: str | None = None
    tool_calls: Any = ()
    tokens = 0

    if task.get("use_fake_model"):
        outcome_content = "synthesized: " + " | ".join(
            f"{step_id}={content[:40]}" for step_id, content in contents.items()
        )
        status = "completed"
    else:
        registry = _tool_registry_for(task, workflow_id)
        caller = _tool_caller(registry, workflow_id, SYNTHESIZE_NODE_ID)
        outcome = run_synthesis(
            task["task"],
            contents,
            settings=resolve_platform_settings(),
            intent=intent,
            failed=failed,
            skipped=skipped,
            caller=caller,
            workflow_id=workflow_id,
            history=session_history(task.get("session_id"), task.get("agent_run_id")),
            preferences=preference_block(USER_MEMORY_ID),
        )
        outcome_content = outcome.content
        tool_calls = outcome.tool_calls
        status = outcome.status
        error = outcome.error
        tokens = outcome.tokens

    previous = {
        "step": ",".join(contents),
        "content": "\n\n".join(
            f"【{step_id}】\n{content}" for step_id, content in contents.items()
        )[:STATE_TRACE_PREVIOUS_LIMIT],
    }
    save_step_result(
        workflow_id,
        dynamic_state_label(SYNTHESIZE_NODE_ID, round_number),
        _node_payload(
            SYNTHESIZE_NODE_ID,
            status=status,
            content=outcome_content,
            previous=previous if contents else None,
            tool_calls=tool_calls,
            error=error,
            tokens=tokens,
        ),
    )
    return {
        "workflow_id": workflow_id,
        "round": round_number,
        "status": status,
        "content": outcome_content,
        "tool_calls": list(tool_calls or ()),
        "error": error,
        # 合成用量进「本次执行累计预算」（ADR-038 §9）。
        "tokens": tokens,
    }


def _validation_trace_text(result: ValidationResult) -> str:
    lines = [
        f"是否满足原始意图：{'是' if result.satisfied else '否'}",
        f"判定来源：{result.source}",
    ]
    if result.defects:
        lines.append("问题：" + "；".join(result.defects))
    if result.missing:
        lines.append("缺失：" + "；".join(result.missing))
    return "\n".join(lines)


def dynamic_validate_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """校验活动：判定交付物是否满足原始意图（不达标才会触发重编排）。"""

    task = activity_input["task"]
    workflow_id = str(
        activity_input.get("workflow_id") or task.get("workflow_id") or ctx.workflow_id
    )
    round_number = int(activity_input.get("round") or 1)
    deliverable = str(activity_input.get("deliverable") or "")
    plan = DynamicPlan.model_validate(activity_input["plan"])
    results = {
        step_id: StepOutcome.model_validate(payload)
        for step_id, payload in (activity_input.get("results") or {}).items()
    }
    raw_intent = activity_input.get("intent")
    intent = IntentResult.model_validate(raw_intent) if raw_intent else None
    failed, _skipped = failure_triples(_state_probe(task, plan, results, round_number))

    if task.get("use_fake_model"):
        result = ValidationResult(satisfied=True, source="fallback")
    else:
        result = run_validation(
            task["task"],
            deliverable,
            settings=resolve_platform_settings(),
            intent=intent,
            failed=failed,
            workflow_id=workflow_id,
        )
    save_step_result(
        workflow_id,
        dynamic_state_label(VALIDATE_NODE_ID, round_number),
        _node_payload(
            VALIDATE_NODE_ID,
            status="completed" if result.satisfied else "failed",
            content=_validation_trace_text(result),
            previous={"step": SYNTHESIZE_NODE_ID, "content": deliverable},
            error=None,
        ),
    )
    return {
        "workflow_id": workflow_id,
        "round": round_number,
        "validation": result.model_dump(mode="json"),
    }


def dynamic_checkpoint_activity(
    ctx: wf.WorkflowActivityContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """把当前摘要写回 ``workflow_runs.checkpoint``（父工作流单点写，避免并行覆盖）。"""

    workflow_id = str(activity_input["workflow_id"])
    update_workflow_run(
        workflow_id,
        status="running",
        checkpoint=activity_input.get("checkpoint"),
    )
    return {"workflow_id": workflow_id}


def _serialized_results(results: dict[str, StepOutcome]) -> dict[str, Any]:
    return {step_id: outcome.model_dump(mode="json") for step_id, outcome in results.items()}


def _dependency_results(
    step: PlanStep, results: dict[str, StepOutcome]
) -> dict[str, Any]:
    """只把该步真正需要的上游结果传给子工作流，控制 Dapr 载荷体积。"""

    return {
        dep: results[dep].model_dump(mode="json")
        for dep in step.depends_on
        if dep in results
    }


def _failed_outcome(
    step: PlanStep, exc: BaseException, *, attempts: int = 1
) -> StepOutcome:
    return StepOutcome(
        step_id=step.id,
        role=step.role,
        instruction=step.instruction,
        status=PlanStepStatus.FAILED,
        error=f"{type(exc).__name__}: {exc}",
        attempts=attempts,
    )


def dynamic_subtask_workflow(
    ctx: wf.DaprWorkflowContext,
    activity_input: dict[str, Any],
) -> dict[str, Any]:
    """单个可恢复子任务：一个持久化边界内执行一个计划步骤（含按步重试）。

    **重试是这里的一层显式循环**，不是 `call_activity(retry_policy=...)`：
    Dapr 的重试策略不把「第几次尝试」告诉工作流代码，而需求要的是**实际**尝试次数
    可审计（checkpoint 的 `attempts`）。循环里的每次尝试都是一个持久化的活动调用，
    退避走 `create_timer`，因此重放语义与重试策略一致，只是把 attempt 计数握在自己手里。
    """

    step = PlanStep.model_validate(activity_input["step"])
    round_number = int(activity_input.get("round") or 1)
    max_attempts = resolve_subtask_attempts(step, activity_input)
    ctx.set_custom_status(dynamic_state_label(step.id, round_number))
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            yield ctx.create_timer(retry_backoff(attempt - 1))
        try:
            completed = yield ctx.call_activity(
                dynamic_step_activity,
                input={
                    **activity_input,
                    "round": round_number,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都进入下一次尝试
            last_error = exc
            continue
        # `attempts` 由**这里**盖章（而不是指望活动结果里自带）：子工作流才是
        # 那个知道"这是第几次"的执行边界。
        return {
            **completed,
            "outcome": {**(completed.get("outcome") or {}), "attempts": attempt},
        }

    # 重试耗尽：收敛成业务失败返回，而不是把异常抛给父工作流——
    # `when_all` 会因任一分支抛错而提前失败，那会连带丢掉同波其它分支的结果。
    ctx.set_custom_status(f"{dynamic_state_label(step.id, round_number)}:failed")
    assert last_error is not None
    return {
        "workflow_id": activity_input.get("workflow_id"),
        "outcome": _failed_outcome(step, last_error, attempts=max_attempts).model_dump(
            mode="json"
        ),
    }


def _run_waves(
    ctx: wf.DaprWorkflowContext,
    *,
    workflow_id: str,
    task: dict[str, Any],
    plan: DynamicPlan,
    base_state: DynamicPipelineState,
    round_number: int,
    results: dict[str, StepOutcome] | None = None,
) -> Iterator[Any]:
    """按波执行计划，返回 `{步骤 id: 结果}`。

    每一批的流程是：连坐判定 → 刷一次 checkpoint → 并行派发子工作流 → 收结果 →
    再刷一次 checkpoint。摘要写两遍是为了让暂停/翻看时看到的进度是真的：
    「正在跑哪一波」与「这一波跑完了」是两种状态，不能只在波后写一次。
    """

    settings = get_settings()
    max_parallel = resolve_max_parallel_workers(settings)
    current: dict[str, StepOutcome] = dict(results or {})

    def probe_with(latest: dict[str, StepOutcome]) -> DynamicPipelineState:
        return base_state.model_copy(update={"results": latest, "plan": plan.steps})

    while True:
        probe = probe_with(current)
        if budget_exhausted(probe):
            # 成本闸门：预算用尽就不再开新批，把剩余步骤标成 skipped（原因写明是预算），
            # 然后交给合成器——**已经拿到的产出照样交付**，只是如实说明还差哪些。
            stopped = _budget_skip_outcomes(probe)
            if stopped:
                current.update(stopped)
            else:
                return current
            yield ctx.call_activity(
                dynamic_checkpoint_activity,
                input={
                    "workflow_id": workflow_id,
                    "checkpoint": dynamic_checkpoint_summary(probe_with(current)),
                },
            )
            return current
        skips = apply_skips(probe)
        if skips:
            current.update(skips)
            probe = probe_with(current)
        batch = pending_batch(probe, plan.steps, max_parallel)
        if not batch:
            return current

        yield ctx.call_activity(
            dynamic_checkpoint_activity,
            input={
                "workflow_id": workflow_id,
                "checkpoint": dynamic_checkpoint_summary(probe_with(current)),
            },
        )


        tasks = [
            ctx.call_child_workflow(
                dynamic_subtask_workflow,
                input={
                    "workflow_id": workflow_id,
                    "task": task,
                    "step": step.model_dump(mode="json"),
                    "results": _dependency_results(step, current),
                    "round": round_number,
                    "max_attempts": effective_attempts(step, settings),
                },
                instance_id=dynamic_subtask_instance_id(workflow_id, step.id, round_number),
            )
            for step in batch
        ]
        # `when_all` 是 **dapr.ext.workflow 的模块级函数**，不是上下文方法：
        # 真实运行时给工作流的上下文是 `_RuntimeOrchestrationContext`，它没有这个属性
        # （2026-09-24 在真实 sidecar 上实测到的 `AttributeError`——替身假装成方法时，
        # 只有真的跑在 Dapr 上才会暴露）。
        completed = yield when_all(tasks)
        for step, item in zip(batch, completed):
            current[step.id] = StepOutcome.model_validate(item["outcome"])

        yield ctx.call_activity(
            dynamic_checkpoint_activity,
            input={
                "workflow_id": workflow_id,
                "checkpoint": dynamic_checkpoint_summary(probe_with(current)),
            },
        )


def _budget_skip_outcomes(state: DynamicPipelineState) -> dict[str, StepOutcome]:
    """预算用尽时给还没跑的步骤补 `skipped` 结果（原因：预算，而非上游失败）。"""

    from app.orchestration.dynamic_graph import apply_budget_stop

    return apply_budget_stop(state)


def agent_dynamic_workflow(
    ctx: wf.DaprWorkflowContext,
    task: dict[str, Any],
) -> dict[str, Any]:
    """动态编排父工作流（ADR-038）：intake →（直答 | 波次并行 + 合成 + 校验）→ 收尾。"""

    workflow_id = str(task.get("workflow_id") or ctx.instance_id)
    terminal_input: dict[str, Any] = {
        "workflow_id": workflow_id,
        "session_id": task.get("session_id"),
        "agent_run_id": task.get("agent_run_id"),
        "message_id": task.get("message_id"),
    }

    try:
        # 前置步骤：改写 + 意图（一次平台调用，ADR-038 §1）。规划是"谁参与、按什么顺序"，
        # intake 是"这句话到底要什么、要不要拆"——没有它，规划拿到「重试」也无从判断。
        ctx.set_custom_status("intake")
        intake = yield ctx.call_activity(
            intake_activity, input={"task": task, "workflow_id": workflow_id}
        )
        current_task = {**task, "task": intake["task"]}
        raw_intent = intake.get("intent")
        intent = IntentResult.model_validate(raw_intent) if raw_intent else None
        route = (
            ROUTE_SINGLE
            if intent is not None and not intent.need_multi_subtask
            else ROUTE_MULTI
        )
        settings = get_settings()

        seed = DynamicPipelineState(
            task=current_task["task"],
            rewritten_task=intake["task"],
            rewrite_source=intake["source"],
            intent=intent,
            intent_source=intake.get("intent_source"),
            route=route,
            # 成本闸门与预算口径（ADR-038 §9）：intake 的用量从这里开始累计，
            # 后面每一步、合成与校验的用量都加到同一条账上。
            token_budget=resolve_token_budget(settings),
            platform_tokens=int(intake.get("tokens") or 0),
            status=PipelineStatus.RUNNING,
        )
        if intent is not None:
            save_step_result(
                workflow_id,
                dynamic_state_label(INTENT_NODE_ID, 1),
                _node_payload(
                    INTENT_NODE_ID,
                    status="completed",
                    content=(
                        f"类型：{intent.intent_type}\n目标：{intent.user_goal}\n"
                        f"约束：{'；'.join(intent.constraints) or '无'}\n"
                        f"需要多 Agent：{'是' if intent.need_multi_subtask else '否'}"
                    ),
                ),
            )

        if route == ROUTE_SINGLE:
            # 单 Agent 直答：跳过编排、并行、合成与校验（护栏与理由见 ADR-038 §2）。
            plan = single_agent_plan(current_task["task"], intent)
            seed = seed.model_copy(
                update={
                    "plan": plan.steps,
                    "plan_source": plan.source,
                    "plan_rationale": plan.rationale,
                }
            )
            results = yield from _run_waves(
                ctx,
                workflow_id=workflow_id,
                task=current_task,
                plan=plan,
                base_state=seed,
                round_number=1,
            )
            sealed = seed.model_copy(update={"results": results})
            settled = finalize_state(
                sealed.model_copy(update={"budget_exceeded": budget_exhausted(sealed)}),
                workflow_id,
            )
        else:
            max_rounds = resolve_max_plan_rounds(settings)
            validation: ValidationResult | None = None
            defects: list[str] = []
            round_number = 1
            plan = DynamicPlan(steps=[], source=PLAN_SOURCE_FALLBACK)
            results = {}
            final_output: str | None = None
            carried_tokens = 0
            round_state = seed

            while True:
                ctx.set_custom_status(f"plan:r{round_number}")
                planned = yield ctx.call_activity(
                    dynamic_plan_activity,
                    input={
                        "task": current_task,
                        "workflow_id": workflow_id,
                        "round": round_number,
                        "intent": raw_intent,
                        "defects": defects,
                    },
                )
                plan = DynamicPlan.model_validate(planned["plan"])
                current = seed.model_copy(
                    update={
                        "round": round_number,
                        "plan": plan.steps,
                        "plan_source": plan.source,
                        "plan_rationale": plan.rationale,
                        "results": {},
                        # 上一轮花掉的用量要带过来，否则重编排会把预算重置成满额。
                        "tokens_used_prior": carried_tokens,
                        "platform_tokens": seed.platform_tokens + plan.tokens,
                        "synthesis_tokens": 0,
                    }
                )
                results = yield from _run_waves(
                    ctx,
                    workflow_id=workflow_id,
                    task=current_task,
                    plan=plan,
                    base_state=current,
                    round_number=round_number,
                )

                ctx.set_custom_status(f"synthesize:r{round_number}")
                synthesized = yield ctx.call_activity(
                    dynamic_synthesize_activity,
                    input={
                        "task": current_task,
                        "workflow_id": workflow_id,
                        "round": round_number,
                        "plan": plan.model_dump(mode="json"),
                        "results": _serialized_results(results),
                        "intent": raw_intent,
                    },
                )
                final_output = (
                    synthesized["content"]
                    if synthesized["status"] == "completed"
                    else None
                )

                if not settings.validation_enabled:
                    validation = None
                    break
                ctx.set_custom_status(f"validate:r{round_number}")
                checked = yield ctx.call_activity(
                    dynamic_validate_activity,
                    input={
                        "task": current_task,
                        "workflow_id": workflow_id,
                        "round": round_number,
                        "plan": plan.model_dump(mode="json"),
                        "results": _serialized_results(results),
                        "intent": raw_intent,
                        "deliverable": final_output or "",
                    },
                )
                validation = ValidationResult.model_validate(checked["validation"])
                round_state = current.model_copy(
                    update={
                        "results": results,
                        "synthesis_tokens": int(synthesized.get("tokens") or 0),
                        "validation": validation,
                        "platform_tokens": current.platform_tokens
                        + int(validation.tokens or 0),
                    }
                )
                if (
                    validation.satisfied
                    or round_number > max_rounds
                    # 预算用尽就不再开第二轮：重编排按定义要再花一份钱。
                    or budget_exhausted(round_state)
                ):
                    break
                defects = [*validation.defects, *validation.missing]
                carried_tokens = tokens_used(round_state)
                round_number += 1
                ctx.set_custom_status(f"replan:r{round_number}")

            if not settings.validation_enabled:
                # 没跑校验时 `round_state` 还没被赋值：用合成结果补一份。
                round_state = current.model_copy(
                    update={
                        "results": results,
                        "synthesis_tokens": int(synthesized.get("tokens") or 0),
                    }
                )
            settled = finalize_state(
                round_state.model_copy(
                    update={
                        "route": ROUTE_MULTI,
                        "round": round_number,
                        "final_output": final_output,
                        "validation": validation,
                        "validation_rounds": max(0, round_number - 1),
                        "defects": defects,
                        "budget_exceeded": budget_exhausted(round_state),
                    }
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
    "StepAttemptFailed",
    "agent_dynamic_workflow",
    "dynamic_checkpoint_activity",
    "dynamic_plan_activity",
    "dynamic_step_activity",
    "dynamic_subtask_workflow",
    "dynamic_synthesize_activity",
    "dynamic_validate_activity",
    "fake_step_outcome",
    "intake_activity",
    "subtask_retry_policy",
]

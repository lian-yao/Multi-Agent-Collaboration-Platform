"""动态编排图（档 2）：由规划节点按任务决定「谁参与、以什么顺序参与」。

与 ``app.orchestration.pipeline_graph`` 的固定三步图**并列**存在，互不影响：

- 静态图：``collector → analyst → reporter``，拓扑与角色分配写死在
  ``PIPELINE_ROLE_ASSIGNMENT``；
- 本模块：``planner →（按依赖就绪度循环 execute）→ finalize``，角色与顺序由规划节点
  在运行期产出。

设计要点（决策记录见 `doc/decisions/019-dynamic-orchestration-graph.md`）：

1. **另写一套状态，不放宽静态状态机**。``PipelineState`` 的 ``current_step`` 是固定
   三元组 ``PipelineStage``，``complete_step`` 强校验顺序；动态路径若复用它会要求放宽
   这套校验，伤及静态链路的既有保证（含 Dapr 侧按阶段拆子 Workflow 的可恢复性）。
   因此本模块自带 ``DynamicPipelineState``，静态契约一个字节都不动。
2. **规划失败必须能降级，不能失败**。规划节点拿到的是一段自由文本，模型可能返回
   Markdown 围栏、解释性前后缀或非法角色；``parse_plan`` 逐条校验，任何一处不合法就
   整份丢弃并回退到 ``fallback_plan()``（固定三步）。**不猜、不修补半份计划**——
   修补出来的计划比固定三步更不可预期。
3. **执行按「依赖就绪」调度**。规划产出的每一步声明 ``depends_on``，节点每次取
   首个「依赖全部完成」的步骤执行，把上游正文按步骤标签拼进输入。因此同一角色可以
   在一次执行里出现多次（例如先收集 A 再收集 B），这是静态图做不到的。
4. **失败只连坐下游**。某步抛错只把该步记为 ``failed``，依赖它的步骤记为 ``skipped``，
   与之无关的步骤照常执行——动态编排下步骤之间是显式依赖关系，不该整条链一起死。
5. **本档为串行执行**。图拓扑一次只放行一个就绪步骤；波内并行（fan-out）与结果聚合
   属档 3，见 `doc/orchestration.md`。

本模块只依赖编排层既有契约与角色定义，不引入新的外部依赖；Dapr 侧的持久化编排
（按步骤拆子 Workflow）在 ``app.workflows.dynamic``。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.agents.roles import RoleId, get_role
from app.attachments import AttachmentPayload, build_human_content
from app.config import AgentSettings, get_settings
from app.observability.instrumentation import observed_stage
from app.observability.logging import get_logger, log_event
from app.orchestration.llm import build_chat_model
from app.orchestration.pipeline import PipelineStatus
from app.orchestration.pipeline_graph import (
    content_with_tools,
    invoke_role_messages,
)
from app.orchestration.tools import ToolCaller, ToolCallRecord, ToolRegistry, default_tool_registry

ORCHESTRATION_MODES: tuple[str, ...] = ("static", "dynamic")
"""编排模式取值，对齐 ``AgentSettings.orchestration_mode``。"""

DEFAULT_MAX_PLAN_STEPS = 6
"""一次执行允许的最大步骤数。既是成本上限，也是回退判据：超出的计划整份丢弃。"""

PLAN_SOURCE_LLM = "llm"
PLAN_SOURCE_FALLBACK = "fallback"

logger = get_logger("orchestration.dynamic")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class PlanStepStatus(StrEnum):
    """单个计划步骤的执行状态。"""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """规划节点产出的一个步骤：由哪个角色、做什么、依赖谁。"""

    id: str = Field(min_length=1, description="步骤 id，同一份计划内唯一，如 s1")
    role: RoleId
    instruction: str = Field(min_length=1, description="该步骤的职责说明，拼进角色输入")
    depends_on: list[str] = Field(default_factory=list, description="依赖的步骤 id")


class DynamicPlan(BaseModel):
    """一次执行的协作计划。"""

    steps: list[PlanStep] = Field(default_factory=list)
    source: Literal["llm", "fallback"] = PLAN_SOURCE_FALLBACK
    rationale: str = ""
    """规划理由；回退时说明回退原因，便于前端与日志区分「真的规划过」与「降级了」。"""


class StepOutcome(BaseModel):
    """一个步骤的执行结果，字段与静态链路 ``_stage_result`` 对齐以便复用展示与审计。"""

    step_id: str
    role: RoleId
    instruction: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0


class DynamicPipelineState(BaseModel):
    """动态编排的可序列化状态。

    与 ``PipelineState`` 的差异：没有 ``current_step`` 单值指针，改为用
    ``results`` 里已有的步骤反推「下一步能跑谁」——依赖关系才是这个图的第一公民。
    """

    task: str = Field(min_length=1)
    status: PipelineStatus = PipelineStatus.PENDING
    plan: list[PlanStep] = Field(default_factory=list)
    plan_source: str = PLAN_SOURCE_FALLBACK
    plan_rationale: str = ""
    results: dict[str, StepOutcome] = Field(default_factory=dict)
    final_output: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# 计划解析与回退
# --------------------------------------------------------------------------------------


def _extract_json(text: str) -> Any | None:
    """从模型输出里取出 JSON 载荷，容忍 Markdown 围栏与解释性前后缀。"""

    candidate = (text or "").strip()
    if not candidate:
        return None
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_plan(text: str, max_steps: int = DEFAULT_MAX_PLAN_STEPS) -> DynamicPlan | None:
    """把规划模型的自由文本解析成计划；任一处不合法返回 ``None``。

    校验口径（全部通过才接受）：

    - 顶层是对象且有非空 ``steps`` 数组，长度不超过 ``max_steps``；
    - 每步 ``role`` 必须是已知角色（``RoleId``）；
    - ``id`` 非空且同一份计划内唯一；
    - ``instruction`` 非空；
    - ``depends_on`` 只引用**在它之前已声明**的步骤 id（由此天然排除自依赖与环）。
    """

    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    if len(raw_steps) > max_steps:
        return None

    steps: list[PlanStep] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            return None
        step_id = str(raw.get("id") or f"s{index}").strip()
        if not step_id or step_id in seen:
            return None
        try:
            role = RoleId(str(raw.get("role") or "").strip())
        except ValueError:
            return None
        instruction = str(raw.get("instruction") or "").strip()
        if not instruction:
            return None
        raw_deps = raw.get("depends_on") or []
        if not isinstance(raw_deps, list):
            return None
        depends_on = [str(item).strip() for item in raw_deps]
        if len(set(depends_on)) != len(depends_on):
            return None
        if any(dep not in seen for dep in depends_on):
            return None
        steps.append(
            PlanStep(id=step_id, role=role, instruction=instruction, depends_on=depends_on)
        )
        seen.add(step_id)

    return DynamicPlan(
        steps=steps,
        source=PLAN_SOURCE_LLM,
        rationale=str(payload.get("rationale") or "").strip(),
    )


def fallback_plan(reason: str = "") -> DynamicPlan:
    """回退计划：与静态流水线同构的三步（收集 → 分析 → 报告）。

    回退**不是错误**，是默认可用路径：规划不可用时行为回到静态链路，
    用户仍能拿到报告，只是失去了「按任务裁剪角色」的收益。
    """

    return DynamicPlan(
        steps=[
            PlanStep(
                id="s1",
                role=RoleId.COLLECTOR,
                instruction="围绕用户任务收集、核实并整理信息与线索，输出结构化信息清单。",
            ),
            PlanStep(
                id="s2",
                role=RoleId.ANALYST,
                instruction="基于上游信息清单做归纳、对比与提炼，输出关键结论、趋势与风险。",
                depends_on=["s1"],
            ),
            PlanStep(
                id="s3",
                role=RoleId.REPORTER,
                instruction=(
                    "基于上游分析结果生成面向用户的最终报告，包含概述、关键结论、"
                    "支撑细节、风险与建议。"
                ),
                depends_on=["s2"],
            ),
        ],
        source=PLAN_SOURCE_FALLBACK,
        rationale=reason or "规划模型未返回可用计划，回退到固定三步流水线。",
    )


def planner_prompt(max_steps: int = DEFAULT_MAX_PLAN_STEPS) -> str:
    """构造规划节点的 system prompt。"""

    roles = "\n".join(
        f"- {role.value}（{get_role(role).name}）：{_role_summary(role)}"
        for role in RoleId
    )
    return (
        "你是多智能体协作平台的「任务规划 Agent」。你的唯一职责是判断这个任务需要"
        "哪些协作角色、以什么顺序参与，并输出一份协作计划。\n\n"
        f"可用角色：\n{roles}\n\n"
        "输出要求（务必严格遵守）：\n"
        "- 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性文字；\n"
        '- 结构：{"rationale": "一句话说明为什么这样安排", "steps": [...]}；\n'
        '- 每个步骤：{"id": "s1", "role": "collector", '
        '"instruction": "该步骤要做什么", "depends_on": []}；\n'
        "- instruction 写清该步骤的交付物，会被直接作为该角色的任务说明；\n"
        f"- 步骤数 1 到 {max_steps} 之间；id 唯一，建议 s1、s2…；\n"
        "- depends_on 只能引用排在它前面的步骤 id，第一个步骤必须是空数组；\n"
        "- 简单任务不要硬凑角色：只问一个事实就用一个 collector 步骤；\n"
        "- 最后一个步骤必须产出面向用户的最终交付物（通常用 reporter）。"
    )


def _role_summary(role: RoleId) -> str:
    summaries = {
        RoleId.COLLECTOR: "收集与核实信息，产出结构化信息清单",
        RoleId.ANALYST: "归纳、对比与提炼，产出结论与风险",
        RoleId.REPORTER: "整合上游结果，产出面向用户的最终报告",
    }
    return summaries[role]


def generate_plan(
    task: str,
    llm: BaseChatModel,
    max_steps: int = DEFAULT_MAX_PLAN_STEPS,
    workflow_id: str | None = None,
) -> DynamicPlan:
    """调用规划模型产出计划；解析失败或调用失败一律回退，不向上抛。"""

    started = time.perf_counter()
    log_event(logger, "dynamic.plan.start", workflow_id=workflow_id, task_chars=len(task))
    try:
        response = llm.invoke(
            [
                SystemMessage(content=planner_prompt(max_steps)),
                HumanMessage(content=f"用户任务：\n{task}"),
            ]
        )
        text = content_with_tools(response.content)
    except Exception as exc:  # 规划失败不能拖垮整次执行
        plan = fallback_plan(f"规划模型调用失败（{type(exc).__name__}），回退到固定三步流水线。")
        log_event(
            logger,
            "dynamic.plan.failed",
            level=logging.ERROR,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return plan

    plan = parse_plan(text, max_steps) or fallback_plan(
        "规划模型返回的内容不是可用计划，回退到固定三步流水线。"
    )
    log_event(
        logger,
        "dynamic.plan.finish",
        workflow_id=workflow_id,
        source=plan.source,
        steps=len(plan.steps),
        roles=[step.role.value for step in plan.steps],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return plan


# --------------------------------------------------------------------------------------
# 依赖就绪度与步骤执行
# --------------------------------------------------------------------------------------


def _is_satisfied(state: DynamicPipelineState, step_id: str) -> bool:
    """步骤是否已完成（唯一能解锁下游的状态）。"""

    outcome = state.results.get(step_id)
    return outcome is not None and outcome.status is PlanStepStatus.COMPLETED


def ready_steps(state: DynamicPipelineState) -> list[PlanStep]:
    """返回「依赖全部完成且自身未执行」的步骤，保持计划原有顺序。"""

    ready: list[PlanStep] = []
    for step in state.plan:
        if step.id in state.results:
            continue
        if all(_is_satisfied(state, dep) for dep in step.depends_on):
            ready.append(step)
    return ready


def blocked_steps(state: DynamicPipelineState) -> list[PlanStep]:
    """返回被连坐跳过的步骤：依赖里至少有一个不会完成，**含传递闭包**。

    传递性是必须的：``s3`` 依赖 ``s2`` 依赖 ``s1``，``s1`` 失败时 ``s2`` 与 ``s3``
    都永远不会执行。只标直接依赖会让 ``s3`` 一直停在 ``pending``，前端看起来像
    「还在排队」而不是「已经放弃」——这正是用户最容易误读的状态。
    """

    unreachable: set[str] = {
        step_id
        for step_id, outcome in state.results.items()
        if outcome.status is not PlanStepStatus.COMPLETED
    }
    blocked: list[PlanStep] = []
    for step in state.plan:
        if step.id in unreachable:
            continue
        if any(dep in unreachable for dep in step.depends_on):
            blocked.append(step)
            unreachable.add(step.id)
    return blocked


def step_input(task: str, step: PlanStep, results: dict[str, StepOutcome]) -> str:
    """构造步骤的角色输入：任务 + 依赖步骤的正文 + 本步职责。"""

    parts = [f"用户任务：\n{task}"]
    upstream = [
        f"【{dep} · {get_role(results[dep].role).name}】\n{results[dep].content}"
        for dep in step.depends_on
        if dep in results
    ]
    if upstream:
        parts.append("上游结果：\n" + "\n\n".join(upstream))
    parts.append(f"你这一步的职责：\n{step.instruction}")
    return "\n\n".join(parts)


def run_plan_step(
    step: PlanStep,
    task: str,
    results: dict[str, StepOutcome],
    llm: BaseChatModel,
    caller: ToolCaller | None = None,
    workflow_id: str | None = None,
    attachments: Sequence[AttachmentPayload] = (),
) -> StepOutcome:
    """执行一个计划步骤，返回结果；异常被收敛成 ``failed`` 结果而不外抛。

    失败在外抛与收敛之间的取舍：动态编排里步骤是显式依赖关系，单步失败只该
    影响它的下游，因此这里**收敛**；需要让整次执行失败的调用方读 ``status`` 自行判断。

    ``attachments`` 只注入到**根步骤**（``depends_on`` 为空，即直接拿到用户原始任务
    的那些步骤）。多根计划会各拿一份附件——这是有意的：它们彼此看不到对方的产出，
    少给任何一条根步骤，那条分支的模型就完全不知道用户传了东西。
    """

    definition = get_role(step.role)
    prompt = step_input(task, step, results)
    content = build_human_content(prompt, attachments if not step.depends_on else ())
    messages = [
        SystemMessage(content=definition.system_prompt),
        HumanMessage(content=content),
    ]
    started = time.perf_counter()
    log_event(
        logger,
        "dynamic.step.start",
        workflow_id=workflow_id,
        step=step.id,
        role=step.role.value,
        depends_on=list(step.depends_on),
    )
    try:
        with observed_stage(
            workflow_id=workflow_id,
            stage=f"dyn:{step.id}",
            role=step.role.value,
        ):
            content = invoke_role_messages(
                messages, llm, caller, stage=f"dyn:{step.id}", role=step.role
            )
    except Exception as exc:
        log_event(
            logger,
            "dynamic.step.failed",
            level=logging.ERROR,
            workflow_id=workflow_id,
            step=step.id,
            role=step.role.value,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return StepOutcome(
            step_id=step.id,
            role=step.role,
            instruction=step.instruction,
            status=PlanStepStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    records: Sequence[ToolCallRecord] = caller.records if caller is not None else ()
    outcome = StepOutcome(
        step_id=step.id,
        role=step.role,
        instruction=step.instruction,
        status=PlanStepStatus.COMPLETED,
        content=content,
        tool_calls=[record.model_dump(mode="json") for record in records],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    log_event(
        logger,
        "dynamic.step.finish",
        workflow_id=workflow_id,
        step=step.id,
        role=step.role.value,
        chars=len(content),
        tool_calls=len(outcome.tool_calls),
        duration_ms=outcome.duration_ms,
    )
    return outcome


def ordered_outcomes(state: DynamicPipelineState) -> list[StepOutcome]:
    """按计划顺序返回已完成的步骤结果。"""

    return [
        state.results[step.id]
        for step in state.plan
        if step.id in state.results
        and state.results[step.id].status is PlanStepStatus.COMPLETED
    ]


def final_output(state: DynamicPipelineState) -> str | None:
    """最终交付物：计划中最后一个成功步骤的正文。"""

    completed = ordered_outcomes(state)
    return completed[-1].content if completed else None


def finalize_state(state: DynamicPipelineState, workflow_id: str | None = None) -> DynamicPipelineState:
    """收尾：定终态、取最终交付物、汇总失败与跳过原因。"""

    output = final_output(state)
    failed = [o for o in state.results.values() if o.status is PlanStepStatus.FAILED]
    skipped = blocked_steps(state)
    problems: list[str] = []
    if failed:
        problems.append(
            "失败步骤：" + "；".join(f"{o.step_id}({o.error or '未知错误'})" for o in failed)
        )
    if skipped:
        problems.append("因上游失败而跳过：" + "、".join(step.id for step in skipped))
    if not output and not problems:
        problems.append("计划中没有任何步骤产出内容。")

    error = " ".join(problems) if problems else None
    if output is None and error is None:
        error = "没有可用的最终交付物。"
    log_event(
        logger,
        "dynamic.finish",
        level=logging.WARNING if error else logging.INFO,
        workflow_id=workflow_id,
        status=(PipelineStatus.COMPLETED if output else PipelineStatus.FAILED).value,
        steps_done=len(ordered_outcomes(state)),
        steps_total=len(state.plan),
        error=error,
    )
    return state.model_copy(
        update={
            "status": PipelineStatus.COMPLETED if output else PipelineStatus.FAILED,
            "final_output": output,
            "error": error,
            "results": {**state.results, **_skipped_outcomes(skipped, state)},
            "updated_at": _now(),
        }
    )


def _skipped_outcomes(
    skipped: Sequence[PlanStep], state: DynamicPipelineState
) -> dict[str, StepOutcome]:
    """把被连坐跳过的步骤也写进 ``results``，让前端能呈现完整计划而非缺失项。"""

    return {
        step.id: StepOutcome(
            step_id=step.id,
            role=step.role,
            instruction=step.instruction,
            status=PlanStepStatus.SKIPPED,
            error="上游步骤未成功完成，已跳过。",
        )
        for step in skipped
        if step.id not in state.results
    }


# --------------------------------------------------------------------------------------
# LangGraph 图
# --------------------------------------------------------------------------------------


def _node_planner(
    model: BaseChatModel,
    max_steps: int,
    workflow_id: str | None,
):
    def planner(state: DynamicPipelineState) -> dict[str, Any]:
        plan = generate_plan(state.task, model, max_steps, workflow_id=workflow_id)
        return {
            "plan": plan.steps,
            "plan_source": plan.source,
            "plan_rationale": plan.rationale,
            "status": PipelineStatus.RUNNING,
            "updated_at": _now(),
        }

    return planner


def _node_execute(
    model: BaseChatModel,
    registry: ToolRegistry | None,
    workflow_id: str | None,
    attachments: Sequence[AttachmentPayload] = (),
):
    def execute(state: DynamicPipelineState) -> dict[str, Any]:
        ready = ready_steps(state)
        if not ready:
            return {}
        step = ready[0]
        caller = ToolCaller(registry) if registry is not None else None
        outcome = run_plan_step(
            step, state.task, state.results, model, caller, workflow_id, attachments
        )
        return {
            "results": {**state.results, step.id: outcome},
            "updated_at": _now(),
        }

    return execute


def _node_finalize(workflow_id: str | None):
    def finalize(state: DynamicPipelineState) -> dict[str, Any]:
        settled = finalize_state(state, workflow_id)
        return {
            "status": settled.status,
            "final_output": settled.final_output,
            "error": settled.error,
            "results": settled.results,
            "updated_at": settled.updated_at,
        }

    return finalize


def _after_planner(state: DynamicPipelineState) -> str:
    if not state.plan:
        return "finalize"
    return "execute" if ready_steps(state) else "finalize"


def _after_execute(state: DynamicPipelineState) -> str:
    # 还有就绪步骤就继续跑；只剩「被连坐」的步骤时收尾（它们会在 finalize 里记为 skipped）。
    return "execute" if ready_steps(state) else "finalize"


def recursion_limit(max_steps: int) -> int:
    """按步骤数估算 LangGraph 递归上限：planner + 每步 2 个超级步 + finalize，留一倍余量。"""

    return max(25, 4 * max_steps + 10)


def build_dynamic_pipeline(
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
    max_steps: int = DEFAULT_MAX_PLAN_STEPS,
    workflow_id: str | None = None,
    attachments: Sequence[AttachmentPayload] = (),
):
    """构建动态协作图：``planner → execute（循环）→ finalize``。

    ``tool_registry`` 为 None 时使用 ``default_tool_registry()``；解析不到注册表
    （例如无 MCP 实现）时步骤不调用工具，与静态图口径一致。

    ``attachments`` 只在进程内直跑时使用；Dapr 链路按 id 从库里取（ADR-021）。
    """

    model = llm or build_chat_model(settings or get_settings())
    registry = tool_registry if tool_registry is not None else default_tool_registry()

    builder = StateGraph(DynamicPipelineState)
    builder.add_node("planner", _node_planner(model, max_steps, workflow_id))
    builder.add_node(
        "execute", _node_execute(model, registry, workflow_id, attachments)
    )
    builder.add_node("finalize", _node_finalize(workflow_id))

    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner", _after_planner, {"execute": "execute", "finalize": "finalize"}
    )
    builder.add_conditional_edges(
        "execute", _after_execute, {"execute": "execute", "finalize": "finalize"}
    )
    builder.add_edge("finalize", END)
    return builder.compile()


def run_dynamic_pipeline(
    task: str,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
    max_steps: int = DEFAULT_MAX_PLAN_STEPS,
    workflow_id: str | None = None,
    attachments: Sequence[AttachmentPayload] = (),
) -> DynamicPipelineState:
    """用完整的动态图执行一次协作，返回终态 ``DynamicPipelineState``。"""

    graph = build_dynamic_pipeline(
        llm=llm,
        settings=settings,
        tool_registry=tool_registry,
        max_steps=max_steps,
        workflow_id=workflow_id,
        attachments=attachments,
    )
    output = graph.invoke(
        DynamicPipelineState(task=task),
        config={"recursion_limit": recursion_limit(max_steps)},
    )
    return DynamicPipelineState.model_validate(output)


def dynamic_checkpoint_summary(state: DynamicPipelineState) -> dict[str, Any]:
    """落库到 ``workflow_runs.checkpoint`` 的摘要；字段与静态摘要有交集，便于前端复用。"""

    return {
        "mode": "dynamic",
        "status": state.status.value,
        "plan_source": state.plan_source,
        "current_step": None,
        "completed_steps": [outcome.step_id for outcome in ordered_outcomes(state)],
        "plan": [
            {
                "id": step.id,
                "role": step.role.value,
                "depends_on": list(step.depends_on),
                "status": (
                    state.results[step.id].status.value
                    if step.id in state.results
                    else PlanStepStatus.PENDING.value
                ),
            }
            for step in state.plan
        ],
        "updated_at": state.updated_at.isoformat(),
    }


def resolve_orchestration_mode(settings: AgentSettings | None = None) -> str:
    """解析生效的编排模式；非法值回退 ``static``（宁可固定流程，不要跑不起来）。"""

    mode = (settings or get_settings()).orchestration_mode
    return mode if mode in ORCHESTRATION_MODES else "static"


def resolve_max_plan_steps(settings: AgentSettings | None = None) -> int:
    value = (settings or get_settings()).max_plan_steps
    return value if value > 0 else DEFAULT_MAX_PLAN_STEPS

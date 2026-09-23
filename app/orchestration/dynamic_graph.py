"""动态编排图（自动编排）：意图 → 编排 → 波次并行 Worker → 合成 → 校验。

与 ``app.orchestration.pipeline_graph`` 的固定三步图**并列**存在，互不影响：

- 静态图：``collector → analyst → reporter``，拓扑与角色分配写死在
  ``PIPELINE_ROLE_ASSIGNMENT``；
- 本模块：``intake →（单 Agent 直答 | planner → 波内并行 worker → 汇聚循环 → 合成 →
  校验）→ finalize``，角色、顺序与并行度都由运行期产出。

设计要点（决策记录见 ADR-019 与 ADR-038，说明文档 `doc/orchestration.md`）：

1. **另写一套状态，不放宽静态状态机**。``PipelineState`` 的 ``current_step`` 是固定
   三元组 ``PipelineStage``，``complete_step`` 强校验顺序；动态路径若复用它会要求放宽
   这套校验，伤及静态链路的既有保证（含 Dapr 侧按阶段拆子 Workflow 的可恢复性）。
   因此本模块自带 ``DynamicPipelineState``，静态契约一个字节都不动。
2. **意图判定 + 护栏**。``intake`` 一次调用同时产出改写文本与结构化意图；只有意图
   解析成功且 ``need_multi_subtask=False`` 才走单 Agent 直答（固定 1 步），其余一律
   按多 Agent 处理。解析失败、调用失败都只是**降级**，不是失败。
3. **规划失败必须能降级，不能失败**。规划节点拿到的是一段自由文本，模型可能返回
   Markdown 围栏、解释性前后缀或非法角色；``parse_plan`` 逐条校验，任何一处不合法就
   整份丢弃并回退到 ``fallback_plan()``（固定三步）。**不猜、不修补半份计划**——
   修补出来的计划比固定三步更不可预期。
4. **按波并行、波间串行**。``waves()`` / ``pending_batch()`` 由 ``depends_on`` 算层级，
   同一波内无依赖的步骤一起分派（LangGraph 用 ``Send``，Dapr 用 ``when_all``），
   ``max_parallel_workers`` 是并发上限；波次判定只有这一份实现，两条链路共用。
5. **失败只连坐下游，且要说得出来**。某步失败只把依赖它的步骤记为 ``skipped``，
   与之无关的步骤照常执行；合成器拿到失败清单，必须在交付物开头写明哪些子任务没跑成。
   存在失败/跳过时终态仍是 ``completed``，但 ``partial=True``。
6. **合成之后可校验**。校验器只在多 Agent 路径跑，不达标且还有轮次预算时，带缺陷
   清单重编排一轮（最多一轮）；校验器自己坏了视为通过，绝不因此推翻一份可用交付物。

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
from typing import Annotated, Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from app.agents.roles import RoleId, get_role
from app.attachments import AttachmentPayload, build_human_content
from app.config import AgentSettings, get_settings
from app.memory import SessionMessage
from app.observability.instrumentation import observed_stage
from app.observability.logging import get_logger, log_event
from app.orchestration.context import conversation_block
from app.orchestration.intake import (
    INTENT_SOURCE_FALLBACK,
    IntentResult,
    intake_task,
    intent_block,
)
from app.orchestration.llm import build_chat_model
from app.orchestration.pipeline import PipelineStatus
from app.orchestration.pipeline_graph import (
    content_with_tools,
    invoke_role_messages,
    invoke_role_messages_with_usage,
    usage_tokens,
)
from app.orchestration.synthesis import (
    SYNTHESIZE_NODE_ID,
    VALIDATE_NODE_ID,
    SynthesisOutcome,
    ValidationResult,
    run_synthesis,
    run_validation,
)
from app.orchestration.tools import ToolCaller, ToolCallRecord, ToolRegistry, default_tool_registry

ORCHESTRATION_MODES: tuple[str, ...] = ("static", "dynamic")
"""编排模式取值，对齐 ``AgentSettings.orchestration_mode``。"""

DEFAULT_MAX_PLAN_STEPS = 6
"""一次执行允许的最大步骤数。既是成本上限，也是回退判据：超出的计划整份丢弃。"""

PLAN_SOURCE_LLM = "llm"
PLAN_SOURCE_FALLBACK = "fallback"

ROUTE_SINGLE = "single"
"""单 Agent 直答：意图判定「不需要多 Agent」，编排器与并行分支整段跳过。"""

ROUTE_MULTI = "multi"
"""多 Agent 波次协作：编排器出 DAG，波内并行，最后合成。"""

SINGLE_AGENT_ROLE = RoleId.REPORTER
"""单 Agent 直答用的角色。

选 reporter 而不是 collector：用户拿到的必须是**交付物**，而不是一份"待分析的信息清单"
（collector 与 analyst 的 prompt 都明确把成稿交给下游）。单 Agent 路径会在输入里显式标注
「本次为单 Agent 直答，上游没有分析结果」，避免角色按固定三步的位置感去等上游。
"""

DEFAULT_SUBTASK_MAX_ATTEMPTS = 3
"""单个子任务默认的最大尝试次数（含首次）；`AGENT_SUBTASK_MAX_ATTEMPTS` 的兜底值。"""

DEFAULT_SUBTASK_TIMEOUT_SECONDS = 300.0
"""单个子任务模型调用的默认超时（秒）；`AGENT_SUBTASK_TIMEOUT_SECONDS` 的兜底值。"""

DEFAULT_MAX_PARALLEL_WORKERS = 3
"""同波并发上限的兜底值（`AGENT_MAX_PARALLEL_WORKERS`）。"""

DEFAULT_MAX_PLAN_ROUNDS = 1
"""校验不达标时允许的重编排轮数上限；硬上限 1。"""

PLAN_RETRY_LIMIT = 3
"""计划里单步 `retry`（重试次数，不含首次）允许的最大值；越界整份计划丢弃。"""

PLAN_TIMEOUT_MIN_SECONDS = 30
PLAN_TIMEOUT_MAX_SECONDS = 900
"""计划里单步 `timeout_seconds` 的允许区间；越界整份计划丢弃。"""

INTENT_SOURCE_MULTI = INTENT_SOURCE_FALLBACK
"""意图不可用时的口径复用 intake 的 fallback 常量，避免两套词。"""

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
    expected_output: str = Field(
        default="",
        description="期望的输出形态（格式/长度/交付物），拼进该步的输入；空表示不额外要求",
    )
    retry: int | None = Field(
        default=None,
        description=(
            f"该步失败后允许的重试次数（0–{PLAN_RETRY_LIMIT}，不含首次）；"
            "None 表示用服务端配置的最大尝试次数"
        ),
    )
    timeout_seconds: int | None = Field(
        default=None,
        description=(
            f"该步模型调用超时（{PLAN_TIMEOUT_MIN_SECONDS}–{PLAN_TIMEOUT_MAX_SECONDS} 秒）；"
            "None 表示用服务端配置"
        ),
    )


class FlowKind(StrEnum):
    """流程序列里的节点类型（前端画布按它区分展示，见 `doc/api.md` §5.17/§5.18）。"""

    INTENT = "intent"
    PLAN = "plan"
    WORKER = "worker"
    SYNTHESIZE = "synthesize"
    VALIDATE = "validate"


class FlowNode(BaseModel):
    """流程序列的一个节点：把「意图 → 编排 → 并行 Worker → 合成 → 校验」画出来。

    与 ``PlanStep`` 的分工：``plan`` 仍然只装**子任务**（worker），``flow`` 是给人看的
    整条流程（含平台节点）。两者分开是为了让既有消费方（画布按 `plan[]` 推导波次、
    `/stages` 按子任务读轨迹）不必重新理解 `plan` 的语义。
    """

    id: str = Field(min_length=1)
    kind: FlowKind
    label: str = ""
    role: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: str = PlanStepStatus.PENDING.value
    wave: int = 0


INTENT_NODE_ID = "intent"
PLAN_NODE_ID = "plan"


def flow_node_label(kind: FlowKind, role: RoleId | None = None) -> str:
    """节点展示名；worker 用角色名，平台节点用固定名。"""

    if kind is FlowKind.WORKER and role is not None:
        return get_role(role).name
    return {
        FlowKind.INTENT: "意图识别",
        FlowKind.PLAN: "编排器",
        FlowKind.SYNTHESIZE: "合成器",
        FlowKind.VALIDATE: "结果校验",
    }[kind]


def merge_results(
    left: dict[str, StepOutcome] | None,
    right: dict[str, StepOutcome] | None,
) -> dict[str, StepOutcome]:
    """按步骤键合并两份结果（LangGraph 并行分支的 reducer）。

    并行分支只返回自己那一步的增量，因此合并而不是替换才是正确语义。
    Dapr 链路不用这个 reducer（父工作流单点写），但两条路径共用同一个状态类型。
    """

    merged = dict(left or {})
    merged.update(right or {})
    return merged


class DynamicPlan(BaseModel):
    """一次执行的协作计划。"""

    steps: list[PlanStep] = Field(default_factory=list)
    source: Literal["llm", "fallback"] = PLAN_SOURCE_FALLBACK
    rationale: str = ""
    """规划理由；回退时说明回退原因，便于前端与日志区分「真的规划过」与「降级了」。"""
    tokens: int = 0
    """这次规划调用消耗的 Token（累计预算要算进去；取不到用量时为 0）。"""


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
    attempts: int = 1
    """**实际**尝试次数（含首次）。与计划里的 `retry`（配置的重试次数）分开记：
    「配了 2 次重试」和「真的重试了 2 次」是两件事，审计要的是后者。"""

    tokens: int = 0
    """这一步（含所有重试尝试）消耗的 Token 总数；取不到用量时为 0。"""


class DynamicPipelineState(BaseModel):
    """动态编排的可序列化状态。

    与 ``PipelineState`` 的差异：没有 ``current_step`` 单值指针，改为用
    ``results`` 里已有的步骤反推「下一步能跑谁」——依赖关系才是这个图的第一公民。
    """

    task: str = Field(min_length=1)
    # 问题改写（ADR-037）：与静态链路同口径的两个可选字段，随 checkpoint 落库。
    rewritten_task: str | None = None
    rewrite_source: str | None = None
    # 意图识别（ADR-038）：与改写同一次调用产出，缺失即按「需要多 Agent」处理。
    intent: IntentResult | None = None
    intent_source: str | None = None
    route: Literal["single", "multi"] = ROUTE_MULTI
    """本次走的路径：`single` = 单 Agent 直答（跳过编排与并行），`multi` = 波次协作。"""

    round: int = 1
    """当前轮次。校验不达标才会 +1（最多到 2），执行实例 ID 按轮次命名空间区分。"""

    status: PipelineStatus = PipelineStatus.PENDING
    plan: list[PlanStep] = Field(default_factory=list)
    plan_source: str = PLAN_SOURCE_FALLBACK
    plan_rationale: str = ""
    results: Annotated[dict[str, StepOutcome], merge_results] = Field(default_factory=dict)
    """步骤结果。**分流处必须是按键合并**：并行分支各自返回增量，
    整份替换会让后写的覆盖先写的（ADR-038 §6 的并行前置条件）。"""

    flow: list[FlowNode] = Field(default_factory=list)
    validation: ValidationResult | None = None
    synthesis_attempted: bool = False
    """合成节点是否跑过。用来区分「还没到合成」与「合成了但没产出」——
    前者不该报错，后者必须在收尾时说出来，否则用户会以为拿到的就是完整结论。"""

    validation_rounds: int = 0
    """已发生的重编排次数（0 或 1）。"""

    defects: list[str] = Field(default_factory=list)
    """上一轮校验给出的缺陷清单；只有重编排时非空，会带进规划提示词。"""

    partial: bool = False
    """是否存在失败/被跳过的子任务：终态仍是 completed，但结果是**部分**的。"""

    tokens_used_prior: int = 0
    """本轮之前已经花掉的 Token（重编排时由上一轮带入），让预算跨轮次连续。"""

    platform_tokens: int = 0
    """平台节点（intake / 规划 / 校验）消耗的 Token：它们不在 `results` 里。"""

    synthesis_tokens: int = 0
    """合成器消耗的 Token。"""

    token_budget: int = 0
    """本次执行的累计 Token 预算上限；0 表示不限制（默认）。"""

    budget_exceeded: bool = False
    """是否因为达到 Token 预算而提前停止派发剩余子任务。"""

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


def _parse_bounded_int(raw: Any, low: int, high: int) -> tuple[bool, int | None]:
    """解析计划里的可选整数；返回 `(是否合法, 值)`。

    缺省（`None` / 空串）合法且值为 `None`（表示用服务端配置）；给了但不在界内、
    或类型不对（含 JSON 布尔，Python 里 `bool` 是 `int` 的子类）一律不合法。
    """

    if raw is None or raw == "":
        return True, None
    if isinstance(raw, bool):
        return False, None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        value = int(raw.strip())
    else:
        return False, None
    return (low <= value <= high), value


def parse_plan(text: str, max_steps: int = DEFAULT_MAX_PLAN_STEPS) -> DynamicPlan | None:
    """把规划模型的自由文本解析成计划；任一处不合法返回 ``None``。

    校验口径（全部通过才接受）：

    - 顶层是对象且有非空 ``steps`` 数组，长度不超过 ``max_steps``；
    - 每步 ``role`` 必须是已知角色（``RoleId``）；
    - ``id`` 非空且同一份计划内唯一；
    - ``instruction`` 非空；
    - ``depends_on`` 只引用**在它之前已声明**的步骤 id（由此天然排除自依赖与环）；
    - 可选的 ``retry`` / ``timeout_seconds`` / ``expected_output`` 若给了就必须在界内
      （越界同样整份丢弃——「不修补半份计划」是刻意的，见 ADR-019 §2）。
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
        expected_output = str(raw.get("expected_output") or "").strip()
        ok, retry = _parse_bounded_int(raw.get("retry"), 0, PLAN_RETRY_LIMIT)
        if not ok:
            return None
        ok, timeout_seconds = _parse_bounded_int(
            raw.get("timeout_seconds"), PLAN_TIMEOUT_MIN_SECONDS, PLAN_TIMEOUT_MAX_SECONDS
        )
        if not ok:
            return None
        steps.append(
            PlanStep(
                id=step_id,
                role=role,
                instruction=instruction,
                depends_on=depends_on,
                expected_output=expected_output,
                retry=retry,
                timeout_seconds=timeout_seconds,
            )
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
        "你拿到的用户任务是平台按会话上下文**改写过的**（补全了指代、目标与期望交付物），"
        "可以直接当作完整任务来规划。\n\n"
        f"可用角色：\n{roles}\n\n"
        "输出要求（务必严格遵守）：\n"
        "- 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性文字；\n"
        '- 结构：{"rationale": "一句话说明为什么这样安排", "steps": [...]}；\n'
        '- 每个步骤：{"id": "s1", "role": "collector", '
        '"instruction": "该步骤要做什么", "depends_on": [], '
        '"expected_output": "这一步的交付物形态", "retry": 2, "timeout_seconds": 300}；\n'
        "- expected_output 写清这一步要交出什么（格式/长度/字段），会拼进该步的输入；\n"
        f"- retry 是该步失败后的重试次数（0–{PLAN_RETRY_LIMIT}），可选，省略即用服务端默认；\n"
        "- timeout_seconds 是该步模型调用的超时秒数"
        f"（{PLAN_TIMEOUT_MIN_SECONDS}–{PLAN_TIMEOUT_MAX_SECONDS}），可选，省略即用服务端默认；\n"
        "- instruction 写清该步骤的交付物，会被**直接当作该角色的任务说明**；"
        "角色手上只有会话工作区文件工具（读；可写档位下另有写与移动工具，覆盖/删除需人工审批）、"
        "本轮附件、按需发现的 MCP 工具与网页搜索——不要在 instruction 里要求它们做做不到的事；\n"
        f"- 步骤数 1 到 {max_steps} 之间；id 唯一，建议 s1、s2…；\n"
        "- depends_on 只能引用排在它前面的步骤 id，第一个步骤必须是空数组；\n"
        "- **无依赖的步骤会在同一波并行执行**：让彼此独立的工作互相不依赖（例如两路检索），"
        "有前后关系的才写 depends_on；不要把本来能并行的步骤人为串成一条链；\n"
        "- 简单任务不要硬凑角色：只问一个事实就用一个 collector 步骤；\n"
        "- 最后一个步骤通常用 reporter 写成完整成稿，但**面向使用者的最终交付物由平台的合成器产出**"
        "（其后还有一次校验）：子任务的产出写成「给下游/合成器用的高质量素材或成稿」即可，"
        "不必各自重复整篇汇报的口吻，也不要把自己当成唯一对使用者说话的人。"
    )


def _role_summary(role: RoleId) -> str:
    summaries = {
        RoleId.COLLECTOR: "收集与核实信息，产出结构化信息清单（可以与别的子任务并行）",
        RoleId.ANALYST: "归纳、对比与提炼，产出结论与风险",
        RoleId.REPORTER: "整合上游结果写成完整成稿（面向使用者的最终交付物由平台的合成器产出）",
    }
    return summaries[role]


def generate_plan(
    task: str,
    llm: BaseChatModel,
    max_steps: int = DEFAULT_MAX_PLAN_STEPS,
    workflow_id: str | None = None,
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
    intent: IntentResult | None = None,
    defects: Sequence[str] = (),
) -> DynamicPlan:
    """调用规划模型产出计划；解析失败或调用失败一律回退，不向上抛。

    `history` 是会话记忆里最近若干条（不含本轮），按 ADR-019 的口径渲染成提示词前缀——
    **规划也要看得到上下文**：只说「重试」时，规划节点否则连要重试什么都没法判断。

    `intent` 是 intake 节点的结构化意图（可能为空）；`defects` 是上一轮校验给出的缺陷
    清单（只有重编排时非空），带进提示词才有「按缺陷重来」可言。
    """

    def user_input() -> str:
        parts = [f"{preferences}{conversation_block(history)}{intent_block(intent)}"]
        parts.append(f"用户任务：\n{task}")
        if defects:
            lines = "\n".join(f"- {item}" for item in defects)
            parts.append(
                "【上一轮交付物的问题】（这是**重编排**：请针对这些问题调整子任务安排，"
                "而不是把上一轮原样再跑一遍）\n" + lines
            )
        return "\n\n".join(part for part in parts if part)

    started = time.perf_counter()
    log_event(logger, "dynamic.plan.start", workflow_id=workflow_id, task_chars=len(task))
    try:
        response = llm.invoke(
            [
                SystemMessage(content=planner_prompt(max_steps)),
                HumanMessage(content=user_input()),
            ]
        )
        text = content_with_tools(response.content)
        plan_tokens = usage_tokens(response)
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
    plan = plan.model_copy(update={"tokens": plan_tokens})
    log_event(
        logger,
        "dynamic.plan.finish",
        workflow_id=workflow_id,
        source=plan.source,
        steps=len(plan.steps),
        roles=[step.role.value for step in plan.steps],
        defects=len(defects),
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


def step_levels(plan: Sequence[PlanStep]) -> dict[str, int]:
    """按依赖算层级：没有依赖的是第 0 波，其余是「所有依赖里最深的 +1」。

    与前端 `collaboration.ts::levelOf()` 同一口径——「并行与串行是算出来的、不是画死的」
    这件事在两处必须是同一个答案，否则画布上的并行框与实际调度会对不上。
    """

    return _levels_for([(step.id, tuple(step.depends_on)) for step in plan])


def waves(plan: Sequence[PlanStep]) -> list[list[PlanStep]]:
    """把计划按层级分组：同一波内部可以并行，波与波之间是串行关系。

    组内保持计划的声明顺序（模型给的顺序就是它对"先做哪件"的意图）。
    空计划返回空列表——调用方据此直接进合成/收尾。
    """

    if not plan:
        return []
    levels = step_levels(plan)
    grouped: list[list[PlanStep]] = []
    for step in plan:
        level = levels.get(step.id, 0)
        while len(grouped) <= level:
            grouped.append([])
        grouped[level].append(step)
    return grouped


def pending_batch(
    state: DynamicPipelineState,
    plan: Sequence[PlanStep],
    max_parallel: int,
) -> list[PlanStep]:
    """下一批可执行的步骤：还没结果、依赖都已解析、且层级最低的那一波。

    取「层级最低」而不是「计划里最靠前」是并行的关键：同一波里彼此无依赖的步骤必须
    一起放行。`max_parallel` 是成本闸门——同波超过上限时按计划顺序切成多批，
    批与批之间仍然串行（不会偷偷绕开上限）。
    """

    done = set(state.results)
    levels = step_levels(plan)
    candidates = [
        step
        for step in plan
        if step.id not in done
        # 依赖必须**完成**才算就绪；失败/跳过的依赖会让这一步被连坐跳过，
        # 而不是让它带着一个坏输入继续跑（ADR-019 §3）。
        and all(_is_satisfied(state, dep) for dep in step.depends_on)
    ]
    if not candidates:
        return []
    lowest = min(levels.get(step.id, 0) for step in candidates)
    batch = [step for step in candidates if levels.get(step.id, 0) == lowest]
    return batch[: max(1, max_parallel)]


def single_agent_plan(task: str, intent: IntentResult | None = None) -> DynamicPlan:
    """单 Agent 直答的计划：固定 1 步，角色固定为 reporter（见 `SINGLE_AGENT_ROLE`）。

    护栏（ADR-038 §2）：意图判定简单**也不等于**可以省掉交付物要求，因此这里把
    「单 Agent 直答」显式写进 instruction，把用户的格式约束写进 expected_output。
    """

    constraints = "；".join(intent.constraints) if intent and intent.constraints else ""
    instruction = (
        "本次是**单 Agent 直答**：平台判定这个任务不需要多 Agent 并行协作，因此没有"
        "上游的分析结论，也不会再有合成步骤——你的输出就是最终交付物。"
        "请直接依据用户任务作答，该给结论就给结论、该给代码就给代码。"
    )
    if constraints:
        instruction += f"\n用户明确提出的约束：{constraints}。"
    return DynamicPlan(
        steps=[
            PlanStep(
                id="s1",
                role=SINGLE_AGENT_ROLE,
                instruction=instruction,
                expected_output=constraints,
            )
        ],
        source=PLAN_SOURCE_LLM,
        rationale="意图识别判定为单点任务，直接路由到单 Agent 直答（ADR-038 §2）。",
    )


def effective_attempts(step: PlanStep, settings: AgentSettings | None = None) -> int:
    """该步的最大尝试次数（含首次）：计划里的 `retry` 是**重试次数**。"""

    if step.retry is not None:
        return max(1, step.retry + 1)
    configured = (settings or get_settings()).subtask_max_attempts
    return max(1, configured if configured > 0 else DEFAULT_SUBTASK_MAX_ATTEMPTS)


def effective_timeout(step: PlanStep, settings: AgentSettings | None = None) -> float:
    """该步的模型调用超时（秒）。"""

    if step.timeout_seconds is not None:
        return float(step.timeout_seconds)
    configured = (settings or get_settings()).subtask_timeout_seconds
    return float(configured if configured > 0 else DEFAULT_SUBTASK_TIMEOUT_SECONDS)


def resolve_max_parallel_workers(settings: AgentSettings | None = None) -> int:
    """同波并发上限；非正数退回默认值（不允许「无上限并行」）。"""

    value = (settings or get_settings()).max_parallel_workers
    return value if value > 0 else DEFAULT_MAX_PARALLEL_WORKERS


def resolve_max_plan_rounds(settings: AgentSettings | None = None) -> int:
    """允许的重编排轮数；硬上限 1（再多就是烧钱循环）。"""

    value = (settings or get_settings()).max_plan_rounds
    return min(max(value, 0), 1)


def resolve_token_budget(settings: AgentSettings | None = None) -> int:
    """本次执行的累计 Token 预算上限；非正数表示不限制（默认）。"""

    value = (settings or get_settings()).token_budget
    return value if value > 0 else 0


def tokens_used(state: DynamicPipelineState) -> int:
    """本次执行**已消耗**的 Token 累计：平台节点 + 子任务（含重试）+ 合成 + 之前轮次。

    口径是**下限**：模型没回用量时按 0 计（不按字数估算）。因此预算是「花到就停」的
    软闸门，不是硬计费——这条写在 `doc/orchestration.md` 与 ADR-038 里。
    """

    step_tokens = sum(outcome.tokens for outcome in state.results.values())
    return (
        state.tokens_used_prior
        + state.platform_tokens
        + step_tokens
        + state.synthesis_tokens
    )


def budget_exhausted(state: DynamicPipelineState) -> bool:
    """预算是否已经用尽（未配置预算时永远为 False）。"""

    if state.token_budget <= 0:
        return False
    return tokens_used(state) >= state.token_budget


def apply_budget_stop(state: DynamicPipelineState) -> dict[str, StepOutcome]:
    """预算用尽时，把还没跑的子任务标成 `skipped` 并写明原因。

    与「连坐跳过」共用一个状态词（`skipped`），但原因不同：一个是上游失败，
    一个是成本闸门。原因写进 `error`，合成器与画布都会把它带给使用者——
    预算用尽时最不该发生的事，是把「少做的部分」悄悄瞒下来。
    """

    if not budget_exhausted(state):
        return {}
    return {
        step.id: StepOutcome(
            step_id=step.id,
            role=step.role,
            instruction=step.instruction,
            status=PlanStepStatus.SKIPPED,
            error=BUDGET_SKIP_REASON,
        )
        for step in state.plan
        if step.id not in state.results
    }


BUDGET_SKIP_REASON = "本次执行的 Token 预算已用尽，该子任务未执行。"


def step_input(
    task: str,
    step: PlanStep,
    results: dict[str, StepOutcome],
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
) -> str:
    """构造步骤的角色输入：长期记忆 + 会话历史 + 任务 + 依赖步骤的正文 + 本步职责。

    历史放在最前面，与固定三步链路（`pipeline_graph._role_input`）同一形态——两条编排
    对模型呈现的上下文必须一致，否则「同一个会话在两种模式下记忆表现不同」。
    长期记忆（跨会话偏好）再排在历史之前：约束在前、上下文在后（ADR-036）。
    """

    parts = [f"{preferences}{conversation_block(history)}用户任务：\n{task}"]
    upstream = [
        f"【{dep} · {get_role(results[dep].role).name}】\n{results[dep].content}"
        for dep in step.depends_on
        if dep in results
    ]
    if upstream:
        parts.append("上游结果：\n" + "\n\n".join(upstream))
    parts.append(f"你这一步的职责：\n{step.instruction}")
    if step.expected_output:
        parts.append(f"这一步期望的输出形态：\n{step.expected_output}")
    return "\n\n".join(parts)


def run_plan_step(
    step: PlanStep,
    task: str,
    results: dict[str, StepOutcome],
    llm: BaseChatModel | None = None,
    caller: ToolCaller | None = None,
    workflow_id: str | None = None,
    attachments: Sequence[AttachmentPayload] = (),
    history: Sequence[SessionMessage] = (),
    preferences: str = "",
    settings: AgentSettings | None = None,
    timeout_seconds: float | None = None,
) -> StepOutcome:
    """执行一个计划步骤，返回结果；异常被收敛成 ``failed`` 结果而不外抛。

    ``llm`` 为 None 时按该步角色解析配置并建模型（`timeout_seconds` 落到模型客户端）；
    传入模型时（单测 / 进程内直跑）超时由调用方负责。

    失败在外抛与收敛之间的取舍：动态编排里步骤是显式依赖关系，单步失败只该
    影响它的下游，因此这里**收敛**；需要让整次执行失败的调用方读 ``status`` 自行判断。

    ``attachments`` 只注入到**根步骤**（``depends_on`` 为空，即直接拿到用户原始任务
    的那些步骤）。多根计划会各拿一份附件——这是有意的：它们彼此看不到对方的产出，
    少给任何一条根步骤，那条分支的模型就完全不知道用户传了东西。
    """

    definition = get_role(step.role)
    prompt = step_input(task, step, results, history, preferences)
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
        model = llm
        if model is None:
            from app.core.agent_config import resolve_agent_settings

            resolved = (
                settings
                if settings is not None
                else resolve_agent_settings(step.role.value)
            )
            model = build_chat_model(resolved, timeout_seconds=timeout_seconds)
        with observed_stage(
            workflow_id=workflow_id,
            stage=f"dyn:{step.id}",
            role=step.role.value,
        ):
            content, tokens = invoke_role_messages_with_usage(
                messages, model, caller, stage=f"dyn:{step.id}", role=step.role
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
        tokens=tokens,
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
    """回退交付物：计划中最后一个成功步骤的正文。

    多 Agent 路径正常以合成器输出为准；这个函数覆盖两种兜底场景——单 Agent 直答
    （本来就没有合成步骤），以及「合成器失败但子任务有产出」（宁可给一份素材，
    也不要因为最后一步坏了就把前面的成果全丢掉）。
    """

    completed = ordered_outcomes(state)
    return completed[-1].content if completed else None


def failed_steps(state: DynamicPipelineState) -> list[PlanStep]:
    """按计划顺序返回失败的步骤。"""

    return [
        step
        for step in state.plan
        if step.id in state.results
        and state.results[step.id].status is PlanStepStatus.FAILED
    ]


def failure_triples(
    state: DynamicPipelineState,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """返回 `(失败清单, 跳过清单)` 的 `(id, 角色, 原因)` 三元组，按计划顺序。

    合成器与校验器都要吃这份清单（"哪些子任务没跑成"必须一路传到最终交付物），
    所以口径只写一份。

    注意两种「跳过」都要算：**已经标进 results 的**（波次汇聚时连坐判定写进去的）
    与**还没落库但已被连坐的**（收尾前直接问 `blocked_steps`）。只看后者会让
    「失败清单」在真正交付的那一刻凭空少一项——那正是本需求要防的那种静默。
    """

    failed: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for step in state.plan:
        outcome = state.results.get(step.id)
        if outcome is None:
            continue
        if outcome.status is PlanStepStatus.FAILED:
            failed[step.id] = outcome.error or "未知错误"
        elif outcome.status is PlanStepStatus.SKIPPED:
            skipped[step.id] = outcome.error or DEFAULT_SKIP_REASON
    for step in blocked_steps(state):
        skipped.setdefault(step.id, DEFAULT_SKIP_REASON)
    roles = {step.id: step.role.value for step in state.plan}
    return (
        [(step_id, roles[step_id], error) for step_id, error in failed.items()],
        [(step_id, roles[step_id], error) for step_id, error in skipped.items()],
    )


DEFAULT_SKIP_REASON = "上游步骤未成功完成，已跳过。"


def _levels_for(nodes: Sequence[tuple[str, Sequence[str]]]) -> dict[str, int]:
    """通用层级计算：`(id, 依赖)` 列表 → 每个 id 的波次。

    成环等坏数据不让递归拖死（真实数据不会成环，但画布与调度不能因为一份坏数据白屏）。
    """

    ids = {node_id for node_id, _ in nodes}
    deps_of = {node_id: [dep for dep in deps if dep in ids] for node_id, deps in nodes}
    levels: dict[str, int] = {}

    def resolve(node_id: str, seen: set[str]) -> int:
        known = levels.get(node_id)
        if known is not None:
            return known
        if node_id in seen:
            return 0
        seen.add(node_id)
        level = max((resolve(dep, seen) for dep in deps_of[node_id]), default=-1) + 1
        levels[node_id] = level
        return level

    for node_id in deps_of:
        resolve(node_id, set())
    return levels


def build_flow(state: DynamicPipelineState) -> list[FlowNode]:
    """由当前状态推导整条流程：意图 →（编排）→ Worker 波次 →（合成）→（校验）。

    单 Agent 直答没有编排、合成与校验三个平台节点——它本来就是为了省掉这些才存在的。
    状态取值口径：

    - `intent`：解析出意图才是 `completed`，退化成 fallback 记 `skipped`（原因看
      checkpoint 的 `intent_source`）；
    - `worker`：直接取该步结果状态，没有结果即 `pending`（并行的其它分支可能还在跑）；
    - `synthesize`：合成成功 `completed`、失败 `failed`；
    - `validate`：通过 `completed`、不通过 `failed`、校验器不可用 `skipped`。
    """

    nodes: list[FlowNode] = [
        FlowNode(
            id=INTENT_NODE_ID,
            kind=FlowKind.INTENT,
            label=flow_node_label(FlowKind.INTENT),
            status=(
                PlanStepStatus.COMPLETED.value
                if state.intent is not None
                else PlanStepStatus.SKIPPED.value
            ),
        )
    ]
    deps_of: list[tuple[str, Sequence[str]]] = [(INTENT_NODE_ID, ())]
    if state.route == ROUTE_MULTI:
        nodes.append(
            FlowNode(
                id=PLAN_NODE_ID,
                kind=FlowKind.PLAN,
                label=flow_node_label(FlowKind.PLAN),
                depends_on=[INTENT_NODE_ID],
                status=PlanStepStatus.COMPLETED.value,
            )
        )
        deps_of.append((PLAN_NODE_ID, (INTENT_NODE_ID,)))

    worker_parent = PLAN_NODE_ID if state.route == ROUTE_MULTI else INTENT_NODE_ID
    for step in state.plan:
        outcome = state.results.get(step.id)
        nodes.append(
            FlowNode(
                id=step.id,
                kind=FlowKind.WORKER,
                label=flow_node_label(FlowKind.WORKER, step.role),
                role=step.role.value,
                depends_on=[worker_parent, *step.depends_on],
                status=(
                    outcome.status.value
                    if outcome is not None
                    else PlanStepStatus.PENDING.value
                ),
            )
        )
        deps_of.append((step.id, (worker_parent, *step.depends_on)))

    if state.route == ROUTE_MULTI:
        worker_ids = [step.id for step in state.plan]
        if state.synthesis_attempted:
            synthesize_status = (
                PlanStepStatus.COMPLETED.value
                if state.final_output
                else PlanStepStatus.FAILED.value
            )
        else:
            synthesize_status = PlanStepStatus.PENDING.value
        nodes.append(
            FlowNode(
                id=SYNTHESIZE_NODE_ID,
                kind=FlowKind.SYNTHESIZE,
                label=flow_node_label(FlowKind.SYNTHESIZE),
                role=RoleId.REPORTER.value,
                depends_on=worker_ids or [PLAN_NODE_ID],
                status=synthesize_status,
            )
        )
        deps_of.append((SYNTHESIZE_NODE_ID, tuple(worker_ids or [PLAN_NODE_ID])))

        if state.validation is not None:
            if state.validation.source != "model":
                validation_status = PlanStepStatus.SKIPPED.value
            elif state.validation.satisfied:
                validation_status = PlanStepStatus.COMPLETED.value
            else:
                validation_status = PlanStepStatus.FAILED.value
            nodes.append(
                FlowNode(
                    id=VALIDATE_NODE_ID,
                    kind=FlowKind.VALIDATE,
                    label=flow_node_label(FlowKind.VALIDATE),
                    depends_on=[SYNTHESIZE_NODE_ID],
                    status=validation_status,
                )
            )
            deps_of.append((VALIDATE_NODE_ID, (SYNTHESIZE_NODE_ID,)))

    levels = _levels_for(deps_of)
    return [node.model_copy(update={"wave": levels.get(node.id, 0)}) for node in nodes]


def current_wave(state: DynamicPipelineState) -> int | None:
    """还差哪一波没跑完；全部有结果时返回 None。"""

    pending = [step for step in state.plan if step.id not in state.results]
    if not pending:
        return None
    levels = step_levels(state.plan)
    return min(levels.get(step.id, 0) for step in pending)


def finalize_state(state: DynamicPipelineState, workflow_id: str | None = None) -> DynamicPipelineState:
    """收尾：定终态、取最终交付物、汇总失败与跳过原因，并算出流程序列。"""

    synthesized = state.final_output
    fallback_output = final_output(state)
    output = synthesized or fallback_output
    failed, skipped = failure_triples(state)
    problems: list[str] = []
    if failed:
        problems.append(
            "失败步骤："
            + "；".join(f"{step_id}({error})" for step_id, _role, error in failed)
        )
    if skipped:
        problems.append(
            "因上游失败而跳过：" + "、".join(step_id for step_id, _role, _error in skipped)
        )
    if (
        state.synthesis_attempted
        and synthesized is None
        and fallback_output is not None
        and state.plan
    ):
        problems.append("合成器没有产出交付物，已退回最后一个成功子任务的产出。")
    if state.budget_exceeded:
        problems.append(
            f"本次执行达到 Token 预算上限（已用 {tokens_used(state)} / 上限 "
            f"{state.token_budget}），剩余子任务未执行，报告只覆盖已完成的部分。"
        )
    if not output and not problems:
        problems.append("计划中没有任何步骤产出内容。")

    error = " ".join(problems) if problems else None
    if output is None and error is None:
        error = "没有可用的最终交付物。"
    partial = bool(failed or skipped)
    settled = state.model_copy(
        update={
            "status": PipelineStatus.COMPLETED if output else PipelineStatus.FAILED,
            "final_output": output,
            "error": error,
            "partial": partial,
            "results": {**state.results, **_skipped_outcomes(blocked_steps(state), state)},
            "updated_at": _now(),
        }
    )
    settled = settled.model_copy(update={"flow": build_flow(settled)})
    log_event(
        logger,
        "dynamic.finish",
        level=logging.WARNING if error else logging.INFO,
        workflow_id=workflow_id,
        status=(PipelineStatus.COMPLETED if output else PipelineStatus.FAILED).value,
        steps_done=len(ordered_outcomes(state)),
        steps_total=len(state.plan),
        route=state.route,
        round=state.round,
        partial=partial,
        error=error,
    )
    return settled


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


def apply_skips(state: DynamicPipelineState) -> dict[str, StepOutcome]:
    """把「依赖已失败/跳过」的步骤标成 `skipped`，返回可直接并入 ``results`` 的增量。

    波次执行里必须在每一波之后做这件事：否则循环会一直认为"还有没跑的步骤"，
    而这些步骤永远不会就绪（它们的上游已经放弃）。
    """

    return _skipped_outcomes(blocked_steps(state), state)


# --------------------------------------------------------------------------------------
# LangGraph 图
# --------------------------------------------------------------------------------------


def _node_intake(
    model: BaseChatModel,
    workflow_id: str | None,
):
    def intake(state: DynamicPipelineState) -> dict[str, Any]:
        payload = intake_task(state.task, llm=model, workflow_id=workflow_id)
        intent = (
            IntentResult.model_validate(payload["intent"]) if payload.get("intent") else None
        )
        rewritten = str(payload["task"])
        updates: dict[str, Any] = {
            "task": rewritten,
            "rewritten_task": rewritten,
            "rewrite_source": payload["source"],
            "intent": intent,
            "intent_source": payload.get("intent_source"),
            "route": ROUTE_MULTI,
            "platform_tokens": state.platform_tokens + int(payload.get("tokens") or 0),
            "status": PipelineStatus.RUNNING,
            "updated_at": _now(),
        }
        if intent is not None and not intent.need_multi_subtask:
            # 护栏（ADR-038 §2）：只有**解析成功**且明确说"不需要多 Agent"才走直答；
            # 计划固定 1 步，且仍然要产出交付物。
            plan = single_agent_plan(rewritten, intent)
            updates.update(
                {
                    "route": ROUTE_SINGLE,
                    "plan": plan.steps,
                    "plan_source": plan.source,
                    "plan_rationale": plan.rationale,
                }
            )
        return updates

    return intake


def _node_planner(
    model: BaseChatModel,
    max_steps: int,
    workflow_id: str | None,
):
    def planner(state: DynamicPipelineState) -> dict[str, Any]:
        if state.plan:
            # 单 Agent 直答的计划由 intake 直接给出（1 步），这里只负责分派：
            # 再调一次规划模型等于把「简单任务省一次调用」的收益还回去。
            return {"status": PipelineStatus.RUNNING, "updated_at": _now()}
        plan = generate_plan(
            state.task,
            model,
            max_steps,
            workflow_id=workflow_id,
            intent=state.intent,
            defects=state.defects,
        )
        return {
            "plan": plan.steps,
            "plan_source": plan.source,
            "plan_rationale": plan.rationale,
            "platform_tokens": state.platform_tokens + plan.tokens,
            "status": PipelineStatus.RUNNING,
            "updated_at": _now(),
        }

    return planner


def _model_for(
    llm: BaseChatModel | None,
    settings: AgentSettings,
):
    """按步取模型：注入模型（单测/直跑）时原样返回，否则按该步的超时建模型。"""

    def resolve(step: PlanStep) -> BaseChatModel:
        if llm is not None:
            return llm
        return build_chat_model(
            settings, timeout_seconds=effective_timeout(step, settings)
        )

    return resolve


def _node_worker(
    resolve_model: Any,
    registry: ToolRegistry | None,
    workflow_id: str | None,
    attachments: Sequence[AttachmentPayload] = (),
):
    def worker(payload: dict[str, Any]) -> dict[str, Any]:
        step: PlanStep = payload["step"]
        state: DynamicPipelineState = payload["state"]
        caller = (
            ToolCaller(registry, scope=workflow_id, stage=f"dyn:{step.id}")
            if registry is not None
            else None
        )
        outcome = run_plan_step(
            step,
            state.task,
            state.results,
            resolve_model(step),
            caller,
            workflow_id,
            attachments,
        )
        return {
            # 只返回自己这一步的增量：并行分支由 reducer 按键合并（ADR-038 §6）。
            # **不返回 updated_at**：同一超级步里多个分支同时写它会触发
            # LangGraph 的「一个 key 一步只能写一次」校验，时间戳由汇聚节点统一写。
            "results": {step.id: outcome},
        }

    return worker


def _node_join():
    def join(state: DynamicPipelineState) -> dict[str, Any]:
        # 波次结束后两件事：先看成本闸门（预算用尽就不再开新批），再做连坐判定
        # （依赖失败的步骤从此不会再进入下一批）。顺序有讲究——预算拦下的步骤要留下
        # 「预算用尽」这个原因，而不是被连坐逻辑改写成「上游未成功」。
        budget_skips = apply_budget_stop(state)
        return {
            "results": {**budget_skips, **apply_skips(state)},
            "budget_exceeded": budget_exhausted(state),
            "updated_at": _now(),
        }

    return join


def _node_synthesize(
    model: BaseChatModel,
    registry: ToolRegistry | None,
    workflow_id: str | None,
):
    def synthesize(state: DynamicPipelineState) -> dict[str, Any]:
        failed, skipped = failure_triples(state)
        contents = {
            step.id: state.results[step.id].content
            for step in state.plan
            if step.id in state.results
            and state.results[step.id].status is PlanStepStatus.COMPLETED
        }
        caller = (
            ToolCaller(registry, scope=workflow_id, stage=SYNTHESIZE_NODE_ID)
            if registry is not None
            else None
        )
        outcome = run_synthesis(
            state.task,
            contents,
            llm=model,
            intent=state.intent,
            failed=failed,
            skipped=skipped,
            caller=caller,
            workflow_id=workflow_id,
        )
        return {
            "final_output": outcome.content or None,
            "synthesis_attempted": True,
            "synthesis_tokens": state.synthesis_tokens + outcome.tokens,
            "updated_at": _now(),
        }

    return synthesize


def _node_validate(
    model: BaseChatModel,
    workflow_id: str | None,
):
    def validate(state: DynamicPipelineState) -> dict[str, Any]:
        failed, _skipped = failure_triples(state)
        result = run_validation(
            state.task,
            state.final_output or "",
            llm=model,
            intent=state.intent,
            failed=failed,
            workflow_id=workflow_id,
        )
        return {
            "validation": result,
            "platform_tokens": state.platform_tokens + result.tokens,
            "updated_at": _now(),
        }

    return validate


def _after_synthesize(enabled: bool):
    def route(state: DynamicPipelineState) -> str:
        return "validate" if enabled else "finalize"

    return route


def _node_finalize(workflow_id: str | None):
    def finalize(state: DynamicPipelineState) -> dict[str, Any]:
        settled = finalize_state(state, workflow_id)
        return {
            "status": settled.status,
            "final_output": settled.final_output,
            "error": settled.error,
            "partial": settled.partial,
            "results": settled.results,
            "flow": settled.flow,
            "updated_at": settled.updated_at,
        }

    return finalize


def _after_start(state: DynamicPipelineState) -> str:
    """已带意图的续轮（重编排）跳过 intake，不重复花那一次平台调用。"""

    return "router" if state.intent_source is not None else "intake"


def _after_planner(
    max_parallel: int,
    max_steps: int,
):
    def route(state: DynamicPipelineState) -> Any:
        return _dispatch(state, max_parallel, max_steps)

    return route


def _dispatch(
    state: DynamicPipelineState,
    max_parallel: int,
    max_steps: int,
) -> Any:
    """下一批并行分派；没有更多可跑步骤时进合成或直接收尾。"""

    batch = pending_batch(state, state.plan, min(max_parallel, max(1, max_steps)))
    if batch:
        return [
            Send("worker", {"step": step, "state": state}) for step in batch
        ]
    if state.route == ROUTE_SINGLE or not state.plan:
        return "finalize"
    return "synthesize"


def recursion_limit(max_steps: int, max_parallel: int = DEFAULT_MAX_PARALLEL_WORKERS) -> int:
    """按步骤数与并发上限估算 LangGraph 递归上限：每批 2 个超级步（分派 + 汇聚），
    再加 intake / 规划 / 合成 / 校验 / 收尾，留一倍余量。"""

    batches = max_steps if max_parallel <= 1 else max_steps // max_parallel + 1
    return max(25, 4 * batches + 20)


def build_dynamic_pipeline(
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
    tool_registry: ToolRegistry | None = None,
    max_steps: int = DEFAULT_MAX_PLAN_STEPS,
    workflow_id: str | None = None,
    attachments: Sequence[AttachmentPayload] = (),
):
    """构建动态协作图（ADR-038）：

    ``intake →（单 Agent 直答 | planner → 波内并行 worker → 汇聚循环 → 合成 → 校验）→ finalize``。

    ``tool_registry`` 为 None 时使用 ``default_tool_registry()``；解析不到注册表
    （例如无 MCP 实现）时步骤不调用工具，与静态图口径一致。

    ``attachments`` 只在进程内直跑时使用；Dapr 链路按 id 从库里取（ADR-021）。

    这一份是**进程内镜像**：生产链路的持久化执行在 Dapr 父工作流里（ADR-020），
    但两条路径的拓扑、波次判定与状态口径共用本模块的纯函数，避免各写一套。
    """

    resolved = settings or get_settings()
    platform_model = llm or build_chat_model(resolved)
    role_model = _model_for(llm, resolved)
    registry = tool_registry if tool_registry is not None else default_tool_registry()
    max_parallel = resolve_max_parallel_workers(resolved)

    builder = StateGraph(DynamicPipelineState)
    builder.add_node("intake", _node_intake(platform_model, workflow_id))
    builder.add_node("planner", _node_planner(platform_model, max_steps, workflow_id))
    builder.add_node("worker", _node_worker(role_model, registry, workflow_id, attachments))
    builder.add_node("join", _node_join())
    builder.add_node("synthesize", _node_synthesize(platform_model, registry, workflow_id))
    builder.add_node("validate", _node_validate(platform_model, workflow_id))
    builder.add_node("finalize", _node_finalize(workflow_id))

    def router(state: DynamicPipelineState) -> Any:
        return _dispatch(state, max_parallel, max_steps)

    builder.add_conditional_edges(
        START, _after_start, {"intake": "intake", "router": "planner"}
    )
    # intake 之后统一进 planner 节点：它按 `state.plan` 决定「要不要真的规划」
    # （单 Agent 直答的计划已由 intake 给出，此处只分派），再交条件边并行分派。
    builder.add_edge("intake", "planner")
    builder.add_conditional_edges(
        "planner",
        router,
        {"worker": "worker", "synthesize": "synthesize", "finalize": "finalize"},
    )
    builder.add_edge("worker", "join")
    builder.add_conditional_edges(
        "join",
        router,
        {"worker": "worker", "synthesize": "synthesize", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "synthesize",
        _after_synthesize(bool(resolved.validation_enabled)),
        {"validate": "validate", "finalize": "finalize"},
    )
    builder.add_edge("validate", "finalize")
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
    """用完整的动态图执行一次协作，返回终态 ``DynamicPipelineState``。

    校验不达标且还有轮次预算时**重跑一轮**：新的一轮复用第一轮的 intake 结果
    （`intent_source` 已就位 → 图跳过 intake），带上校验器的缺陷清单重新规划与执行。
    重编排放在图外、按状态整份重建，是因为「清空上一轮的结果」在并行 reducer 语义下
    没有干净的写法；判词是「一次 invoke = 一轮执行」，这也正是 Dapr 父工作流的形态。
    """

    resolved = settings or get_settings()
    max_rounds = resolve_max_plan_rounds(resolved)
    max_parallel = resolve_max_parallel_workers(resolved)
    graph = build_dynamic_pipeline(
        llm=llm,
        settings=resolved,
        tool_registry=tool_registry,
        max_steps=max_steps,
        workflow_id=workflow_id,
        attachments=attachments,
    )

    state = DynamicPipelineState(task=task, token_budget=resolve_token_budget(resolved))
    for round_index in range(1, max_rounds + 2):
        output = graph.invoke(
            state,
            config={"recursion_limit": recursion_limit(max_steps, max_parallel)},
        )
        settled = DynamicPipelineState.model_validate(output)
        if (
            settled.status is not PipelineStatus.COMPLETED
            or settled.validation is None
            or settled.validation.satisfied
            or round_index > max_rounds
            # 预算已经用尽的执行不再开第二轮：重编排是**新的一轮完整执行**，
            # 按定义就会再花一份钱——成本闸门必须在它之前生效。
            or budget_exhausted(settled)
        ):
            return settled
        log_event(
            logger,
            "dynamic.replan",
            workflow_id=workflow_id,
            round=round_index + 1,
            defects=len(settled.validation.defects),
            missing=len(settled.validation.missing),
        )
        state = DynamicPipelineState(
            task=settled.rewritten_task or settled.task,
            rewritten_task=settled.rewritten_task,
            rewrite_source=settled.rewrite_source,
            intent=settled.intent,
            intent_source=settled.intent_source,
            route=ROUTE_MULTI,
            round=round_index + 1,
            defects=[*settled.validation.defects, *settled.validation.missing],
            validation_rounds=round_index,
            tokens_used_prior=tokens_used(settled),
            token_budget=settled.token_budget,
            final_output=None,
        )
    return settled


def dynamic_checkpoint_summary(state: DynamicPipelineState) -> dict[str, Any]:
    """落库到 ``workflow_runs.checkpoint`` 的摘要；字段与静态摘要有交集，便于前端复用。

    契约（`doc/api.md` §5.18、ADR-038）：

    - `plan[]` 仍然**只装子任务**，语义不变；新增 `expected_output` / `retry` /
      `timeout_seconds` 三个来自计划的字段；
    - `flow[]` 是给人看的整条流程（意图 / 编排 / Worker / 合成 / 校验），画布优先读它；
    - `route` / `round` / `partial` / `failed_steps` / `skipped_steps` / `validation`
      让「走了哪条路、是不是部分结果、校验过没过」不用靠猜。
    """

    failed, skipped = failure_triples(state)
    return {
        "mode": "dynamic",
        "status": state.status.value,
        "route": state.route,
        "plan_source": state.plan_source,
        # 改写结果随 checkpoint 落库（ADR-037）：使用者要能看见"平台把我的话改成了什么"。
        "rewritten_task": state.rewritten_task,
        "rewrite_source": state.rewrite_source,
        "intent": state.intent.model_dump(mode="json") if state.intent else None,
        "intent_source": state.intent_source,
        "round": state.round,
        "validation_rounds": state.validation_rounds,
        "current_step": None,
        "current_wave": current_wave(state),
        # 成本闸门（ADR-038 §9）：用量是**下限口径**（取不到模型用量时按 0 计），
        # 预算为 0 表示不限制——两种取值都如实带出来，界面不必猜。
        "tokens_used": tokens_used(state),
        "token_budget": state.token_budget,
        "budget_exceeded": state.budget_exceeded,
        "completed_steps": [outcome.step_id for outcome in ordered_outcomes(state)],
        "failed_steps": [step_id for step_id, _role, _error in failed],
        "skipped_steps": [step_id for step_id, _role, _error in skipped],
        "partial": state.partial,
        "validation": (
            state.validation.model_dump(mode="json") if state.validation else None
        ),
        "flow": [node.model_dump(mode="json") for node in state.flow or build_flow(state)],
        "plan": [
            {
                "id": step.id,
                "role": step.role.value,
                "depends_on": list(step.depends_on),
                "expected_output": step.expected_output,
                "retry": step.retry,
                "timeout_seconds": step.timeout_seconds,
                "attempts": (
                    state.results[step.id].attempts if step.id in state.results else None
                ),
                "tokens": (
                    state.results[step.id].tokens if step.id in state.results else None
                ),
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


def dynamic_state_label(node_id: str, round_number: int = 1) -> str:
    """动态节点在状态存储里的键后缀：`dyn:r{轮次}:{节点}`。

    轮次进 key 是必须的：重编排的第二轮会有同名的 `s1`，不带轮次就会把上一轮的
    轨迹覆盖掉，「哪一轮的产出」也就无从审计。
    """

    return f"dyn:r{round_number}:{node_id}"


def dynamic_subtask_instance_id(
    workflow_id: str, step_id: str, round_number: int = 1
) -> str:
    """子工作流实例 ID：`{workflow_id}:dyn:r{轮次}:{步骤}`。

    与静态链路的 `{workflow_id}:{stage}` 同口径——同一次执行重放必须得到相同实例 ID，
    否则恢复一次就会重跑一遍已经完成的子任务。
    """

    return f"{workflow_id}:{dynamic_state_label(step_id, round_number)}"


def resolve_orchestration_mode(settings: AgentSettings | None = None) -> str:
    """解析生效的编排模式；非法值回退 ``static``（宁可固定流程，不要跑不起来）。"""

    mode = (settings or get_settings()).orchestration_mode
    return mode if mode in ORCHESTRATION_MODES else "static"


def resolve_max_plan_steps(settings: AgentSettings | None = None) -> int:
    value = (settings or get_settings()).max_plan_steps
    return value if value > 0 else DEFAULT_MAX_PLAN_STEPS

"""Agent 角色 Prompt 定义（成员 C D5-6 内容层）。

对齐事实源：
- `分工.md` §2/§3：成员 C 负责「设计 Agent 角色 Prompt 与示例场景数据」；
- `doc/data-model.md` §3 agents 表：role ∈ collector / analyst / reporter；
- `doc/api.md` §4.7：GET /agents 返回角色示例（collector → 信息收集 Agent）；
- `doc/15 AI Native多智能体协作平台.md` 模块1/§六：三步协作
  「信息收集 → 数据分析 → 报告生成」。

本模块只提供角色静态定义（id、展示名、默认模型参数、系统 Prompt）与访问器，
不包含图拓扑、角色分配或流水线接线——这些由编排层（成员 A）与 Workflow（成员 B）
负责。决策记录见 `doc/decisions/006-agent-role-prompts.md`。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RoleId(StrEnum):
    """角色标识，取值对齐 `doc/data-model.md` §3 agents.role。

    注意与 `app.orchestration.pipeline.PipelineStage` 的差异：后者是流水线阶段
    （collect/analyze/report），这里是协作角色（collector/analyst/reporter）。
    """

    COLLECTOR = "collector"
    ANALYST = "analyst"
    REPORTER = "reporter"


class RoleDefinition(BaseModel):
    """一个 Agent 角色的静态定义。

    字段与 `doc/data-model.md` §3 agents 表（name/role/model/temperature）对应，
    额外增加 `system_prompt`（应用层角色内容，本期不作为数据库列，见 ADR-006）。
    """

    id: RoleId
    name: str = Field(min_length=1, description="展示名，如「信息收集 Agent」")
    model: str = Field(
        default="qwen2.5-coder:7b",
        description="默认模型，默认对齐 app.config.AgentSettings.ollama_model",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    system_prompt: str = Field(min_length=1)


_COLLECTOR_PROMPT = """\
你是「信息收集 Agent」，负责多智能体流水线的收集阶段。

职责：围绕用户任务的主题，收集、检索并整理相关的事实、要点与线索，
输出一份结构化的信息清单，供「数据分析 Agent」使用。

要求：
- 逐条列出收集到的要点，尽量具体、可核验；
- 不臆造来源与数据；信息不足时如实标注「待补充」；
- 只输出信息清单本身，不要加入分析或结论（那是下游职责）。\
"""

_ANALYST_PROMPT = """\
你是「数据分析 Agent」，负责多智能体流水线的分析阶段。

职责：基于「信息收集 Agent」提供的信息清单，进行归纳、对比与提炼，
识别关键结论、趋势与风险，输出结构化的分析摘要，供「报告生成 Agent」使用。

要求：
- 结论必须能从输入信息中推导，不无中生有；
- 区分「事实」与「推断」，推断需说明依据；
- 对相互矛盾的信息明确指出，并给出取舍建议；
- 只输出分析结果，不要重复原始信息清单。\
"""

_REPORTER_PROMPT = """\
你是「报告生成 Agent」，负责多智能体流水线的报告阶段。

职责：基于「数据分析 Agent」的分析摘要，生成面向最终用户的正式报告，
结构清晰、语言通顺、可直接阅读。

要求：
- 报告包含：概述、关键结论、支撑细节、风险与建议；
- 使用正式中文，条理清晰，适度使用标题与列表；
- 忠实于上游结论，不引入未在输入中出现的新事实。\
"""


ROLE_DEFINITIONS: dict[RoleId, RoleDefinition] = {
    RoleId.COLLECTOR: RoleDefinition(
        id=RoleId.COLLECTOR,
        name="信息收集 Agent",
        system_prompt=_COLLECTOR_PROMPT,
    ),
    RoleId.ANALYST: RoleDefinition(
        id=RoleId.ANALYST,
        name="数据分析 Agent",
        system_prompt=_ANALYST_PROMPT,
    ),
    RoleId.REPORTER: RoleDefinition(
        id=RoleId.REPORTER,
        name="报告生成 Agent",
        system_prompt=_REPORTER_PROMPT,
    ),
}


def get_role(role_id: RoleId | str) -> RoleDefinition:
    """按角色 id 返回角色定义；未知角色抛 ``ValueError``。"""
    try:
        rid = role_id if isinstance(role_id, RoleId) else RoleId(role_id)
    except ValueError as exc:
        raise ValueError(f"未知角色: {role_id}") from exc
    return ROLE_DEFINITIONS[rid]


def role_ids() -> tuple[RoleId, ...]:
    """按流水线顺序（收集 → 分析 → 报告）返回全部角色 id。"""
    return (RoleId.COLLECTOR, RoleId.ANALYST, RoleId.REPORTER)

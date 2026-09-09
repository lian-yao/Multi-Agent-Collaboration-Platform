"""示例场景数据（成员 C D5-6 内容层）。

提供多智能体协作流水线的演示场景：用户原始任务 + 三步角色的目标说明，
供 M3 演示与后续 A/B 接线、D 端 Web 展示复用。

角色顺序对齐 `app.agents.roles.role_ids()`：collector → analyst → reporter。
决策记录见 `doc/decisions/006-agent-role-prompts.md`。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScenarioStage(BaseModel):
    """场景中一个阶段的说明：角色 + 该阶段目标。"""

    role: str = Field(
        min_length=1,
        description="角色 id，与 app.agents.roles.RoleId 的取值对齐",
    )
    objective: str = Field(min_length=1, description="该阶段的输入/目标说明")


class Scenario(BaseModel):
    """一个演示场景。"""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    task: str = Field(min_length=1, description="用户原始任务")
    description: str = ""
    stages: list[ScenarioStage] = Field(min_length=1)


ARTICLE_ANALYSIS = Scenario(
    id="article-analysis",
    name="技术文章要点分析",
    task="分析一篇关于大语言模型 Agent 的技术文章，提炼核心要点并生成报告",
    description="三步协作演示：信息收集 → 数据分析 → 报告生成",
    stages=[
        ScenarioStage(
            role="collector",
            objective="收集该主题相关的资料与要点",
        ),
        ScenarioStage(
            role="analyst",
            objective="归纳关键结论、趋势与风险",
        ),
        ScenarioStage(
            role="reporter",
            objective="生成面向用户的正式报告",
        ),
    ],
)

DEMO_SCENARIOS: list[Scenario] = [ARTICLE_ANALYSIS]

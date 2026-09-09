"""示例场景（成员 C D5-6：示例场景数据）。

提供多智能体协作流水线的演示场景，供 M3 演示与 A/B 接线、D 端 Web 展示复用。
决策记录见 `doc/decisions/006-agent-role-prompts.md`。
"""

from examples.scenarios import (
    DEMO_SCENARIOS,
    Scenario,
    ScenarioStage,
)

__all__ = [
    "DEMO_SCENARIOS",
    "Scenario",
    "ScenarioStage",
]

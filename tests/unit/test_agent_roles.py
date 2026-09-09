"""成员 C D5-6：角色 Prompt 与示例场景数据的单元测试。

验证三角色定义齐备、系统 Prompt 非空、访问器行为、默认值与数据模型对齐，
以及演示场景的三步顺序与角色一致。不依赖任何外部服务。
"""

import pytest

from app.agents.roles import (
    ROLE_DEFINITIONS,
    RoleDefinition,
    RoleId,
    get_role,
    role_ids,
)
from examples.scenarios import DEMO_SCENARIOS


def test_role_ids_match_data_model():
    assert {role.value for role in RoleId} == {
        "collector",
        "analyst",
        "reporter",
    }
    assert role_ids() == (RoleId.COLLECTOR, RoleId.ANALYST, RoleId.REPORTER)


def test_three_roles_are_defined_in_order():
    assert list(ROLE_DEFINITIONS) == [
        RoleId.COLLECTOR,
        RoleId.ANALYST,
        RoleId.REPORTER,
    ]


def test_each_role_has_nonempty_name_and_prompt():
    for role in RoleId:
        definition = get_role(role)
        assert isinstance(definition, RoleDefinition)
        assert definition.name
        assert definition.system_prompt


def test_prompts_are_distinct_across_roles():
    prompts = {get_role(role).system_prompt for role in RoleId}
    assert len(prompts) == 3


def test_defaults_align_with_agent_settings():
    definition = get_role(RoleId.COLLECTOR)

    assert definition.model == "qwen2.5-coder:7b"
    assert definition.temperature == 0.2


def test_get_role_accepts_string_id():
    definition = get_role("collector")

    assert definition.id is RoleId.COLLECTOR
    assert definition.name == "信息收集 Agent"


def test_get_role_rejects_unknown_id():
    with pytest.raises(ValueError, match="未知角色"):
        get_role("nonexistent")


def test_scenario_stages_follow_pipeline_role_order():
    scenario = DEMO_SCENARIOS[0]

    assert scenario.id == "article-analysis"
    assert scenario.task
    assert [stage.role for stage in scenario.stages] == [
        "collector",
        "analyst",
        "reporter",
    ]


def test_scenario_stage_roles_align_with_role_ids():
    scenario = DEMO_SCENARIOS[0]

    assert [stage.role for stage in scenario.stages] == [
        role.value for role in role_ids()
    ]

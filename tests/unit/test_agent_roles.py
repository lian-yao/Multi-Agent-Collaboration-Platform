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


def test_prompts_are_aligned_with_the_current_software():
    """提示词必须写清平台**真实**给到角色能力与约束（ADR-037 §4）。

    这条钉的是"与软件对齐"这个要求本身：原先三个 prompt 只描述「收集/分析/报告」的抽象分工，
    完全没提会话工作区文件工具、附件、MCP 与审批，模型因此不知道自己手上有什么。
    以后若有人精简提示词把这些删掉，这条会红——而不是等到线上答题变差才发现。
    """

    for role in RoleId:
        prompt = get_role(role).system_prompt
        assert "工作区" in prompt, f"{role.value} 的提示词没交代工作区边界"
        assert "list_work_files" in prompt, f"{role.value} 的提示词没列出手上的文件工具"
        assert "MCP" in prompt or "搜索" in prompt or "附件" in prompt, (
            f"{role.value} 的提示词没交代平台提供的其它工具"
        )

    # 破坏性动作的口径：覆盖与删除是**人工审批**，不是失败（会写文件的角色必须知道）
    assert "人工审批" in get_role(RoleId.COLLECTOR).system_prompt
    assert "人工审批" in get_role(RoleId.REPORTER).system_prompt
    # 上下游契约：每个角色都要知道自己给谁、从谁那儿拿
    assert "下游" in get_role(RoleId.COLLECTOR).system_prompt
    assert "上游" in get_role(RoleId.ANALYST).system_prompt
    assert "使用者" in get_role(RoleId.REPORTER).system_prompt


def test_role_prompts_match_the_automatic_orchestration_mode():
    """位置口径必须跟**当前工作模式**一致（ADR-006 修订 2026-09-24、ADR-038 §9）。

    自动编排（前端默认）下：同一角色可能在一次执行里**并行出现多次**、上游可能是零个或多个
    子任务、使用者看到的最终交付物由**合成器**产出。旧提示词写死「第一步 / 第二步 / 最后一步」，
    会让 collector 以为下游一定是 analyst、让 reporter 以为「使用者主要看我的输出」——
    后者会直接导致子任务成稿用错口吻、和合成器抢着当最终结论。

    这条同时钉三件事：不再出现固定步序、说清并行互不可见、说清产出会被合成。
    """

    for role in RoleId:
        prompt = get_role(role).system_prompt
        assert "并行" in prompt, f"{role.value} 没说明同波并行子任务互相看不到产出"
        assert "合成" in prompt, f"{role.value} 没说明产出会被合成器收口"
        for stale in ("第一步", "第二步", "最后一步"):
            assert stale not in prompt, f"{role.value} 还在用固定三步的位置口径：{stale}"

    reporter = get_role(RoleId.REPORTER).system_prompt
    assert "不一定" in reporter and "单 Agent 直答" in reporter, (
        "reporter 必须知道自己的产出不一定是使用者看到的最终交付物（单 Agent 直答时才是）"
    )
    collector = get_role(RoleId.COLLECTOR).system_prompt
    assert "同时有多个你" in collector, "collector 要知道同一角色可以并行出现多次"


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

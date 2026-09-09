"""成员 A D5-6：多 Agent 流水线图与角色分配的单元测试。

覆盖 U-05（LangGraph 状态 Schema 序列化）与 M3 固定团队：
collector → analyst → reporter 顺序执行，且每个节点携带对应角色的
system_prompt。全程注入 FakeChatModel，不请求任何外部模型服务。
"""

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agents.roles import RoleId, get_role, role_ids
from app.orchestration.pipeline import (
    PIPELINE_STEPS,
    PipelineStage,
    PipelineStatus,
    deserialize_pipeline_state,
    serialize_pipeline_state,
)
from app.orchestration.pipeline_graph import (
    graph_node_order,
    pipeline_roles,
    role_for_stage,
    run_multi_agent_pipeline,
    run_role_stage,
)


class ScriptedChatModel(BaseChatModel):
    """记录每次调用的消息并按序返回固定回复，替代真实 LLM。"""

    replies: list[str]
    calls: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.replies.pop(0) if self.replies else "done"
        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_pipeline_roles_match_data_model_order():
    assert pipeline_roles() == role_ids()
    assert graph_node_order() == ("collector", "analyst", "reporter")


def test_role_assignment_maps_each_stage_to_fixed_role():
    assert [
        role_for_stage(stage).value
        for stage in PIPELINE_STEPS
    ] == ["collector", "analyst", "reporter"]


def test_role_for_stage_rejects_unknown_stage():
    with pytest.raises(ValueError, match="未知流水线阶段"):
        role_for_stage("explore")


def test_multi_agent_graph_runs_roles_in_order():
    fake = ScriptedChatModel(
        replies=["要点一\n要点二", "关键结论：要点一风险较低", "最终报告正文"]
    )

    state = run_multi_agent_pipeline(
        "分析一篇技术文章并生成报告",
        llm=fake,
    )

    assert state.status is PipelineStatus.COMPLETED
    assert state.completed_steps == [
        PipelineStage.COLLECT,
        PipelineStage.ANALYZE,
        PipelineStage.REPORT,
    ]
    assert state.results[PipelineStage.COLLECT]["content"] == "要点一\n要点二"
    report = state.results[PipelineStage.REPORT]
    assert report["step"] == "report"
    assert report["content"] == "最终报告正文"
    assert report["previous"]["step"] == "analyze"
    assert report["previous"]["previous"]["step"] == "collect"


def test_each_role_node_uses_its_own_system_prompt():
    fake = ScriptedChatModel(replies=["收集", "分析", "报告"])

    run_multi_agent_pipeline("演示任务", llm=fake)

    assert len(fake.calls) == 3
    for role, messages in zip(role_ids(), fake.calls):
        assert messages[0].content == get_role(role).system_prompt


def test_downstream_prompt_contains_upstream_content():
    fake = ScriptedChatModel(replies=["上游要点", "分析结论", "报告"])

    run_multi_agent_pipeline("演示任务", llm=fake)

    analyst_human = fake.calls[1][1].content
    reporter_human = fake.calls[2][1].content
    assert "上游要点" in analyst_human
    assert "分析结论" in reporter_human


def test_graph_terminal_state_roundtrips_through_json():
    fake = ScriptedChatModel(replies=["收集", "分析", "报告"])

    state = run_multi_agent_pipeline("演示任务", llm=fake)
    restored = deserialize_pipeline_state(
        json.dumps(serialize_pipeline_state(state))
    )

    assert restored == state
    assert restored.results == state.results


def test_run_role_stage_is_drop_in_for_fake_stage_result():
    fake = ScriptedChatModel(replies=["关键风险：延迟"])
    previous = {"step": "collect", "status": "completed", "content": "要点"}

    result = run_role_stage(
        PipelineStage.ANALYZE,
        task="分析任务",
        previous=previous,
        llm=fake,
    )

    assert result == {
        "step": "analyze",
        "status": "completed",
        "content": "关键风险：延迟",
        "previous": previous,
    }
    assert fake.calls[0][0].content == get_role(RoleId.ANALYST).system_prompt


def test_downstream_role_rejects_missing_upstream():
    with pytest.raises(ValueError, match="缺少上游结果"):
        run_role_stage(
            PipelineStage.REPORT,
            task="报告任务",
            llm=ScriptedChatModel(replies=["报告"]),
        )

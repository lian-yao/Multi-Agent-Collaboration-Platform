"""阶段执行轨迹的读取契约（`doc/api.md` §5.17）。

这一层是「执行台卡片弹窗显示什么」的唯一数据源，因此重点不在「能读出字段」，
而在**四种「没有轨迹」互不混淆**：还没轮到、正在跑、跑完了但状态被清理、动态链路
不落盘。把它们混成一句「暂无数据」，用户就只能猜自己的任务执行到哪一步了。

读取函数通过 `read_step` 注入替身：本文件**不连 Dapr、不连数据库**。
"""

import json

import pytest

from app.api.stage_trace import (
    DYNAMIC_TRACE_REASON,
    STAGE_TRACE_MAX_OUTPUT_CHARS,
    STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS,
    read_stage_traces,
)
from app.orchestration.pipeline import (
    PipelineStage,
    complete_step,
    new_pipeline_state,
    serialize_pipeline_state,
    start,
)

TASK = "统计上季度华东区的退货率，并给出异常门店清单"

COLLECT_RESULT = {
    "step": "collect",
    "status": "completed",
    "content": "已收齐三类原始数据：销量表、退货单、区域字典。",
    "previous": None,
    "tool_calls": [
        {
            "call_id": "call-collect-1",
            "tool_name": "list_session_files",
            "input": {"session_id": "s-1"},
            "output": ["sales.csv", "returns.csv"],
            "status": "succeeded",
            "error": None,
        }
    ],
}

ANALYZE_RESULT = {
    "step": "analyze",
    "status": "completed",
    "content": "整体退货率 4.8%，高于基线的门店有三家。",
    "previous": COLLECT_RESULT,
    "tool_calls": [
        {
            "call_id": "call-analyze-1",
            "tool_name": "calculator",
            "input": {"expression": "128/2680"},
            "output": {"result": 0.0478},
            "status": "succeeded",
            "error": None,
        },
        {
            "call_id": "call-analyze-2",
            "tool_name": "sql_query",
            "input": {"sql": "select * from returns"},
            "output": None,
            "status": "failed",
            "error": "SandboxViolation: 只读沙箱拒绝该语句",
        },
    ],
}

REPORT_RESULT = {
    "step": "report",
    "status": "completed",
    "content": "结论：退货率处于正常区间，三家门店需要复核。",
    "previous": ANALYZE_RESULT,
    "tool_calls": [],
}


def _states() -> dict[str, dict]:
    """每个阶段 key 存的是**该阶段完成后**的完整状态（与写入侧一致）。"""

    state = start(new_pipeline_state(task=TASK))
    states: dict[str, dict] = {}
    for stage, result in (
        (PipelineStage.COLLECT, COLLECT_RESULT),
        (PipelineStage.ANALYZE, ANALYZE_RESULT),
        (PipelineStage.REPORT, REPORT_RESULT),
    ):
        state = complete_step(state, stage, result)
        states[stage.value] = serialize_pipeline_state(state)
    return states


def _reader(payloads: dict[str, dict]):
    """构造 `read_step(workflow_id, stage)` 替身：只对给定阶段返回载荷。"""

    def read_step(_workflow_id: str, stage: str):
        state = payloads.get(stage)
        return None if state is None else {"workflow_id": "w1", "step": stage, "result": state}

    return read_step


def _items(result: dict) -> dict[str, dict]:
    return {item["stage"]: item for item in result["items"]}


def test_static_trace_carries_output_tool_calls_and_upstream_input():
    states = _states()
    result = read_stage_traces("w1", checkpoint=None, read_step=_reader(states))

    assert result["availability"] == "available"
    assert result["mode"] == "static"
    assert result["task"] == TASK  # 原始任务从任一阶段状态里取回，弹窗不必另外要一次
    items = _items(result)

    assert items["collect"]["output"] == COLLECT_RESULT["content"]
    assert items["collect"]["input"] is None  # 根阶段没有上游，输入就是原始任务
    assert items["collect"]["input_from"] is None
    assert [call["tool_name"] for call in items["collect"]["tool_calls"]] == ["list_session_files"]

    # 下游阶段的「分配到的任务」= 上游产出，且要标明来自哪一步。
    assert items["analyze"]["input"] == COLLECT_RESULT["content"]
    assert items["analyze"]["input_from"] == "collect"
    assert items["report"]["input"] == ANALYZE_RESULT["content"]
    assert items["report"]["input_from"] == "analyze"

    # 失败的调用要连着原因一起带出来——弹窗里最该被看到的就是这一条。
    failed = items["analyze"]["tool_calls"][1]
    assert failed["status"] == "failed"
    assert failed["error"].startswith("SandboxViolation")
    assert items["analyze"]["reason"] is None
    assert items["analyze"]["truncated"] is False


@pytest.mark.parametrize(
    "checkpoint,stage,expected",
    [
        ({}, "collect", "尚未开始"),
        ({"current_step": "analyze"}, "analyze", "正在执行"),
        ({"completed_steps": ["collect"], "current_step": "analyze"}, "collect", "已被清理"),
    ],
)
def test_missing_trace_explains_which_case_it_is(checkpoint, stage, expected):
    """三种「没有轨迹」给三句不同的话，都指向不同的下一步动作。"""

    result = read_stage_traces("w1", checkpoint=checkpoint, read_step=_reader({}))
    item = _items(result)[stage]

    assert item["output"] is None
    assert item["tool_calls"] == []
    assert expected in item["reason"]


def test_dynamic_mode_says_not_integrated_instead_of_pretending_empty():
    result = read_stage_traces(
        "w1",
        checkpoint={"mode": "dynamic", "completed_steps": ["s1"], "plan": []},
        read_step=_reader(_states()),
    )

    assert result["mode"] == "dynamic"
    assert result["availability"] == "not_integrated"
    assert result["items"] == []
    assert result["reason"] == DYNAMIC_TRACE_REASON
    assert "不落盘" in result["reason"]


def test_long_output_is_truncated_and_flagged():
    states = _states()
    states["report"]["results"]["report"] = {
        **REPORT_RESULT,
        "content": "报" * (STAGE_TRACE_MAX_OUTPUT_CHARS + 500),
    }
    item = _items(read_stage_traces("w1", checkpoint=None, read_step=_reader(states)))["report"]

    assert item["truncated"] is True
    assert len(item["output"]) == STAGE_TRACE_MAX_OUTPUT_CHARS
    # 截断不外泄：上游输入不因为下游超长而被动过。
    assert item["input"] == ANALYZE_RESULT["content"]


def test_oversized_tool_payload_keeps_a_preview_and_the_byte_count():
    states = _states()
    blob = "x" * (STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS + 100)
    states["collect"]["results"]["collect"] = {
        **COLLECT_RESULT,
        "tool_calls": [
            {
                "call_id": "call-big",
                "tool_name": "read_session_file",
                "input": {"name": "huge.csv"},
                "output": {"text": blob},
                "status": "succeeded",
                "error": None,
            }
        ],
    }
    call = _items(read_stage_traces("w1", checkpoint=None, read_step=_reader(states)))["collect"][
        "tool_calls"
    ][0]

    assert call["input"] == {"name": "huge.csv"}  # 小入参原样保留
    assert call["output"]["truncated"] is True
    assert call["output"]["bytes"] > STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS
    assert len(call["output"]["preview"]) == STAGE_TRACE_MAX_TOOL_PAYLOAD_CHARS
    assert json.dumps(call["output"], ensure_ascii=False)


def test_corrupt_payload_is_reported_instead_of_raised():
    """状态存储里躺着一份坏载荷时，弹窗要说明白，不能 500。"""

    def read_step(_workflow_id: str, stage: str):
        if stage != "collect":
            return None
        return {"workflow_id": "w1", "step": stage, "result": {"task": ""}}  # 缺 results 且 task 非法

    result = read_stage_traces("w1", checkpoint={"completed_steps": ["collect"]}, read_step=read_step)
    item = _items(result)["collect"]

    assert item["output"] is None
    assert "无法解析" in item["reason"]


def test_state_store_failure_is_not_swallowed():
    """读取器抛异常时**继续往上抛**：由接口层归一化成 503。

    吞掉它就等于把「环境没起来」伪装成「这个阶段没有轨迹」。
    """

    def read_step(_workflow_id: str, _stage: str):
        raise OSError("connection refused")

    with pytest.raises(OSError):
        read_stage_traces("w1", checkpoint=None, read_step=read_step)

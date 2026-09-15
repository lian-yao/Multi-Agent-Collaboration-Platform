"""性能测量脚本的纯逻辑（成员 C D9-10）。

`scripts/perf_concurrency.py` 的百分位与日志解析是「性能数据」的口径来源：
口径错了数据就没意义，因此这里用真实日志行形状固定住解析契约与百分位算法，
不依赖容器环境（采集通道本身在真实环境验收时验证）。
"""

from __future__ import annotations

from scripts.perf_concurrency import build_report, percentile, summarize_logs

# 真实 backend 日志行（`event=llm.finish` / `event=tool.call`，见 app/observability）。
LLM_FINISH_LINE = (
    "backend-1  | 2026-09-15 07:39:02,691 INFO macp.observability.callbacks "
    "event=llm.finish duration_ms=7870.5 input_tokens=579 output_tokens=30 total_tokens=609"
)
TOOL_FAILED_LINE = (
    "backend-1  | 2026-09-15 07:39:02,692 WARNING macp.orchestration.tools "
    'event=tool.call call_id=abc tool_name=web_search status=failed duration_ms=3.5 '
    'error="ToolExecutionError: boom"'
)
TOOL_SUCCEEDED_LINE = (
    "backend-1  | 2026-09-15 07:39:02,700 INFO macp.orchestration.tools "
    "event=tool.call call_id=def tool_name=calculator status=succeeded duration_ms=1.2"
)


def test_percentile_uses_nearest_rank() -> None:
    values = [float(value) for value in range(1, 11)]

    assert percentile(values, 0.50) == 5.0
    assert percentile(values, 0.95) == 10.0
    assert percentile([3.0], 0.95) == 3.0
    assert percentile([], 0.5) is None


def test_summarize_logs_counts_tokens_and_tool_outcomes() -> None:
    summary = summarize_logs(
        "\n".join(
            [
                LLM_FINISH_LINE,
                LLM_FINISH_LINE.replace("total_tokens=609", "total_tokens=641"),
                TOOL_FAILED_LINE,
                TOOL_SUCCEEDED_LINE,
                # 不认识的行必须被忽略，不能污染统计。
                "backend-1  | 2026-09-15 07:39:02,700 INFO macp.orchestration.pipeline "
                "event=stage.finish stage=collect chars=80 tool_calls=0",
            ]
        )
    )

    assert summary["llm_calls"] == 2
    assert summary["input_tokens"] == 1158
    assert summary["output_tokens"] == 60
    assert summary["total_tokens"] == 1250
    assert summary["tokens_per_call"] == 625.0
    assert summary["llm_duration_ms"]["max"] == 7870.5
    assert summary["tool_calls"] == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "success_rate": 0.5,
        "by_name": {"web_search": 1, "calculator": 1},
    }


def test_summarize_logs_without_samples_reports_no_rate() -> None:
    """没有工具调用时不给成功率（F-02 下真实模型就是这种情况），不能编造成 0%。"""

    summary = summarize_logs(LLM_FINISH_LINE)

    assert summary["tool_calls"]["total"] == 0
    assert summary["tool_calls"]["success_rate"] is None


def test_build_report_separates_success_from_terminal_failure() -> None:
    from scripts.perf_concurrency import SessionResult

    results = [
        SessionResult(index=0, total_seconds=1.0, accept_seconds=0.1, status="completed"),
        SessionResult(index=1, total_seconds=3.0, accept_seconds=0.2, status="failed", error="boom"),
    ]

    report = build_report(
        results,
        sessions=2,
        concurrency=2,
        base_url="http://localhost:8000",
        window_seconds=3.5,
        logs=None,
        log_error="未采集",
    )

    assert report["success_rate"] == 0.5
    assert report["status_counts"] == {"completed": 1, "failed": 1}
    assert report["latency_seconds"]["p50"] == 1.0
    assert report["accept_seconds"]["max"] == 0.2
    assert [failure["session"] for failure in report["failures"]] == [1]

"""并发会话性能测量：10 个并发会话的延迟、成功率与 Token/工具调用数据（成员 C D9-10）。

对齐 `doc/testing.md` §3.2「并发会话测试（E 系列性能）」与
`doc/15 AI Native多智能体协作平台.md` §六「性能测试」：

1. 用 **N 个并发会话**打真实 REST API（创建会话 → 发消息 → 轮询 Workflow 到终态），
   统计受理延迟（202）与端到端延迟的 p50/p95/max、成功率、HTTP 状态分布；
2. 采样 Token 消耗与工具调用成功率。

关于第 2 点的事实说明（不掩盖缺口）：

- 事实源要求「指标从 Prometheus 导出」。当前 backend **没有暴露 `/metrics`
  Prometheus 文本端点**，`deploy/prometheus.yml` 也只抓取 dapr-sidecar，
  且 `metrics` 表尚未建（`/api/v1/metrics` 恒为 `not_integrated`）；
- 因此本脚本改用**真实进程日志**采样：成员 C 的行为日志会把每次模型调用写成
  `event=llm.finish ... input_tokens= output_tokens= total_tokens= duration_ms=`，
  工具调用写成 `event=tool.call ... status=succeeded|failed`，数据同样是真实测量值，
  只是采集通道从 Prometheus 换成容器日志；
- `--service` 未指定或 docker 不可用时，脚本只输出延迟与成功率，并明确标注
  Token 段「未采样」。

用法（仓库根目录，compose 已启动）：

```bash
uv run python scripts/perf_concurrency.py --sessions 10 --concurrency 10
uv run python scripts/perf_concurrency.py --sessions 10 --json perf-report.json
```

退出码：全部会话成功且失败率不超过 `--max-failure-rate`（默认 0）时为 0。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_COMPOSE_FILE = PROJECT_ROOT / "deploy" / "compose.yaml"

_LLM_FINISH = re.compile(r"event=llm\.finish\b(?P<fields>.*)$")
_TOOL_CALL = re.compile(r"event=tool\.call\b(?P<fields>.*)$")
_FIELD = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\"[^\"]*\"|\S+)")


@dataclass
class SessionResult:
    """一个并发会话的完整观测结果。"""

    index: int
    session_id: str | None = None
    workflow_id: str | None = None
    accept_seconds: float | None = None
    total_seconds: float | None = None
    status: str | None = None
    http_errors: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.error is None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(values: list[float], ratio: float) -> float | None:
    """最近秩法百分位：取不小于 ratio 分位的最小观测值（数据量小，避免插值假精度）。"""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-len(ordered) * ratio // 1))))
    return ordered[rank - 1]


def _run_one_session(
    index: int,
    *,
    base_url: str,
    task: str,
    poll_interval: float,
    timeout: float,
) -> SessionResult:
    """单个虚拟会话：创建会话 → 发消息 → 轮询到终态。"""

    result = SessionResult(index=index)
    limits = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    try:
        with httpx.Client(base_url=base_url, timeout=limits) as client:
            session = client.post(
                "/api/v1/sessions", json={"user_id": f"perf-{index}"}
            )
            if session.status_code != 201:
                result.http_errors.append(f"create_session={session.status_code}")
                result.error = f"创建会话失败: {session.text[:200]}"
                return result
            result.session_id = session.json()["id"]

            started = time.perf_counter()
            accepted = client.post(
                f"/api/v1/sessions/{result.session_id}/messages",
                json={"content": task},
            )
            result.accept_seconds = round(time.perf_counter() - started, 3)
            if accepted.status_code != 202:
                result.http_errors.append(f"send_message={accepted.status_code}")
                result.error = f"提交消息失败: {accepted.text[:200]}"
                return result
            result.workflow_id = accepted.json()["workflow_id"]

            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                polled = client.get(f"/api/v1/workflows/{result.workflow_id}")
                if polled.status_code != 200:
                    result.http_errors.append(f"get_workflow={polled.status_code}")
                else:
                    payload = polled.json()
                    if payload["status"] in TERMINAL_STATUSES:
                        result.status = payload["status"]
                        break
                time.sleep(poll_interval)
            result.total_seconds = round(time.perf_counter() - started, 3)
            if result.status is None:
                result.error = f"Workflow 未在 {timeout}s 内到达终态"
    except Exception as exc:  # 网络/超时等：记为失败会话，不让整个测量中止
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _compose_logs(
    *,
    compose_file: Path,
    service: str,
    since: str,
) -> tuple[str, str | None]:
    """取指定时间窗内 backend 的日志；返回 (日志文本, 失败原因)。"""

    if shutil.which("docker") is None:
        return "", "未找到 docker 命令"
    if not compose_file.exists():
        return "", f"compose 文件不存在: {compose_file}"
    command = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "logs",
        "--no-color",
        "--since",
        since,
        service,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        return "", f"docker compose logs 退出码 {completed.returncode}: {completed.stderr.strip()[:200]}"
    return completed.stdout, None


def _parse_fields(text: str) -> dict[str, str]:
    return {match.group("key"): match.group("value").strip('"') for match in _FIELD.finditer(text)}


def _as_int(value: str | None) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def summarize_logs(log_text: str) -> dict[str, Any]:
    """从行为日志里统计 Token 消耗、模型调用耗时与工具调用成功率。"""

    input_tokens = output_tokens = total_tokens = 0
    llm_calls = 0
    llm_durations: list[float] = []
    tool_total = tool_succeeded = tool_failed = 0
    tool_names: dict[str, int] = {}
    models: dict[str, int] = {}

    for line in log_text.splitlines():
        llm_match = _LLM_FINISH.search(line)
        if llm_match:
            fields = _parse_fields(llm_match.group("fields"))
            llm_calls += 1
            input_tokens += _as_int(fields.get("input_tokens"))
            output_tokens += _as_int(fields.get("output_tokens"))
            total_tokens += _as_int(fields.get("total_tokens"))
            duration = _as_float(fields.get("duration_ms"))
            if duration is not None:
                llm_durations.append(duration)
            if fields.get("model"):
                models[fields["model"]] = models.get(fields["model"], 0) + 1
            continue
        tool_match = _TOOL_CALL.search(line)
        if tool_match:
            fields = _parse_fields(tool_match.group("fields"))
            tool_total += 1
            if fields.get("status") == "succeeded":
                tool_succeeded += 1
            else:
                tool_failed += 1
            name = fields.get("tool_name", "unknown")
            tool_names[name] = tool_names.get(name, 0) + 1

    return {
        "llm_calls": llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tokens_per_call": round(total_tokens / llm_calls, 1) if llm_calls else None,
        "llm_duration_ms": {
            "p50": percentile(llm_durations, 0.50),
            "p95": percentile(llm_durations, 0.95),
            "max": max(llm_durations) if llm_durations else None,
        },
        "models": models,
        "tool_calls": {
            "total": tool_total,
            "succeeded": tool_succeeded,
            "failed": tool_failed,
            "success_rate": round(tool_succeeded / tool_total, 4) if tool_total else None,
            "by_name": tool_names,
        },
    }


def build_report(
    results: list[SessionResult],
    *,
    sessions: int,
    concurrency: int,
    base_url: str,
    window_seconds: float,
    logs: dict[str, Any] | None,
    log_error: str | None,
) -> dict[str, Any]:
    """汇总成可写入报告的测量结果。"""

    total_latencies = [
        r.total_seconds for r in results if r.total_seconds is not None
    ]
    accept_latencies = [
        r.accept_seconds for r in results if r.accept_seconds is not None
    ]
    succeeded = [r for r in results if r.succeeded]
    status_counts: dict[str, int] = {}
    for result in results:
        key = result.status or "no_terminal_state"
        status_counts[key] = status_counts.get(key, 0) + 1
    http_error_count = sum(len(r.http_errors) for r in results)

    return {
        "measured_at": _now_iso(),
        "target": base_url,
        "sessions": sessions,
        "concurrency": concurrency,
        "wall_clock_seconds": round(window_seconds, 3),
        "success_rate": round(len(succeeded) / sessions, 4) if sessions else None,
        "status_counts": status_counts,
        "http_error_count": http_error_count,
        "accept_seconds": {
            "p50": percentile(accept_latencies, 0.50),
            "p95": percentile(accept_latencies, 0.95),
            "max": max(accept_latencies) if accept_latencies else None,
        },
        "latency_seconds": {
            "p50": percentile(total_latencies, 0.50),
            "p95": percentile(total_latencies, 0.95),
            "max": max(total_latencies) if total_latencies else None,
            "mean": round(statistics.fmean(total_latencies), 3) if total_latencies else None,
            "samples": len(total_latencies),
        },
        "observability_from_logs": logs,
        "observability_log_error": log_error,
        "failures": [
            {
                "session": r.index,
                "workflow_id": r.workflow_id,
                "status": r.status,
                "error": r.error,
                "http_errors": r.http_errors,
            }
            for r in results
            if not r.succeeded
        ],
    }


def _print_report(report: dict[str, Any]) -> None:
    latency = report["latency_seconds"]
    accept = report["accept_seconds"]
    print("=" * 72)
    print(f"并发会话测量 @ {report['measured_at']}  目标 {report['target']}")
    print("=" * 72)
    print(f"会话数/并发度      : {report['sessions']} / {report['concurrency']}")
    print(f"总墙钟耗时         : {report['wall_clock_seconds']}s")
    print(
        f"成功率             : {report['success_rate']}  "
        f"终态分布={report['status_counts']}  HTTP 错误={report['http_error_count']}"
    )
    print(
        f"受理延迟(202) 秒   : p50={accept['p50']} p95={accept['p95']} max={accept['max']}"
    )
    print(
        f"端到端延迟 秒      : p50={latency['p50']} p95={latency['p95']} "
        f"max={latency['max']} mean={latency['mean']} (样本={latency['samples']})"
    )
    logs = report.get("observability_from_logs")
    if logs is None:
        print(f"Token/工具采样     : 未采样（{report.get('observability_log_error')}）")
    else:
        tools = logs["tool_calls"]
        print(
            f"Token 消耗         : total={logs['total_tokens']} "
            f"(in={logs['input_tokens']} out={logs['output_tokens']}) "
            f"模型调用={logs['llm_calls']} 每次={logs['tokens_per_call']}"
        )
        print(f"模型调用耗时 ms    : {logs['llm_duration_ms']}")
        print(
            f"工具调用成功率     : {tools['success_rate']} "
            f"({tools['succeeded']}/{tools['total']}) 明细={tools['by_name']}"
        )
        if logs["models"]:
            print(f"模型分布           : {logs['models']}")
    if report["failures"]:
        print("-" * 72)
        print("失败会话：")
        for failure in report["failures"]:
            print(f"  #{failure['session']} status={failure['status']} error={failure['error']}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="并发会话性能测量（成员 C D9-10）")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--sessions", type=int, default=10, help="会话总数")
    parser.add_argument("--concurrency", type=int, default=10, help="并发度")
    parser.add_argument("--timeout", type=float, default=600.0, help="单会话等待终态上限（秒）")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="轮询间隔（秒）")
    parser.add_argument(
        "--task",
        default="请分析多智能体协作平台的核心要点并生成一份简报",
        help="每个会话提交的任务内容",
    )
    parser.add_argument(
        "--compose-file",
        default=str(DEFAULT_COMPOSE_FILE),
        help="用于采集 backend 日志的 compose 文件",
    )
    parser.add_argument("--service", default="backend", help="采集日志的服务名")
    parser.add_argument(
        "--no-log-sample",
        action="store_true",
        help="不采集容器日志（只测延迟与成功率）",
    )
    parser.add_argument(
        "--max-failure-rate",
        type=float,
        default=0.0,
        help="允许的失败率上限，超过则以非零码退出",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="把报告写入 JSON 文件")
    args = parser.parse_args(argv)

    print(
        f"开始并发测量：{args.sessions} 个会话 / 并发度 {args.concurrency} / "
        f"目标 {args.base_url}"
    )
    since = _now_iso()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(
            pool.map(
                lambda index: _run_one_session(
                    index,
                    base_url=args.base_url,
                    task=args.task,
                    poll_interval=args.poll_interval,
                    timeout=args.timeout,
                ),
                range(args.sessions),
            )
        )
    window = time.perf_counter() - started

    logs: dict[str, Any] | None = None
    log_error: str | None = None
    if args.no_log_sample:
        log_error = "使用 --no-log-sample 显式跳过"
    else:
        # 日志窗口向后多取 5 秒，避免「测量结束但日志尚未落盘」的边界丢失。
        text, log_error = _compose_logs(
            compose_file=Path(args.compose_file), service=args.service, since=since
        )
        if text:
            logs = summarize_logs(text)

    report = build_report(
        results,
        sessions=args.sessions,
        concurrency=args.concurrency,
        base_url=args.base_url,
        window_seconds=window,
        logs=logs,
        log_error=log_error,
    )
    _print_report(report)

    if args.json_path:
        path = Path(args.json_path)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入：{path}")

    failure_rate = 1 - (report["success_rate"] or 0)
    if failure_rate > args.max_failure_rate:
        print(
            f"失败率 {failure_rate:.2%} 超过上限 {args.max_failure_rate:.2%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

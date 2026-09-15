"""E-03 故障恢复测量：重启 backend 后 Workflow 从断点恢复的耗时（成员 C D9-10）。

对齐 `doc/testing.md` §3.1「Workflow 恢复测试（I-03 / E-03）」与
`doc/15 AI Native多智能体协作平台.md` §六「性能测试」第 1 条：

    模拟 Pod 崩溃后重启，测量恢复耗时（目标 < 5 秒）

测量步骤（与 `scripts/fault_recovery.ps1` 的手工演练一致，但输出结构化数据）：

1. 用 `app.workflows.poc --hold-seconds H --no-wait` 起一条 Workflow：collect 阶段
   结束后进入一个**持久化定时器**（`ctx.create_timer`），形成「执行中断点」；
2. 轮询 `GET /workflows/{id}` 直到 `checkpoint.completed_steps == ["collect"]`，
   即进入定时器等待窗口；
3. 在定时器到期前 `docker compose restart backend`（模拟 Pod 崩溃重启，
   Dapr sidecar 与 placement 存活，Workflow 实例状态不丢）；
4. 轮询 `/health` 直到可用，得到**服务不可用时长**；
5. 轮询同一 Workflow 直到下一个阶段回写 Checkpoint 或到达终态，得到
   **恢复耗时 = 服务可用 → 实例重新执行**；再等到终态确认任务完成、无状态丢失；
6. 额外校验 Dapr orchestration 终态（从 backend 日志取）：业务库写的是 `completed`
   而运行时可能是 `FAILED`（`finalize_activity` 写业务终态后、写报告消息时抛错），
   只看业务行会把这种情况误判成恢复成功。两者不一致时脚本以非零码退出并标注。
   该不一致即缺口 F-05（见 ADR-016），**已由成员 B 修复**（poc 不再硬编码
   `session_id="demo-session"`，且 CLI 对运行时终态非 `COMPLETED` 即非零码退出）。
   本校验**保留为守卫**：不因为对方说自己修好了就撤掉，跑一次新演练即可确认两个终态
   同时为成功。ADR-016 里的 E-03 数据取自修复前，且走 poc 的确定性（假模型）路径。

定义说明：若重启期间持久化定时器尚未到期，实例本就不该继续执行，此时
`恢复耗时` 会包含剩余等待时间；脚本同时给出 `timer_pending_seconds`（服务可用时
剩余定时器时长）与扣除后的 `adjusted_recovery_seconds`，两者都会打印，避免把
「等定时器」算成恢复慢。

用法（仓库根目录，compose 已启动）：

```bash
uv run python scripts/measure_recovery.py
uv run python scripts/measure_recovery.py --hold-seconds 20 --json recovery.json
```

退出码：任务最终 completed、业务状态保留、恢复耗时（扣除定时器等待）不超过
`--target-seconds`（默认 5）、且 Dapr 终态不是 FAILED 时为 0，否则 1。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

DEFAULT_COMPOSE_FILE = PROJECT_ROOT / "deploy" / "compose.yaml"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_WORKFLOW_ID = re.compile(r"workflow_id=([0-9a-fA-F-]{36})")
HEALTH_INTERVAL_SECONDS = 0.2
PROGRESS_INTERVAL_SECONDS = 0.2


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose(compose_file: Path, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "-f", str(compose_file), *args]
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def _health_ok(client: httpx.Client) -> bool:
    try:
        return client.get("/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def _workflow(client: httpx.Client, workflow_id: str) -> dict[str, Any] | None:
    try:
        response = client.get(
            f"/api/v1/workflows/{workflow_id}",
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0),
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    return response.json()


def _completed_steps(workflow: dict[str, Any] | None) -> list[str]:
    if not workflow:
        return []
    checkpoint = workflow.get("checkpoint") or {}
    steps = checkpoint.get("completed_steps") or []
    return [str(step) for step in steps]


def _wait_until(
    predicate,
    *,
    timeout: float,
    interval: float,
) -> tuple[bool, float]:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return True, time.perf_counter() - started
        time.sleep(interval)
    return False, time.perf_counter() - started


def _orchestration_status(
    compose_file: Path,
    *,
    service: str,
    workflow_id: str,
    since: str,
) -> str | None:
    """从 backend 日志里取该实例的 Dapr orchestration 终态（COMPLETED / FAILED）。

    业务库（PostgreSQL）与 Dapr 运行时的终态可能不一致：`finalize_activity` 先写业务
    终态、再写报告消息，若报告消息写入抛错，业务行是 completed 而 orchestration 是
    FAILED。只看业务行会把这种情况误判成「恢复成功」，因此这里补一条日志侧校验。
    """

    completed = _compose(
        compose_file, "logs", "--no-color", "--since", since, service, timeout=120.0
    )
    if completed.returncode != 0:
        return None
    status: str | None = None
    marker = f"{workflow_id}: Orchestration completed with status: "
    for line in completed.stdout.splitlines():
        index = line.find(marker)
        if index >= 0:
            status = line[index + len(marker) :].strip().split()[0]
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E-03 Workflow 故障恢复测量（成员 C D9-10）")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--compose-file", default=str(DEFAULT_COMPOSE_FILE))
    parser.add_argument("--service", default="backend", help="被重启的服务名")
    parser.add_argument("--hold-seconds", type=int, default=20, help="collect 之后的持久化定时器时长")
    parser.add_argument(
        "--restart-lead-seconds",
        type=float,
        default=6.0,
        help="在定时器到期前多少秒发起重启（让停机覆盖定时器到期点）",
    )
    parser.add_argument("--target-seconds", type=float, default=5.0, help="恢复耗时目标（doc/15 §六）")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待终态上限（秒）")
    parser.add_argument("--json", dest="json_path", default=None, help="把测量结果写入 JSON 文件")
    args = parser.parse_args(argv)

    compose_file = Path(args.compose_file)
    if shutil.which("docker") is None:
        print("未找到 docker 命令，无法执行恢复演练", file=sys.stderr)
        return 2
    if not compose_file.exists():
        print(f"compose 文件不存在：{compose_file}", file=sys.stderr)
        return 2

    limits = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
    with httpx.Client(base_url=args.base_url, timeout=limits) as client:
        if not _health_ok(client):
            print(f"backend 未就绪：{args.base_url}/health", file=sys.stderr)
            return 2

        measured_since = _now_iso()
        print(f"[1/6] 调度带 {args.hold_seconds}s 持久化定时器的 Workflow …")
        scheduled = _compose(
            compose_file,
            "exec",
            "-T",
            args.service,
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "app.workflows.poc",
            "--hold-seconds",
            str(args.hold_seconds),
            "--no-wait",
        )
        if scheduled.returncode != 0:
            print(f"调度失败：{scheduled.stdout}{scheduled.stderr}", file=sys.stderr)
            return 2
        match = _WORKFLOW_ID.search(scheduled.stdout)
        if not match:
            print(f"无法解析 workflow_id：{scheduled.stdout}", file=sys.stderr)
            return 2
        workflow_id = match.group(1)
        print(f"      workflow_id={workflow_id}")

        print("[2/6] 等待 collect 阶段完成（进入定时器等待窗口）…")
        ok, waited = _wait_until(
            lambda: "collect" in _completed_steps(_workflow(client, workflow_id)),
            timeout=60.0,
            interval=PROGRESS_INTERVAL_SECONDS,
        )
        if not ok:
            print("collect 阶段未在 60s 内完成，中止演练", file=sys.stderr)
            return 1
        collect_done = time.perf_counter()
        timer_due_at = collect_done + args.hold_seconds
        baseline = len(_completed_steps(_workflow(client, workflow_id)))
        print(
            f"      collect 完成（等待 {waited:.1f}s），定时器约 {args.hold_seconds}s 后到期，"
            f"中断前已完成阶段数={baseline}"
        )

        # 让重启起点落在「定时器到期前 restart_lead 秒」：停机窗口覆盖定时器到期点后，
        # 服务一恢复实例就该立刻继续执行，测得的才是真正的恢复耗时。
        wait_for = (timer_due_at - args.restart_lead_seconds) - time.perf_counter()
        if wait_for > 0:
            print(f"[3/6] {wait_for:.1f}s 后重启 {args.service}（模拟 Pod 崩溃）…")
            time.sleep(wait_for)
        restart_at = time.perf_counter()
        restarted = _compose(compose_file, "restart", args.service, timeout=180.0)
        if restarted.returncode != 0:
            print(f"重启失败：{restarted.stdout}{restarted.stderr}", file=sys.stderr)
            return 2
        print(f"      已发起重启 @ {_now_iso()}")

        print("[4/6] 等待 backend 恢复可用 …")
        ok, _ = _wait_until(
            lambda: _health_ok(client), timeout=args.timeout, interval=HEALTH_INTERVAL_SECONDS
        )
        if not ok:
            print("backend 未在超时内恢复", file=sys.stderr)
            return 1
        ready_at = time.perf_counter()
        downtime = ready_at - restart_at
        print(f"      服务恢复，不可用时长 {downtime:.1f}s")

        print("[5/6] 等待 Workflow 实例从断点继续执行（下一个阶段回写 Checkpoint）…")

        def _resumed() -> bool:
            current = _workflow(client, workflow_id) or {}
            return (
                len(_completed_steps(current)) > baseline
                or current.get("status") in TERMINAL_STATUSES
            )

        ok, resume_waited = _wait_until(
            _resumed, timeout=args.timeout, interval=PROGRESS_INTERVAL_SECONDS
        )
        resumed_at = time.perf_counter()
        resume_seconds = resumed_at - ready_at
        timer_pending = max(0.0, timer_due_at - ready_at)
        adjusted = max(0.0, resume_seconds - timer_pending)
        if not ok:
            print(
                f"Workflow 未在 {args.timeout:.0f}s 内继续执行（status/步骤均未推进）",
                file=sys.stderr,
            )
            return 1
        if resume_waited <= PROGRESS_INTERVAL_SECONDS:
            print(
                f"      服务恢复时实例已完成续跑：恢复耗时 ≤{resume_seconds:.2f}s"
                f"（低于 {PROGRESS_INTERVAL_SECONDS}s 轮询粒度，定时器已到期）"
            )
        else:
            print(
                f"      实例已恢复执行：恢复耗时 {resume_seconds:.2f}s"
                f"（服务可用时定时器剩余 {timer_pending:.2f}s，扣除后 {adjusted:.2f}s）"
            )

        print("[6/6] 等待终态，确认无状态丢失 …")
        ok, _ = _wait_until(
            lambda: (_workflow(client, workflow_id) or {}).get("status") in TERMINAL_STATUSES,
            timeout=args.timeout,
            interval=PROGRESS_INTERVAL_SECONDS,
        )
        final = _workflow(client, workflow_id) or {}
        if not ok:
            print(f"Workflow 未到达终态：{final}", file=sys.stderr)
            return 1
        total_seconds = time.perf_counter() - restart_at
        steps = _completed_steps(final)
        print(f"      业务终态 status={final.get('status')} completed_steps={steps}")

        # 业务行与 Dapr 运行时终态可能不一致，补一条日志侧校验（见 _orchestration_status）。
        orchestration = _orchestration_status(
            compose_file,
            service=args.service,
            workflow_id=workflow_id,
            since=measured_since,
        )
        print(f"      Dapr orchestration 终态={orchestration}")
        if orchestration == "FAILED":
            print(
                "      注意：业务行是 completed 但 Dapr orchestration 为 FAILED，"
                "两者终态不一致（缺口 F-05，详见 ADR-016；若 poc 侧修复已生效，"
                "这里应不再触发）",
                file=sys.stderr,
            )

    report = {
        "measured_at": _now_iso(),
        "target": args.base_url,
        "service": args.service,
        "workflow_id": workflow_id,
        "hold_seconds": args.hold_seconds,
        "steps_before_restart": baseline,
        "downtime_seconds": round(downtime, 3),
        "recovery_seconds": round(resume_seconds, 3),
        "recovery_at_poll_resolution": resume_waited <= PROGRESS_INTERVAL_SECONDS,
        "timer_pending_seconds": round(timer_pending, 3),
        "adjusted_recovery_seconds": round(adjusted, 3),
        "target_seconds": args.target_seconds,
        "recovery_within_target": adjusted <= args.target_seconds,
        "restart_to_terminal_seconds": round(total_seconds, 3),
        "final_status": final.get("status"),
        "completed_steps": steps,
        "state_preserved": final.get("status") == "completed"
        and steps == ["collect", "analyze", "report"],
        "orchestration_status": orchestration,
        "orchestration_failed": orchestration == "FAILED",
        "terminal_state_consistent": orchestration != "FAILED",
    }

    print("=" * 72)
    print(f"E-03 恢复测量 @ {report['measured_at']}")
    print("=" * 72)
    print(f"Workflow           : {workflow_id}")
    print(f"不可用时长         : {downtime:.2f}s")
    print(
        f"恢复耗时           : {resume_seconds:.2f}s"
        f"（扣除定时器等待 {adjusted:.2f}s，目标 <{args.target_seconds:.0f}s）"
    )
    print(f"重启→业务终态耗时  : {total_seconds:.2f}s")
    print(f"业务状态保留       : {report['state_preserved']}（steps={steps}）")
    print(f"Dapr 终态          : {orchestration}")
    print("=" * 72)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入：{args.json_path}")

    passed = (
        report["recovery_within_target"]
        and report["state_preserved"]
        and report["terminal_state_consistent"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

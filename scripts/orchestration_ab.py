"""静态 vs 动态编排的净收益度量（成员 D）。

## 为什么需要它

动态编排（档 2，ADR-019）让规划节点自己决定「谁参与、以什么顺序参与」。这是一个
**有成本**的能力：每次执行多一次规划调用。所以「动态更好」不能靠感觉说，得给出：

- **成本**：墙钟耗时、模型调用次数、输入/输出 token；
- **产出**：执行了几个步骤、最终交付物长度；
- **正确性**：任务声明的必备内容是否出现在交付物里（`must_include`）。

只有三项一起看才有意义——便宜一半但答错的编排不是"净收益"。

## 口径说明

- 两条路径都是**进程内直跑**（`run_multi_agent_pipeline` / `run_dynamic_pipeline`），
  不经 Dapr，避免把工作流调度开销算进编排差异；Dapr 链路的开销另有 `scripts/measure_recovery.py`。
- token 取自平台自己的指标采集器（`app/observability`），不是从响应里另算一份——
  这样量到的就是平台实际记录的值。
- 每个组合默认重复一次；`--repeat` 调大后报告取**均值**，并给出每次的原始值，
  单次采样的抖动不会被均值掩掉。
- **必须给模型调用之间留间隔**（`--min-interval`，默认 6 秒）：本机网关（`localhost:3000`，
  one-api 系）在**紧接着**上一次请求结束就发下一次时，必然在 ~1.4s 内失败
  （500 `do_request_failed`）；隔 6 秒再发就成功。实测过 A→B（立刻）→C（隔 6s）：
  A 成功、B 失败、C 成功；**新建客户端也一样**，所以不是连接复用，是网关侧节流。
  评测脚本用 `GatewayThrottle` 显式等待，并**把等待时间与模型纯耗时分开记**——
  否则量到的"耗时"是网关限流，不是编排差异。
- **每次模型调用都会重试**（`--retry`，默认 2 次）。一次失败的调用若混进 token 对比表，
  会得出"动态更便宜"这类错结论。但只有调用**报错**才重试：模型正常返回、只是答得不好，
  那是有意义的观测，不该被重试抹掉。

用法：

    MACP_AB_LIVE=1 uv run python scripts/orchestration_ab.py --out doc/evals/orchestration-ab.md

跑完会把**原始行**另写一份 `<--out>.rows.json`。真实模型跑批要 20 分钟且会产生费用，
所以**改报告措辞时用离线重渲染**，不要再跑一遍模型：

    python scripts/orchestration_ab.py \
      --from-rows doc/evals/orchestration-ab.rows.json --out doc/evals/orchestration-ab.md
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODES = ("static", "dynamic")

TOKEN_METRICS = ("input_tokens", "output_tokens", "total_tokens")


@dataclass(frozen=True)
class AbTask:
    """一个对比任务：任务文本 + 「合格交付物必须提到的内容」。"""

    key: str
    task: str
    must_include: tuple[str, ...]
    note: str = ""


TASKS: tuple[AbTask, ...] = (
    AbTask(
        key="single_question",
        task="用两句话说明多智能体协作平台解决什么问题。",
        must_include=("智能体",),
        note="单点问题：动态多花一次规划调用，理论上应是净亏",
    ),
    AbTask(
        key="collect_then_summarize",
        task="先把下面这段需求里的关键约束逐条列出，再据此给出一句话结论：系统需支持四人协作、每人一条独立分支、最终合并到主干、并且每次提交都要能追溯。",
        must_include=("分支", "主干"),
        note="收集 + 归纳：动态有机会把两步拆开",
    ),
    AbTask(
        key="two_independent_sources",
        task="分别从「性能」与「安全」两个角度评估沙箱方案，最后合并成一段结论。",
        must_include=("性能", "安全"),
        note="两路独立收集：静态三步图只有一个 collect，动态可以放两步",
    ),
    AbTask(
        key="analysis_only",
        task="给定结论「构建退出码为 0 但样式丢失」，分析最可能的原因并给出验证方法。",
        must_include=("原因", "验证"),
        note="偏分析：静态的 collect 步骤可能纯属浪费",
    ),
)


@dataclass
class RunStats:
    task: AbTask
    mode: str
    seconds: float = 0.0
    steps: int = 0
    model_calls: int = 0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    output_chars: int = 0
    output: str = ""
    plan_source: str = ""
    error: str | None = None
    throttle_wait: float = 0.0
    model_seconds: float = 0.0
    retried: int = 0
    quality_ok: bool | None = None
    """离线重渲染时用来还原判定——交付物正文不入库，`quality` 无法重算。"""
    metric_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_tokens(self) -> float:
        return self.input_tokens + self.output_tokens

    @property
    def quality(self) -> bool:
        if self.quality_ok is not None:
            return self.quality_ok
        if self.error is not None or not self.output:
            return False
        return all(item in self.output for item in self.task.must_include)


class _NullSink:
    """不落库的指标出口：度量脚本不该往开发库写采样。"""

    def write(self, samples: Sequence[Any]) -> None:  # noqa: ANN401 - 与 MetricSink 协议一致
        return None


def _run_static(task: AbTask, llm: Any, *, max_steps: int) -> RunStats:
    from app.orchestration.pipeline import PipelineStage
    from app.orchestration.pipeline_graph import run_multi_agent_pipeline

    state = run_multi_agent_pipeline(task.task, llm=llm)
    report = state.results.get(PipelineStage.REPORT.value) or {}
    content = report.get("content") if isinstance(report, dict) else ""
    return RunStats(
        task=task,
        mode="static",
        steps=len(state.completed_steps),
        output=str(content or ""),
        plan_source="fixed",
    )


def _run_dynamic(task: AbTask, llm: Any, *, max_steps: int) -> RunStats:
    from app.orchestration.dynamic_graph import final_output, run_dynamic_pipeline
    from app.orchestration.pipeline import PipelineStatus

    state = run_dynamic_pipeline(task.task, llm=llm, max_steps=max_steps)
    # 动态链路**不抛异常**：失败被记进 state（`dynamic.finish status=failed`），
    # 没有可用交付物时终态才是 failed（ADR-019 决策 3）。所以这里必须显式转成异常，
    # 否则 `measure` 的重试逻辑失效，一次上游 500 会被记成"便宜的一次运行"。
    if state.status is PipelineStatus.FAILED:
        detail = state.error or "；".join(
            f"{step}：{outcome.error}" for step, outcome in state.results.items() if outcome.error
        )
        raise RuntimeError(f"动态执行终态 failed：{detail or '没有可用交付物'}")
    return RunStats(
        task=task,
        mode="dynamic",
        steps=len(state.results),
        output=str(final_output(state) or ""),
        plan_source=str(state.plan_source),
    )


class GatewayThrottle:
    """给相邻两次模型调用之间强制留间隔，并分别累计「等待」与「模型耗时」。

    为什么必须有它：本机网关（one-api 系）在**紧接着**上一次请求结束就发下一次时，
    必然在 ~1.4s 内失败（500 `do_request_failed`）。A→B(立刻)→C(隔 6s) 实测为
    「成功 / 失败 / 成功」，且**新建客户端也失败**——不是连接复用，是网关侧节流。

    没有它，编排 A/B 量到的全是"哪一次调用撞上节流"，而不是编排差异。

    两个累计量分开记，是为了让报告能说清一件事：墙钟时间被网关节流污染了，
    而**模型纯耗时**与 **token** 没有被污染，后者才是可比的口径。
    """

    def __init__(self, min_interval: float, retries: int) -> None:
        self._min_interval = max(0.0, min_interval)
        self._retries = max(0, retries)
        self._last_finished = 0.0
        self.wait_seconds = 0.0
        self.model_seconds = 0.0
        self.calls = 0
        self.retried = 0

    def _wait(self) -> None:
        if self._min_interval <= 0:
            return
        remaining = self._min_interval - (time.perf_counter() - self._last_finished)
        if remaining > 0:
            time.sleep(remaining)
            self.wait_seconds += remaining

    def invoke(self, runnable: Any, messages: Any, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            self._wait()
            started = time.perf_counter()
            try:
                result = runnable.invoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 报错才重试，见模块 docstring
                last = exc
                self._last_finished = time.perf_counter()
                self.model_seconds += self._last_finished - started
                if attempt < self._retries:
                    self.retried += 1
                continue
            self._last_finished = time.perf_counter()
            self.model_seconds += self._last_finished - started
            self.calls += 1
            return result
        assert last is not None
        raise last


class ThrottledModel:
    """`BaseChatModel` 的最小鸭子类型替身：只做节流与重试，其余原样透传。

    `_invoke_role` 只用到 `bind_tools()` 与 `invoke()`（`app/orchestration/pipeline_graph.py`），
    所以包这两个方法就够；其余属性走 `__getattr__` 转发，避免替身比真身少东西时
    在别处报出无关的错误。
    """

    def __init__(self, inner: Any, throttle: GatewayThrottle) -> None:
        self._inner = inner
        self._throttle = throttle

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        return self._throttle.invoke(self._inner, messages, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> ThrottledModel:
        return ThrottledModel(self._inner.bind_tools(tools, **kwargs), self._throttle)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def measure(
    task: AbTask,
    mode: str,
    *,
    llm: Any,
    max_steps: int,
    min_interval: float = 6.0,
    retries: int = 2,
) -> RunStats:
    """跑一次并采集指标。

    - **每次都用全新的指标采集器**，避免把上一次的采样算进来；
    - 每次调用前先过 `GatewayThrottle`（间隔 + 报错重试）；
    - 跑出来的异常记进 `error` 而**不再在外层重跑**：外层重跑会把"整段执行"重来一遍，
      与"某一次调用重试"是两回事，混在一起会让 wall clock 无法解释。
    """

    from app.observability.metrics import MetricsCollector, set_metrics_collector

    collector = MetricsCollector(sink=_NullSink(), enabled=True)
    set_metrics_collector(collector)
    throttle = GatewayThrottle(min_interval, retries)
    model = ThrottledModel(llm, throttle)

    started = time.perf_counter()
    try:
        stats = _run_static(task, model, max_steps=max_steps) if mode == "static" else _run_dynamic(
            task, model, max_steps=max_steps
        )
    except Exception as exc:  # noqa: BLE001 - 度量脚本必须把失败记录成"这一次不成立"
        stats = RunStats(task=task, mode=mode, error=f"{type(exc).__name__}: {exc}")
    stats.seconds = round(time.perf_counter() - started, 2)
    stats.throttle_wait = round(throttle.wait_seconds, 2)
    stats.model_seconds = round(throttle.model_seconds, 2)
    stats.retried = throttle.retried

    samples = list(collector.pending())
    stats.metric_names = tuple(sorted({sample.metric_name for sample in samples}))
    for sample in samples:
        if sample.metric_name == "input_tokens":
            stats.input_tokens += float(sample.value)
        elif sample.metric_name == "output_tokens":
            stats.output_tokens += float(sample.value)
    # 调用次数以**真实发生的调用**为准：指标采样来自平台内部记录，重试的那几次不会各自
    # 落一条成功采样，用它数会少算。
    stats.model_calls = throttle.calls
    stats.output_chars = len(stats.output)
    return stats


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


TASKS_BY_KEY = {task.key: task for task in TASKS}


def rows_to_payload(
    rows: list[RunStats],
    *,
    model: str,
    base_url: str,
    repeat: int,
    min_interval: float,
) -> dict[str, Any]:
    """把原始行连同运行参数一起序列化。

    存原始行而不是只存渲染好的 Markdown，是为了**改报告模板时不必再花一次模型开销**：
    真实模型跑批要 20 分钟且产生费用，报告措辞却会反复改。
    """

    return {
        "meta": {
            "model": model,
            "base_url": base_url,
            "repeat": repeat,
            "min_interval": min_interval,
            "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        },
        "rows": [
            {
                "task": row.task.key,
                "mode": row.mode,
                "seconds": row.seconds,
                "model_seconds": row.model_seconds,
                "throttle_wait": row.throttle_wait,
                "steps": row.steps,
                "model_calls": row.model_calls,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "output_chars": row.output_chars,
                "plan_source": row.plan_source,
                "error": row.error,
                "retried": row.retried,
                "quality": row.quality,
                "metric_names": list(row.metric_names),
            }
            for row in rows
        ],
    }


def rows_from_payload(payload: dict[str, Any]) -> tuple[list[RunStats], dict[str, Any]]:
    rows: list[RunStats] = []
    for item in payload.get("rows", []):
        task = TASKS_BY_KEY.get(str(item.get("task")))
        if task is None:
            continue
        rows.append(
            RunStats(
                task=task,
                mode=str(item.get("mode")),
                seconds=float(item.get("seconds") or 0.0),
                model_seconds=float(item.get("model_seconds") or 0.0),
                throttle_wait=float(item.get("throttle_wait") or 0.0),
                steps=int(item.get("steps") or 0),
                model_calls=int(item.get("model_calls") or 0),
                input_tokens=float(item.get("input_tokens") or 0.0),
                output_tokens=float(item.get("output_tokens") or 0.0),
                output_chars=int(item.get("output_chars") or 0),
                # 交付物正文不入库（可能很长），所以离线重渲染时「必备内容命中」直接沿用
                # 当时算出的布尔值，而不是拿空正文重算一遍。
                quality_ok=bool(item["quality"]) if "quality" in item else None,
                plan_source=str(item.get("plan_source") or ""),
                error=item.get("error"),
                retried=int(item.get("retried") or 0),
                metric_names=tuple(str(name) for name in item.get("metric_names") or ()),
            )
        )
    return rows, dict(payload.get("meta") or {})


def report(
    rows: list[RunStats],
    *,
    model: str,
    base_url: str,
    repeat: int,
    min_interval: float,
    recorded_at: str | None = None,
) -> str:
    # 离线重渲染时**必须**用当时的记录时间：用 `now()` 会让报告声称这次跑批发生在改模板的时刻。
    timestamp = recorded_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "# 编排模式净收益度量（静态 vs 动态）",
        "",
        f"- 运行时间：{timestamp}",
        f"- 模型：`{model}`（base_url=`{base_url}`）",
        f"- 每个组合重复 {repeat} 次，表中为均值",
        "- 两条路径都在进程内直跑，不含 Dapr 调度开销",
        f"- 相邻模型调用之间强制间隔 {min_interval}s（网关节流，见下「口径」）；"
        f"调用报错最多重试，实际发生重试 {sum(row.retried for row in rows)} 次",
        "",
        "| 任务 | 模式 | 墙钟(s) | 模型纯耗时(s) | 节流等待(s) | 步骤 | 模型调用 | "
        "输入 token | 输出 token | 交付物字符 | 必备内容 | 计划来源 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in TASKS:
        for mode in MODES:
            group = [row for row in rows if row.task.key == task.key and row.mode == mode]
            if not group:
                continue
            ok = sum(1 for row in group if row.quality)
            failures = [row.error for row in group if row.error]
            mark = f"{ok}/{len(group)}" + (f"（{failures[0][:40]}）" if failures else "")
            lines.append(
                "| {task} | {mode} | {seconds} | {model_seconds} | {wait} | {steps} | {calls} | "
                "{input_tokens} | {output_tokens} | {chars} | {mark} | {source} |".format(
                    task=task.key,
                    mode=mode,
                    seconds=_mean([row.seconds for row in group]),
                    model_seconds=_mean([row.model_seconds for row in group]),
                    wait=_mean([row.throttle_wait for row in group]),
                    steps=_mean([float(row.steps) for row in group]),
                    calls=_mean([float(row.model_calls) for row in group]),
                    input_tokens=_mean([row.input_tokens for row in group]),
                    output_tokens=_mean([row.output_tokens for row in group]),
                    chars=_mean([float(row.output_chars) for row in group]),
                    mark=mark,
                    source="、".join(sorted({row.plan_source for row in group})) or "—",
                )
            )

    lines += ["", "## 合计与结论", ""]
    for mode in MODES:
        group = [row for row in rows if row.mode == mode]
        if not group:
            continue
        passed = sum(1 for row in group if row.quality)
        lines.append(
            f"- **{mode}**：模型纯耗时 {_mean([row.model_seconds for row in group])}s/次，"
            f"{_mean([row.input_tokens + row.output_tokens for row in group])} token/次，"
            f"{_mean([float(row.steps) for row in group])} 步，"
            f"{_mean([float(row.model_calls) for row in group])} 次模型调用，"
            f"必备内容命中 {passed}/{len(group)}"
        )

    static_tokens = [row.total_tokens for row in rows if row.mode == "static"]
    dynamic_tokens = [row.total_tokens for row in rows if row.mode == "dynamic"]
    static_calls = [float(row.model_calls) for row in rows if row.mode == "static"]
    dynamic_calls = [float(row.model_calls) for row in rows if row.mode == "dynamic"]
    if static_tokens and dynamic_tokens:
        delta = _mean(dynamic_tokens) - _mean(static_tokens)
        share = delta / _mean(static_tokens) * 100 if _mean(static_tokens) else 0.0
        call_delta = _mean(dynamic_calls) - _mean(static_calls)
        lines += [
            "",
            f"动态相对静态的 token 差：**{delta:+.0f}/次（{share:+.1f}%）**；"
            f"模型调用次数差：**{call_delta:+.1f} 次/次**。",
            "token 差为正表示动态更贵。规划调用的成本就体现在这两个差值里；",
            "但只有「必备内容命中」也持平或更高时，这个成本才是可接受的。",
        ]

    names = sorted({name for row in rows for name in row.metric_names})
    if names:
        lines += ["", "## 采集到的指标名（来自 `app/observability`）", "", ", ".join(f"`{n}`" for n in names)]

    # 按任务看「谁更省」。合计那一栏会被任务难度差异掩盖：单点任务上动态便宜一大截，
    # 复杂任务上动态贵一大截，两者相加得到的"平均"说明不了任何一件事。
    lines += [
        "",
        "## 按任务看差值与产出",
        "",
        "| 任务 | token（静态 → 动态） | token 差 | 调用数差 | 步骤（静态 → 动态） | 交付物字符（静态 → 动态） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for task in TASKS:
        static_rows = [row for row in rows if row.task.key == task.key and row.mode == "static"]
        dynamic_rows = [row for row in rows if row.task.key == task.key and row.mode == "dynamic"]
        if not static_rows or not dynamic_rows:
            continue
        static_total = _mean([row.total_tokens for row in static_rows])
        dynamic_total = _mean([row.total_tokens for row in dynamic_rows])
        share = (
            (dynamic_total - static_total) / static_total * 100 if static_total else 0.0
        )
        lines.append(
            "| {task} | {static:.0f} → {dynamic:.0f} | {delta:+.0f}（{share:+.1f}%） | "
            "{calls:+.1f} | {static_steps:.1f} → {dynamic_steps:.1f} | {static_chars:.0f} → {dynamic_chars:.0f} |".format(
                task=task.key,
                static=static_total,
                dynamic=dynamic_total,
                delta=dynamic_total - static_total,
                share=share,
                calls=_mean([float(row.model_calls) for row in dynamic_rows])
                - _mean([float(row.model_calls) for row in static_rows]),
                static_steps=_mean([float(row.steps) for row in static_rows]),
                dynamic_steps=_mean([float(row.steps) for row in dynamic_rows]),
                static_chars=_mean([float(row.output_chars) for row in static_rows]),
                dynamic_chars=_mean([float(row.output_chars) for row in dynamic_rows]),
            )
        )
    lines += [
        "",
        "这张表才是能下判断的地方：**合计的 token 差会把难度差异平均掉**。看两件事——",
        "「动态把它拆成了几步」，以及「花掉这些 token 换来的交付物是更长还是更短」。",
        "一个拆成多步、token 数倍、交付物却更短的任务，就是这一轮动态没有收益的证据。",
        "",
    ]
    lines += [
        "## 怎么读这份结果",
        "",
        "- **墙钟时间这一列读不得**：里面混着网关节流的强制等待，`节流等待` 列是它的可解释"
        "部分，`模型纯耗时` 才是模型侧的真实开销。token 与调用次数不受节流影响，"
        "它们才是两个模式之间可比的口径。",
        "- 单点任务上动态**必然**更贵：多一次规划调用，产出却不会更好。这类任务是"
        "判断「动态值不值得开」的下限。",
        "- 只有「需要两路独立收集再合并」这类任务，静态三步图在结构上做不到，动态才"
        "可能出现真正的净收益；`two_independent_sources` 就是为这一点准备的。",
        "- 必备内容命中是**弱判据**（只看关键词是否出现），它只能挡住明显的答非所问，"
        "不能替代人工读一遍交付物。",
        "- 采样次数少时结论不稳（模型输出长度本身抖动很大），`--repeat` 调大再看趋势。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="静态/动态编排净收益度量（真实模型）")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--gap", type=float, default=3.0, help="组合之间的间隔秒数")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=6.0,
        help="相邻模型调用之间的最小间隔秒数；网关紧接着上一次请求会必失败，6s 实测可过",
    )
    parser.add_argument("--retry", type=int, default=2, help="单次调用报错后的重试次数")
    parser.add_argument(
        "--base-url",
        default="",
        help="覆盖 base_url；默认把 host.docker.internal 换成 localhost（脚本跑在宿主机上）",
    )
    parser.add_argument(
        "--rows-out",
        type=Path,
        default=None,
        help="把原始行写到该 JSON（默认写到 <--out>.json）；改报告模板时用它离线重渲染",
    )
    parser.add_argument(
        "--from-rows",
        type=Path,
        default=None,
        help="不调用模型，直接用已存的原始行 JSON 重渲染报告",
    )
    args = parser.parse_args(argv)

    if args.from_rows is not None:
        payload = json.loads(args.from_rows.read_text(encoding="utf-8"))
        rows, meta = rows_from_payload(payload)
        text = report(
            rows,
            model=str(meta.get("model") or "?"),
            base_url=str(meta.get("base_url") or ""),
            repeat=int(meta.get("repeat") or 1),
            min_interval=float(meta.get("min_interval") or 0.0),
            recorded_at=str(meta["recorded_at"]) if meta.get("recorded_at") else None,
        )
        print(text)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"报告已重渲染到 {args.out}（未调用模型）")
        return 0

    if os.environ.get("MACP_AB_LIVE") != "1":
        print(
            "未设置 MACP_AB_LIVE=1：这个脚本会调用真实模型多次并产生费用，因此默认不跑。"
            "已有原始行时可改用 --from-rows 离线重渲染。",
            file=sys.stderr,
        )
        return 2

    from app.config import get_settings
    from app.core.provider_config import resolve_provider_settings
    from app.orchestration.llm import build_chat_model

    settings = resolve_provider_settings(get_settings())
    base_url = args.base_url or settings.openai_base_url
    if base_url and "host.docker.internal" in base_url:
        base_url = base_url.replace("host.docker.internal", "localhost")
    if base_url != settings.openai_base_url:
        settings = settings.model_copy(update={"openai_base_url": base_url})

    llm = build_chat_model(settings)
    model_name = settings.openai_model or settings.ollama_model

    rows: list[RunStats] = []
    for task in TASKS:
        for mode in MODES:
            for run in range(args.repeat):
                if rows:
                    time.sleep(args.gap)
                stats = measure(
                    task,
                    mode,
                    llm=llm,
                    max_steps=args.max_steps,
                    min_interval=args.min_interval,
                    retries=args.retry,
                )
                rows.append(stats)
                flag = "OK " if stats.quality else "BAD"
                print(
                    f"[{flag}] {task.key}/{mode}#{run + 1} "
                    f"wall={stats.seconds}s model={stats.model_seconds}s wait={stats.throttle_wait}s "
                    f"steps={stats.steps} calls={stats.model_calls} "
                    f"tokens={stats.total_tokens:.0f} chars={stats.output_chars}"
                    + (f" retried={stats.retried}" if stats.retried else "")
                    + (f" error={stats.error}" if stats.error else ""),
                    flush=True,
                )

    text = report(
        rows,
        model=model_name,
        base_url=base_url or "",
        repeat=args.repeat,
        min_interval=args.min_interval,
    )
    print()
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.out}")

    rows_out = args.rows_out or (args.out.with_suffix(".rows.json") if args.out else None)
    if rows_out is not None:
        rows_out.parent.mkdir(parents=True, exist_ok=True)
        rows_out.write_text(
            json.dumps(
                rows_to_payload(
                    rows,
                    model=model_name,
                    base_url=base_url or "",
                    repeat=args.repeat,
                    min_interval=args.min_interval,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"原始行已写入 {rows_out}（改报告模板时用 --from-rows 离线重渲染）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

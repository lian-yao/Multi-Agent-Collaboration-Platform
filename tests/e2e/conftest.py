"""`tests/e2e` 共享替身：无容器环境下用真实代码跑通端到端链路（成员 C D9-10）。

对齐 `doc/testing.md` §1/§2.3：端到端用例（E 系列）的验收环境是完整 compose
（真实 Dapr + PostgreSQL + Redis）。`test_pipeline_e2e.py` 是**无容器环境下的回归网**：
它执行真实的 API 路由、真实的 Workflow/活动代码、真实的三步 LangGraph 流水线、
真实的 MCP 注册表与可观测采集，只把两处进程外依赖换成替身：

1. **Dapr 运行时**（调度、活动与子 Workflow 的持久化）→ `InlineWorkflowDriver`
   直接驱动真实的 `agent_pipeline_workflow` / `agent_subtask_workflow` 生成器；
2. **PostgreSQL 落库**（`app/core/checkpoint.py` 的业务回写与 `tool_calls` 审计）
   → 写回 `app.api.store.InMemoryApiStore` 与内存审计表，语义（状态迁移校验、
   报告消息幂等、running→终态）与真实实现保持一致。

**数据库 DSN 的自足化**：替身只覆盖了 checkpoint 的写入侧，读取侧仍有一处会碰配置库
——`app/core/checkpoint.py::get_provider_config()`（读 Provider 覆盖行，未命中即回落
环境默认值）。DSN 指向不可达的 PostgreSQL 时，SQLAlchemy 建连会一直等在 `select` 上，
用例不是快速失败而是**挂住**，ADR-016 的「无容器也能跑」因此不成立。
所以这里在导入应用模块**之前**把 DSN 钉成一个自足的内存 SQLite，
语义等价于「配置库里没有覆盖行」（两者都回落到环境默认设置）。
要让这组用例对着真实 PostgreSQL 跑，设 `MACP_E2E_DATABASE_URL`。
真实 Dapr + PostgreSQL 上的验收见 `test_live_e2e.py`（走 HTTP，不使用这里的 DSN）。

**Provider 配置的 Redis 镜像**（2026-09-20 补）：`app/core/provider_config.py` 的解析
顺序是 **Redis 优先**，所以只钉 DSN 还不够——回归网仍会读到本机 `provider:config`
里开发环境保存的 provider/model，语义上不再是「无覆盖」，Redis 不可达时每个流水线用例
还会多等约 12s 的连接重试（实测该批用例 6.63s → 80.09s）。这里与
`tests/unit/conftest.py` / `tests/integration/conftest.py` 同口径，用 autouse fixture
把该镜像换成内存替身。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

REGRESSION_DATABASE_URL = "sqlite+pysqlite:///:memory:"
"""无容器回归网的默认数据库：进程内 SQLite，不建连、不落盘。"""

# 必须在导入 `app.*` 之前设置：`app/core/storage.py` 的引擎与 session 工厂是
# 模块级 lru_cache，一旦用错 DSN 建过就换不回来了。
#
# 显式给出的 DSN 优先（`MACP_E2E_DATABASE_URL`，其次运行环境里已有的 `DATABASE_URL`）：
# 一次 pytest 进程会加载多个 conftest，若这里无条件覆盖，`DATABASE_URL=真实库 pytest tests`
# 会被静默改写成 SQLite，集成用例就再也连不上目标库了。
_explicit_dsn = os.getenv("MACP_E2E_DATABASE_URL") or os.getenv("DATABASE_URL")
os.environ["DATABASE_URL"] = _explicit_dsn or REGRESSION_DATABASE_URL

# 以下导入一律晚于 DSN 设定（`app.core.storage` 的引擎是模块级缓存，必须先定 DSN）。
import pytest  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

from app.api.store import InMemoryApiStore
from app.core import checkpoint as checkpoint_module
from app.core.provider_config import set_redis_factory  # noqa: E402
from app.memory import SessionMessage  # noqa: E402
from app.memory.runtime import set_conversation_memory_factory  # noqa: E402
from app.observability.metrics import (
    MetricSample,
    MetricsCollector,
    get_metrics_collector,
    set_metrics_collector,
)
from app.orchestration import pipeline_graph
from app.workflows import pipeline as workflow_pipeline


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _callable_name(target: Any) -> str:
    return getattr(target, "__name__", type(target).__name__)


# --------------------------------------------------------------------------- #
# 替身 1：Dapr 运行时
# --------------------------------------------------------------------------- #


class _PendingCall:
    """Workflow 生成器 `yield` 出来的一次 Dapr 调用。"""

    def __init__(
        self,
        kind: str,
        target: Any = None,
        payload: dict[str, Any] | None = None,
        instance_id: str | None = None,
        max_attempts: int = 1,
    ) -> None:
        self.kind = kind
        self.target = target
        self.payload = payload
        self.instance_id = instance_id
        self.max_attempts = max_attempts


class _WorkflowContext:
    """真实 Dapr Workflow 上下文的最小替身，只保留 Workflow 代码用到的接口。"""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.workflow_id = instance_id
        self.statuses: list[str] = []

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)

    def call_activity(self, activity: Any, *, input: Any = None, **_kwargs: Any) -> _PendingCall:
        return _PendingCall("activity", activity, input)

    def call_child_workflow(
        self,
        workflow: Any,
        *,
        input: Any = None,
        instance_id: str | None = None,
        retry_policy: Any = None,
        **_kwargs: Any,
    ) -> _PendingCall:
        attempts = int(getattr(retry_policy, "max_number_of_attempts", 1) or 1)
        return _PendingCall("child_workflow", workflow, input, instance_id, attempts)

    def when_all(self, tasks: list[_PendingCall]) -> _PendingCall:
        """并行波次：真实 Dapr 会并发驱动这批子工作流；回归网按顺序驱动同一批。"""

        return _PendingCall("when_all", payload=tasks)

    def create_timer(self, _delta: Any) -> _PendingCall:
        return _PendingCall("timer")


class _ActivityContext:
    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id


class InlineWorkflowDriver:
    """内联执行真实 Workflow：活动直接调用，子 Workflow 递归驱动。

    与 Dapr 的差异只在于「持久化边界」：这里不落盘、不重放，但调用顺序、
    实例 ID 生成与重试次数都按真实代码的参数执行。
    """

    def __init__(self) -> None:
        self.activities: list[str] = []
        self.child_instances: list[str] = []
        self.workflow_statuses: dict[str, list[str]] = {}

    def run(
        self,
        workflow_fn: Any,
        workflow_input: dict[str, Any],
        *,
        instance_id: str,
    ) -> Any:
        context = _WorkflowContext(instance_id)
        self.workflow_statuses[instance_id] = context.statuses
        return self._drive(workflow_fn, context, workflow_input)

    def _drive(self, workflow_fn: Any, context: _WorkflowContext, workflow_input: Any) -> Any:
        generator = workflow_fn(context, workflow_input)
        outcome: Any = None
        error: BaseException | None = None
        while True:
            try:
                # 失败要注回生成器内部（等价 Dapr 的调用失败），否则 Workflow 的
                # 异常分支与终态回写不会执行。
                pending = (
                    generator.throw(error) if error is not None else generator.send(outcome)
                )
            except StopIteration as stop:
                return stop.value
            outcome = None
            error = None
            try:
                outcome = self._resolve(pending, context)
            except BaseException as exc:
                error = exc

    def _resolve(self, pending: _PendingCall, context: _WorkflowContext) -> Any:
        if pending.kind == "activity":
            self.activities.append(_callable_name(pending.target))
            return pending.target(_ActivityContext(context.instance_id), pending.payload)
        if pending.kind == "when_all":
            # 顺序驱动同一批子工作流：结果顺序与真实实现一致（`WhenAllTask` 按入参顺序出结果）。
            return [self._resolve(task, context) for task in pending.payload]
        if pending.kind == "child_workflow":
            instance_id = pending.instance_id or (
                f"{context.instance_id}:{_callable_name(pending.target)}"
            )
            self.child_instances.append(instance_id)
            return self._run_child(pending, instance_id)
        return None  # 定时器：回归用例不需要真实等待

    def _run_child(self, pending: _PendingCall, instance_id: str) -> Any:
        last_error: Exception | None = None
        for _ in range(max(1, pending.max_attempts)):
            try:
                return self.run(pending.target, pending.payload, instance_id=instance_id)
            except Exception as exc:  # 与 Dapr 重试策略一致：重跑子 Workflow
                last_error = exc
        assert last_error is not None
        raise last_error


class InlineWorkflowService:
    """`app/api/main.py` 依赖的 WorkflowService 替身（替换 Dapr 调度）。

    真实语义是异步（API 返回 202 后由 Dapr 执行），因此这里也在后台线程里驱动，
    用例仍能观察到 pending → running → completed 的轮询过程。
    """

    def __init__(self, driver: InlineWorkflowDriver) -> None:
        self._driver = driver
        self.scheduled: list[Any] = []
        self.errors: list[BaseException] = []
        self._threads: list[threading.Thread] = []

    def schedule(self, task: Any) -> str:
        self.scheduled.append(task)
        thread = threading.Thread(
            target=self._run,
            args=(task,),
            name=f"inline-workflow-{task.workflow_id}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()
        return str(task.workflow_id)

    def _run(self, task: Any) -> None:
        try:
            # 按生效的编排模式选工作流：与 `WorkflowService.schedule` 同一判断
            # （`resolve_workflow_name`），否则「单次请求选动态」在回归网里会被静默忽略。
            from app.workflows import dynamic as workflow_dynamic
            from app.workflows.service import resolve_workflow_name

            dynamic_mode = (
                resolve_workflow_name(task.orchestration_mode)
                == workflow_dynamic.DYNAMIC_WORKFLOW_NAME
            )
            self._driver.run(
                (
                    workflow_dynamic.agent_dynamic_workflow
                    if dynamic_mode
                    else workflow_pipeline.agent_pipeline_workflow
                ),
                task.asdict(),
                instance_id=str(task.workflow_id),
            )
        except BaseException as exc:  # 失败由终态回写体现，线程内不抛出
            self.errors.append(exc)

    def join(self, timeout: float = 30.0) -> None:
        for thread in list(self._threads):
            thread.join(timeout)


# --------------------------------------------------------------------------- #
# 替身 2：PostgreSQL 落库
# --------------------------------------------------------------------------- #


class MemoryCheckpoint:
    """`app/core/checkpoint.py` 写入侧的进程内替身。

    只替换 Workflow 回写链路调用的函数，语义保持一致：
    状态迁移按 `checkpoint.ALLOWED_TRANSITIONS` 校验，报告消息按固定 ID 幂等。
    """

    def __init__(self, store: InMemoryApiStore) -> None:
        self._store = store
        self.step_results: dict[str, dict[str, Any]] = {}

    def save_step_result(self, workflow_id: str, step: str, result: dict[str, Any]) -> None:
        self.step_results[f"{workflow_id}:{step}"] = result

    def update_workflow_run(
        self,
        workflow_id: str,
        *,
        status: str | None = None,
        current_step: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        instance_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        row = self._store.workflows.get(str(workflow_id))
        if row is None:
            raise KeyError(f"workflow run not found: {workflow_id}")
        if status is not None and status != row["status"]:
            allowed = checkpoint_module.ALLOWED_TRANSITIONS.get(row["status"], set())
            if status not in allowed:
                raise ValueError(
                    f"invalid workflow status transition: {row['status']} -> {status}"
                )
            row["status"] = status
            if status in {"completed", "failed", "cancelled"}:
                row["completed_at"] = _utcnow()
        if current_step is not None:
            row["current_step"] = current_step
        if checkpoint is not None:
            row["checkpoint"] = checkpoint
        if instance_id is not None:
            row["instance_id"] = instance_id
        if error is not None:
            row["error"] = error
        row["updated_at"] = _utcnow()
        return row.copy()

    def update_agent_run_status(self, agent_run_id: str, status: str) -> dict[str, Any]:
        return self._store.update_agent_run_status(str(agent_run_id), status)

    def update_message_status(self, message_id: str, status: str) -> dict[str, Any]:
        return self._store.update_message_status(str(message_id), status)

    def upsert_message(
        self,
        session_id: str,
        *,
        message_id: str,
        content: str,
        role: str,
        status: str = "queued",
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        existing = self._store.messages.get(str(message_id))
        if existing is not None:
            return existing.copy()
        row = {
            "id": str(message_id),
            "session_id": str(session_id),
            "role": role,
            "content": content,
            "agent_run_id": agent_run_id,
            "status": status,
            "created_at": _utcnow(),
        }
        self._store.messages[row["id"]] = row
        return row.copy()


class MemoryToolAudit:
    """`tool_calls` 表的进程内替身；真实审计逻辑（`execute_tool_call`）保持不替换。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get_tool_call(self, call_id: Any) -> dict[str, Any] | None:
        row = self.rows.get(str(call_id))
        return dict(row) if row else None

    def create_tool_call(
        self,
        *,
        call_id: Any,
        run_id: Any,
        tool_name: str,
        tool_input: dict[str, Any],
        workflow_run_id: Any = None,
    ) -> dict[str, Any]:
        key = str(call_id)
        if key not in self.rows:
            self.rows[key] = {
                "id": key,
                "run_id": str(run_id),
                "workflow_run_id": str(workflow_run_id) if workflow_run_id else None,
                "tool_name": tool_name.strip(),
                "input": tool_input,
                "output": None,
                "status": "running",
                "error": None,
                "created_at": _utcnow(),
                "updated_at": _utcnow(),
            }
        return dict(self.rows[key])

    def mark_tool_call_running(self, call_id: Any) -> dict[str, Any]:
        row = self.rows[str(call_id)]
        if row["status"] != "succeeded":
            row.update(status="running", output=None, error=None)
        row["updated_at"] = _utcnow()
        return dict(row)

    def complete_tool_call(self, call_id: Any, *, output: Any) -> dict[str, Any]:
        row = self.rows[str(call_id)]
        row.update(status="succeeded", output=output, error=None, updated_at=_utcnow())
        return dict(row)

    def fail_tool_call(self, call_id: Any, *, error: str) -> dict[str, Any]:
        row = self.rows[str(call_id)]
        row.update(status="failed", output=None, error=error, updated_at=_utcnow())
        return dict(row)


# --------------------------------------------------------------------------- #
# 测试替身：模型与指标
# --------------------------------------------------------------------------- #


class ScriptedStageModel(BaseChatModel):
    """假模型：每个阶段先请求一次真实工具，收到观察后给出该阶段的结论。

    与 `doc/testing.md` §1 的约定一致：不请求任何外部模型服务。
    """

    tool_name: str = "calculator"
    expressions: list[str] = Field(
        default_factory=lambda: ["12*(3+4)", "12*(3+5)", "12*(3+6)"]
    )
    bound_tools: list[Any] = Field(default_factory=list, exclude=True)
    calls: list[list[Any]] = Field(default_factory=list, exclude=True)
    requests: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    stage: int = Field(default=0, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted-stage-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain 钩子签名
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        if any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(content=f"第 {self.stage} 阶段结论：已采纳工具观察结果。")
        else:
            self.stage += 1
            arguments = {"expression": self.expressions[self.stage - 1]}
            self.requests.append(arguments)
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": arguments,
                        "id": f"call-{self.stage}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _prompt_text(messages: Any) -> str:
    """把消息列表拼成一段文本，用来判别"这是哪个节点的提示词"。"""

    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                str(block.get("text", "")) for block in content if isinstance(block, dict)
            )
    return "\n".join(parts)


def _json_message(payload: dict[str, Any]) -> ChatResult:
    return ChatResult(
        generations=[
            ChatGeneration(message=AIMessage(content=json.dumps(payload, ensure_ascii=False)))
        ]
    )


DYNAMIC_PLAN_STEPS: list[dict[str, Any]] = [
    {
        "id": "s1",
        "role": "collector",
        "instruction": "收集 A 口径",
        "depends_on": [],
        "expected_output": "要点清单",
    },
    {"id": "s2", "role": "collector", "instruction": "收集 B 口径", "depends_on": []},
    {
        "id": "s3",
        "role": "analyst",
        "instruction": "对比两路口径",
        "depends_on": ["s1", "s2"],
    },
]


class ScriptedE2EModel(ScriptedStageModel):
    """回归网的统一假模型：动态链路的**平台节点**按提示词返回结构化 JSON，
    其余（静态阶段、动态 worker）沿用 `ScriptedStageModel` 的脚本行为。

    按提示词判别而不是按"第几次调用"判别是刻意的：并行波次下的调用顺序不确定，
    按序号发回复会让用例在并行开启后变得随机。真实链路也是这么区分的——
    每个节点有自己的 system prompt。
    """

    @property
    def _llm_type(self) -> str:
        return "scripted-e2e-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        text = _prompt_text(messages)
        if "问题理解 Agent" in text:
            self.calls.append(list(messages))
            return _json_message(
                {
                    "rewritten_task": "对比 A 与 B 的实测口径并出报告",
                    "intent": {
                        "intent_type": "report",
                        "user_goal": "出一份对比报告",
                        "constraints": ["中文"],
                        "need_multi_subtask": True,
                    },
                }
            )
        if "任务规划 Agent" in text:
            self.calls.append(list(messages))
            return _json_message({"rationale": "两路并行收集再对比", "steps": DYNAMIC_PLAN_STEPS})
        if "结果校验 Agent" in text:
            self.calls.append(list(messages))
            return _json_message({"satisfied": True, "defects": [], "missing": []})
        if "合成器" in text:
            self.calls.append(list(messages))
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="合成后的对比报告"))]
            )
        return super()._generate(messages, stop, run_manager, **kwargs)


class MemoryMetricSink:
    """把采样留在内存里：默认 `PostgresMetricSink` 会连接 `metrics` 表（成员 B 建）。"""

    def __init__(self) -> None:
        self.written: list[MetricSample] = []

    def write(self, samples) -> int:  # noqa: ANN001 - MetricSink 协议
        self.written.extend(samples)
        return len(samples)

    def names(self) -> list[str]:
        return [sample.metric_name for sample in self.written]

    def labels(self, metric_name: str) -> list[dict[str, str]]:
        return [
            sample.labels
            for sample in self.written
            if sample.metric_name == metric_name
        ]


class E2EEnvironment:
    """一次端到端回归用的替身集合与真实组件引用。"""

    def __init__(
        self,
        *,
        store: InMemoryApiStore,
        driver: InlineWorkflowDriver,
        service: InlineWorkflowService,
        checkpoint: MemoryCheckpoint,
        audit: MemoryToolAudit,
        metrics: MemoryMetricSink,
        model: ScriptedStageModel,
    ) -> None:
        self.store = store
        self.driver = driver
        self.service = service
        self.checkpoint = checkpoint
        self.audit = audit
        self.metrics = metrics
        self.model = model

    def checkpoint_state(self, workflow_id: str, stage: str) -> dict[str, Any]:
        """阶段完成后落 Checkpoint 的 PipelineState 快照（`save_step_result`）。"""

        return self.checkpoint.step_results[f"{workflow_id}:{stage}"]

    def stage_payload(self, workflow_id: str, stage: str) -> dict[str, Any]:
        """Checkpoint 快照里该阶段的载荷：step / status / content / tool_calls。"""

        return self.checkpoint_state(workflow_id, stage)["results"][stage]


# --------------------------------------------------------------------------- #
# 替身 3：Provider 配置的 Redis 镜像
# --------------------------------------------------------------------------- #


class MemoryRedis:
    """最小 Redis 替身：Provider 配置镜像只用到 `get` / `set`
    （`app/core/provider_config.py::_read_mirror` / `_write_mirror`）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class MemoryConversationMemory:
    """会话记忆替身：进程内 List，语义与 Redis 实现一致（按时间正序、可丢失）。

    F-06 接线后 API 与 Workflow 活动都会读写会话记忆；回归网用它保证
    「多轮上下文继承」可被断言，同时又不需要真实 Redis（见 ADR-019）。
    """

    def __init__(self) -> None:
        self.data: dict[str, list[SessionMessage]] = {}

    def append_message(self, session_id: str, message: SessionMessage) -> None:
        self.data.setdefault(session_id, []).append(message)

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[SessionMessage]:
        messages = list(self.data.get(session_id, []))
        if limit is None:
            return messages
        count = max(1, int(limit))
        return messages[-count:]


@pytest.fixture
def memory_metrics() -> Iterator[tuple[MetricsCollector, MemoryMetricSink]]:
    """安装内存指标采集器，避免默认 sink 在建连上耗时。"""

    sink = MemoryMetricSink()
    collector = MetricsCollector(sink=sink, enabled=True)
    previous = get_metrics_collector()
    set_metrics_collector(collector)
    yield collector, sink
    set_metrics_collector(previous)


@pytest.fixture(autouse=True)
def memory_provider_cache() -> Iterator[MemoryRedis]:
    """把 Provider 配置的 Redis 镜像换成内存替身，用例之间互不影响。"""

    redis = MemoryRedis()
    set_redis_factory(lambda: redis)
    yield redis
    set_redis_factory(None)


@pytest.fixture(autouse=True)
def memory_conversation() -> Iterator[MemoryConversationMemory]:
    """把会话记忆换成内存替身，用例之间互不影响。"""

    memory = MemoryConversationMemory()
    set_conversation_memory_factory(lambda: memory)
    yield memory
    set_conversation_memory_factory(None)


@pytest.fixture
def e2e_env(
    monkeypatch: pytest.MonkeyPatch,
    memory_metrics: tuple[MetricsCollector, MemoryMetricSink],
) -> Iterator[E2EEnvironment]:
    """把 Dapr 运行时与 PostgreSQL 落库换成替身，其余链路保持真实。"""

    from app.api import main as api_main

    _collector, sink = memory_metrics
    store = InMemoryApiStore()
    driver = InlineWorkflowDriver()
    service = InlineWorkflowService(driver)
    memory_checkpoint = MemoryCheckpoint(store)
    audit = MemoryToolAudit()
    model = ScriptedE2EModel()

    monkeypatch.setattr(api_main, "api_store", store)
    monkeypatch.setattr(api_main, "get_workflow_service", lambda: service)
    monkeypatch.setattr(pipeline_graph, "build_chat_model", lambda *a, **k: model)

    for name in (
        "update_workflow_run",
        "update_agent_run_status",
        "update_message_status",
        "upsert_message",
    ):
        monkeypatch.setattr(
            workflow_pipeline, name, getattr(memory_checkpoint, name)
        )
    monkeypatch.setattr(
        workflow_pipeline, "save_step_result", memory_checkpoint.save_step_result
    )

    # 动态链路（ADR-038）自己绑定了这两个名字，否则它会写真实 Dapr 状态库与真实数据库：
    # 前者让回归网不再自足，后者在无 PostgreSQL 的环境里直接炸。
    from app.api import stage_trace as stage_trace_module
    from app.orchestration import dynamic_graph as dynamic_graph_module
    from app.orchestration import intake as intake_module
    from app.orchestration import synthesis as synthesis_module
    from app.workflows import dynamic as workflow_dynamic

    monkeypatch.setattr(
        workflow_dynamic, "save_step_result", memory_checkpoint.save_step_result
    )
    monkeypatch.setattr(
        workflow_dynamic, "update_workflow_run", memory_checkpoint.update_workflow_run
    )
    # 每个模块都从 `app.orchestration.llm` 绑了自己的 `build_chat_model` 名字，
    # 只补一处会漏——动态链路的 intake / 规划 / 步骤 / 合成 / 校验各走一个模块。
    for module in (
        workflow_dynamic,
        dynamic_graph_module,
        intake_module,
        synthesis_module,
    ):
        monkeypatch.setattr(module, "build_chat_model", lambda *a, **k: model)
    # 动态节点载荷都进内存 checkpoint，`/stages` 的读取侧同样指向它——
    # 这样读接口跑的是真实分支逻辑（按 flow 逐节点取载荷），数据来源换成内存。
    monkeypatch.setattr(
        stage_trace_module,
        "read_step_result",
        lambda workflow_id, step: (
            {
                "workflow_id": workflow_id,
                "step": step,
                "result": memory_checkpoint.step_results[f"{workflow_id}:{step}"],
            }
            if f"{workflow_id}:{step}" in memory_checkpoint.step_results
            else None
        ),
    )

    for name in (
        "get_tool_call",
        "create_tool_call",
        "mark_tool_call_running",
        "complete_tool_call",
        "fail_tool_call",
    ):
        monkeypatch.setattr(checkpoint_module, name, getattr(audit, name))

    environment = E2EEnvironment(
        store=store,
        driver=driver,
        service=service,
        checkpoint=memory_checkpoint,
        audit=audit,
        metrics=sink,
        model=model,
    )
    try:
        yield environment
    finally:
        service.join(timeout=5)
        # 工具注册表按进程缓存，关闭它避免用例之间串用同一实例的连接。
        from app.mcp.registry import reset_tool_registry_cache

        reset_tool_registry_cache()

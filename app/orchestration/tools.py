"""成员 A D7-8：编排层接入 MCP 工具（工具发现与调用）。

职责边界（`分工.md` §1/§3、`doc/architecture.md`、`doc/15 ...平台.md` 模块 3）：

- 本模块只定义**编排层消费的工具契约**：工具描述（ToolSpec）、调用请求（ToolCall）、
  调用记录（ToolCallRecord）、注册表协议（ToolRegistry）与带记录的调用入口（ToolCaller）；
- 具体工具实现、MCP Server 与沙箱由成员 C 在 `app/mcp`、`app/tools`、`app/sandbox` 提供，
  只要实现 ToolRegistry 协议即可被流水线直接消费（默认接入点见 `default_tool_registry`）；
- 工具调用审计落库（`tool_calls` 表）由成员 B 负责，本模块只产出可落库的记录对象。

对齐事实源：

- `doc/data-model.md` §3 tool_calls 表：tool_name / input / output / status / error；
- `doc/api.md` §4.10 `GET /tools`：name / description / input_schema；
- `doc/dapr-integration.md` §6：允许重放但禁止重复外部副作用，注册表按调用 ID 去重，
  因此调用方可以给出稳定 scope 让同一次执行的调用 ID 可复现（`tool_call_id`）。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.observability.logging import get_logger, log_event

TOOL_CALL_NAMESPACE = uuid.NAMESPACE_URL

TOOL_CALL_RETRY_LIMIT = 3
"""单次工具调用失败后的最大重试次数（首次 + 3 次重试 = 最多 4 次尝试）。

重试沿用同一个 `call_id`：审计层按 ID 幂等，失败行会先被重置为 `running` 再执行
（`app/core/tool_audit.py::_begin_call`，U-07「失败可同 ID 重试」），因此一次逻辑调用
在 `tool_calls` 表里始终只有一行，最终状态就是真实结果。耗尽重试后不再重试，
失败以 `failed` 记录交给模型，由模型按实际结果输出结论（不中断流水线）。
"""

logger = get_logger("orchestration.tools")


class ToolCallStatus(StrEnum):
    """调用状态，取值对齐 `doc/data-model.md` §3 tool_calls.status。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolSpec(BaseModel):
    """工具的对外描述，对齐 `doc/api.md` §4.10 GET /tools 的返回项。"""

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """一次工具调用请求；`call_id` 是注册表去重与审计的主键。"""

    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    """一次工具调用的结果记录，字段对齐 `doc/data-model.md` §3 tool_calls 表。"""

    call_id: str
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    status: ToolCallStatus = ToolCallStatus.RUNNING
    error: str | None = None


@runtime_checkable
class ToolRegistry(Protocol):
    """编排层依赖的工具注册表协议，由 `app/mcp` 提供实现。

    - `list_tools` 暴露当前可用工具，供流水线做动态发现；
    - `call` 执行一次调用，失败时抛异常（由 `ToolCaller` 归一化为 failed 记录）。
    """

    def list_tools(self) -> Sequence[ToolSpec]: ...

    def call(self, request: ToolCall) -> Any: ...


def tool_call_id(
    index: int,
    tool_name: str,
    *,
    scope: str | None = None,
    stage: str | None = None,
) -> str:
    """生成调用 ID。

    给定 `scope`（例如 Workflow 实例 ID）时用 uuid5 派生，使同一次执行的调用 ID
    在重放后可复现；未给定时退化为 uuid4，只要在本进程内唯一即可。

    `stage` 是派生键的一部分，可持久化执行必须传入阶段名：`ToolCaller` 每个阶段重建、
    `index` 从 0 起算，只按 `scope + index + tool_name` 派生会让同一 Workflow 的两个阶段
    拿到同一个调用 ID，后一个阶段被审计层当成重放，直接返回前一个阶段的缓存结果（F-01）。
    与 `app/core/tool_audit.py` 的约定一致——call_id 取自
    `workflow_id + stage + tool_name`。
    """

    if scope is None:
        return str(uuid.uuid4())
    return str(
        uuid.uuid5(
            TOOL_CALL_NAMESPACE,
            f"macp:tool:{scope}:{stage or '-'}:{index}:{tool_name}",
        )
    )


def as_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """把工具描述转成模型 `bind_tools` 需要的 OpenAI function 结构。"""

    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema or {"type": "object", "properties": {}},
        },
    }


class ToolCaller:
    """一次执行内消费工具注册表：负责发现、调用与记录（不落库）。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        scope: str | None = None,
        stage: str | None = None,
    ) -> None:
        self._registry = registry
        self._scope = scope
        self._stage = stage
        self._discovered = tuple(registry.list_tools())
        self.records: list[ToolCallRecord] = []
        self._index = 0
        if self._discovered:
            log_event(
                logger,
                "tools.discovered",
                count=len(self._discovered),
                tools=",".join(spec.name for spec in self._discovered),
            )

    def available(self) -> tuple[ToolSpec, ...]:
        """本次执行开始时发现的工具快照。"""

        return self._discovered

    def has_tools(self) -> bool:
        return bool(self._discovered)

    def openai_tools(self) -> list[dict[str, Any]]:
        """供 `bind_tools` 直接使用的工具描述列表。"""

        return [as_openai_tool(spec) for spec in self._discovered]

    def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> ToolCallRecord:
        """调用一个工具并记录结果；失败按 `TOOL_CALL_RETRY_LIMIT` 重试。

        重试沿用同一个 `call_id`（审计层可重置失败行，见模块常量说明），所以一次逻辑调用
        只占一行审计；耗尽重试后归一化为 failed 记录交给模型，不向外抛异常。
        """

        call_id = tool_call_id(
            self._index,
            tool_name,
            scope=self._scope,
            stage=self._stage,
        )
        self._index += 1
        record = ToolCallRecord(
            call_id=call_id,
            tool_name=tool_name,
            input=dict(arguments or {}),
        )
        request = ToolCall(
            call_id=record.call_id,
            tool_name=record.tool_name,
            arguments=record.input,
        )
        max_attempts = TOOL_CALL_RETRY_LIMIT + 1
        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                output = self._registry.call(request)
            except Exception as exc:  # 工具失败不应中断流水线，重试后交给模型继续
                record.status = ToolCallStatus.FAILED
                record.output = None
                record.error = f"{type(exc).__name__}: {exc}"
                log_event(
                    logger,
                    "tool.call",
                    level=logging.WARNING,
                    call_id=call_id,
                    tool_name=tool_name,
                    status=record.status.value,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=record.error,
                )
                if attempt < max_attempts:
                    log_event(
                        logger,
                        "tool.retry",
                        level=logging.WARNING,
                        call_id=call_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        error=record.error,
                    )
                    continue
                break
            record.status = ToolCallStatus.SUCCEEDED
            record.output = output
            record.error = None
            log_event(
                logger,
                "tool.call",
                call_id=call_id,
                tool_name=tool_name,
                status=record.status.value,
                attempt=attempt,
                max_attempts=max_attempts,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            break
        self.records.append(record)
        return record


_REGISTRY_FACTORY: Callable[[], ToolRegistry | None] | None = None


def set_tool_registry_factory(factory: Callable[[], ToolRegistry | None] | None) -> None:
    """覆盖默认注册表解析方式（主要供测试与自定义部署使用）。"""

    global _REGISTRY_FACTORY
    _REGISTRY_FACTORY = factory


def default_tool_registry() -> ToolRegistry | None:
    """解析默认工具注册表：显式工厂优先，其次成员 C 的 `app/mcp` 接入点。

    `app/mcp` 尚未提供时返回 None，流水线保持“不调用工具”的原有行为；
    因此成员 C 按契约实现 `app.mcp.registry.build_tool_registry` 后，
    流水线无需改动即可发现并调用工具。
    """

    if _REGISTRY_FACTORY is not None:
        return _REGISTRY_FACTORY()
    try:
        from app.mcp.registry import build_tool_registry
    except ModuleNotFoundError:
        return None
    return build_tool_registry()

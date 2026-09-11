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

import uuid
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

TOOL_CALL_NAMESPACE = uuid.NAMESPACE_URL


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
) -> str:
    """生成调用 ID。

    给定 `scope`（例如 Workflow 实例 ID）时用 uuid5 派生，使同一次执行的调用 ID
    在重放后可复现；未给定时退化为 uuid4，只要在本进程内唯一即可。
    """

    if scope is None:
        return str(uuid.uuid4())
    return str(uuid.uuid5(TOOL_CALL_NAMESPACE, f"macp:tool:{scope}:{index}:{tool_name}"))


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

    def __init__(self, registry: ToolRegistry, *, scope: str | None = None) -> None:
        self._registry = registry
        self._scope = scope
        self._discovered = tuple(registry.list_tools())
        self.records: list[ToolCallRecord] = []
        self._index = 0

    def available(self) -> tuple[ToolSpec, ...]:
        """本次执行开始时发现的工具快照。"""

        return self._discovered

    def has_tools(self) -> bool:
        return bool(self._discovered)

    def openai_tools(self) -> list[dict[str, Any]]:
        """供 `bind_tools` 直接使用的工具描述列表。"""

        return [as_openai_tool(spec) for spec in self._discovered]

    def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> ToolCallRecord:
        """调用一个工具并记录结果；失败归一化为 failed 记录，不向外抛异常。"""

        call_id = tool_call_id(self._index, tool_name, scope=self._scope)
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
        try:
            output = self._registry.call(request)
        except Exception as exc:  # 工具失败不应中断流水线，记录后交给模型继续
            record.status = ToolCallStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
        else:
            record.status = ToolCallStatus.SUCCEEDED
            record.output = output
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

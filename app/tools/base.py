"""内置工具的统一形状（成员 C D7-8）。

每个工具 = 一份对外描述（`ToolSpec`，对齐 `doc/api.md` §5.3 的
name / description / input_schema）+ 一个 Pydantic 入参模型 + 一个 `run` 实现。

职责边界：本模块只定义工具自身的形状与错误类型；注册表与流水线接线见
`app/tools/registry.py` 与 `app/mcp/registry.py`，编排层契约见
`app/orchestration/tools.py`（成员 A 冻结，ADR-009）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from app.orchestration.tools import ToolSpec


class ToolExecutionError(RuntimeError):
    """工具执行失败（参数非法、外部依赖不可用、策略拒绝等）。

    该异常由编排层 `ToolCaller` 归一化为 `failed` 调用记录，不中断流水线（ADR-009）。

    `retryable` 决定编排层是否重试（ADR-009 修订 3）：

    - `True`（默认）：**瞬时故障**——服务不可达、超时、沙箱/数据库暂时不可用等，
      换个时刻同样的调用可能成功；
    - `False`：**由入参或安全策略导致的确定性失败**——参数不合法、表达式/SQL 非法、
      策略拒绝、工具未注册等，原样重试只会重复失败，编排层直接放弃重试。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class BuiltinTool(ABC):
    """一个内置工具：`spec()` 暴露描述，`invoke()` 校验入参后执行。"""

    name: ClassVar[str]
    description: ClassVar[str] = ""
    args_model: ClassVar[type[BaseModel]]

    def spec(self) -> ToolSpec:
        """把工具描述成 MCP / 编排层通用的 JSON Schema 形式。"""

        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.args_model.model_json_schema(),
        )

    def invoke(self, arguments: dict[str, Any] | None = None) -> Any:
        """校验入参并执行；参数非法统一转为 `ToolExecutionError`。"""

        try:
            parsed = self.args_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ToolExecutionError(
                f"{self.name} 参数不合法: {_format_validation_error(exc)}",
                retryable=False,
            ) from exc
        return self.run(parsed)

    @abstractmethod
    def run(self, args: Any) -> Any:
        """执行工具逻辑；失败时抛 `ToolExecutionError`。"""


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )

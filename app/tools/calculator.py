"""计算器工具（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3 示例工具集
「计算器（数学表达式求值）」；工具名对齐 `doc/data-model.md` §3 tool_calls.tool_name 示例。

实现要点：**不使用 `eval`**——表达式经 `ast.parse` 后按白名单节点求值，
只放行四则运算、幂、比较与少量 `math` 函数/常量，因此不存在任意代码执行面。
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.tools.base import BuiltinTool, ToolExecutionError

MAX_ABS_INT = 10**12
MAX_ABS_EXPONENT = 1_000
MAX_FACTORIAL = 100

_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS: dict[str, Callable[..., Any]] = {
    name: getattr(math, name)
    for name in (
        "acos",
        "asin",
        "atan",
        "atan2",
        "ceil",
        "cos",
        "degrees",
        "exp",
        "fabs",
        "factorial",
        "floor",
        "gcd",
        "hypot",
        "log",
        "log10",
        "radians",
        "sin",
        "sqrt",
        "tan",
        "trunc",
    )
}
_FUNCTIONS.update({"abs": abs, "max": max, "min": min, "pow": pow, "round": round})

_CONSTANTS: dict[str, float] = {"e": math.e, "pi": math.pi, "tau": math.tau}


class CalculatorArgs(BaseModel):
    expression: str = Field(
        min_length=1,
        max_length=500,
        description="要计算的数学表达式，例如 (1+2)*3 或 sqrt(16)+log(100, 10)",
    )


class CalculatorTool(BuiltinTool):
    name = "calculator"
    description = "计算数学表达式（支持四则运算、幂运算与常用 math 函数/常量）。"
    args_model = CalculatorArgs

    def run(self, args: CalculatorArgs) -> dict[str, Any]:
        value = self._evaluate(args.expression)
        return {"expression": args.expression, "value": value}

    def _evaluate(self, expression: str) -> int | float:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ToolExecutionError(f"表达式语法错误: {exc.msg}") from exc
        try:
            result = self._evaluate_node(tree.body)
        except ToolExecutionError:
            raise
        except ZeroDivisionError as exc:
            raise ToolExecutionError("除数不能为 0") from exc
        except (OverflowError, ValueError) as exc:
            raise ToolExecutionError(f"计算失败: {exc}") from exc
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise ToolExecutionError("表达式结果不是数值")
        if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
            raise ToolExecutionError("表达式结果不是有限数值")
        return result

    def _evaluate_node(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return self._constant(node)
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        if isinstance(node, ast.UnaryOp):
            return self._unary(node)
        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            raise ToolExecutionError(f"未知标识符: {node.id}")
        if isinstance(node, ast.Call):
            return self._call(node)
        raise ToolExecutionError(f"不支持的表达式节点: {type(node).__name__}")

    def _constant(self, node: ast.Constant) -> int | float:
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolExecutionError("只支持数值常量")
        if isinstance(value, int) and abs(value) > MAX_ABS_INT:
            raise ToolExecutionError("整数常量超出允许范围")
        return value

    def _binary(self, node: ast.BinOp) -> Any:
        function = _BINARY_OPERATORS.get(type(node.op))
        if function is None:
            raise ToolExecutionError(f"不支持的运算符: {type(node.op).__name__}")
        left = self._evaluate_node(node.left)
        right = self._evaluate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_ABS_EXPONENT:
            raise ToolExecutionError("幂指数超出允许范围")
        return function(left, right)

    def _unary(self, node: ast.UnaryOp) -> Any:
        function = _UNARY_OPERATORS.get(type(node.op))
        if function is None:
            raise ToolExecutionError(f"不支持的一元运算符: {type(node.op).__name__}")
        return function(self._evaluate_node(node.operand))

    def _call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ToolExecutionError("只支持直接调用白名单函数")
        function = _FUNCTIONS.get(node.func.id)
        if function is None:
            raise ToolExecutionError(f"不支持的函数: {node.func.id}")
        if node.keywords:
            raise ToolExecutionError("函数调用不支持关键字参数")
        arguments = [self._evaluate_node(argument) for argument in node.args]
        if node.func.id == "factorial" and arguments and abs(arguments[0]) > MAX_FACTORIAL:
            raise ToolExecutionError(f"factorial 入参不能超过 {MAX_FACTORIAL}")
        return function(*arguments)

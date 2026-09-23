"""成员 C D7-8：内置工具的独立行为（`doc/testing.md` §2.1 U-06）。

覆盖 `app/tools` 的 calculator 与 sql_query，以及把四个工具暴露给编排层的注册表：

- 计算器返回值正确，且拒绝一切非数值表达式（不给工具留代码执行面）；
- 只读 SQL 的静态校验（注释、字符串字面量、分号拼接、写关键字）与真实只读执行；
- 注册表按名称发现与调用，未注册工具抛 `ToolExecutionError` 而不中断调用方。

测试替身按 `doc/testing.md` §1：SQL 只用 SQLite 内存库（不连接真实 PostgreSQL），
不请求任何外部模型或网络服务。`code_execution` 的行为在
`tests/unit/test_sandbox_policy.py` 中以拒绝后端验证，本文件不触发 Docker。
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.orchestration.tools import ToolCall
from app.tools import (
    BuiltinToolRegistry,
    CalculatorTool,
    SqlQueryTool,
    ToolExecutionError,
    WebSearchTool,
    assert_read_only_statement,
    builtin_tools,
)
from app.tools.config import ToolSettings

EXPECTED_TOOL_NAMES = ["calculator", "code_execution", "sql_query", "web_search"]


@pytest.fixture
def sql_engine():
    """单连接 SQLite 内存库；`StaticPool` 保证校验与查询看到同一个库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE metrics (metric_name TEXT, value REAL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO metrics (metric_name, value) VALUES"
            " ('tool_calls', 1.0), ('tool_calls', 2.0), ('stage_runs', 3.0),"
            " ('input_tokens', 4.0), ('output_tokens', 5.0)"
        )
    yield engine
    engine.dispose()


# --- calculator -----------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("1+2*3", 7),
        ("(1+2)*3", 9),
        ("7/2", 3.5),
        ("7//2", 3),
        ("-5%3", 1),
        ("2**10", 1024),
        ("-(-4)", 4),
    ],
)
def test_calculator_evaluates_arithmetic(expression, expected):
    assert CalculatorTool().invoke({"expression": expression})["value"] == expected


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("sqrt(16)+log(100, 10)", 6.0),
        ("round(3.14159, 2)", 3.14),
        ("max(1, 2, 3)", 3),
        ("floor(2.9)", 2),
        ("gcd(12, 18)", 6),
        ("factorial(5)", 120),
    ],
)
def test_calculator_supports_math_functions(expression, expected):
    assert CalculatorTool().invoke({"expression": expression})["value"] == expected


def test_calculator_supports_constants():
    result = CalculatorTool().invoke({"expression": "pi"})

    assert result["expression"] == "pi"
    assert result["value"] == math.pi


def test_calculator_returns_expression_alongside_value():
    assert CalculatorTool().invoke({"expression": "1+1"}) == {
        "expression": "1+1",
        "value": 2,
    }


def test_calculator_spec_matches_api_contract():
    """`doc/api.md` §4.10：对外描述含 name / description / input_schema。"""

    spec = CalculatorTool().spec()

    assert spec.name == "calculator"
    assert spec.description
    assert spec.input_schema["properties"]["expression"]["type"] == "string"
    assert spec.input_schema["required"] == ["expression"]


@pytest.mark.parametrize(
    "expression",
    [
        # 代码执行面：内建函数与属性逃逸一律拒绝
        "__import__('os').system('ls')",
        "open('/etc/passwd')",
        "(1).__class__",
        "eval('1+1')",
        # 非数值节点
        "1 if True else 2",
        "x + 1",
        "[1, 2][0]",
        # 资源耗尽保护
        "2 ** 100000",
        "factorial(1000)",
        # 运行期错误
        "1/0",
        "sqrt(-1)",
    ],
)
def test_calculator_rejects_unsupported_expressions(expression):
    with pytest.raises(ToolExecutionError):
        CalculatorTool().invoke({"expression": expression})


def test_calculator_requires_expression_argument():
    with pytest.raises(ToolExecutionError) as failure:
        CalculatorTool().invoke({})

    assert "参数不合法" in str(failure.value)


def test_calculator_failures_are_classified_non_retryable():
    """表达式内容决定的失败不带重试价值：标记 retryable=False（ADR-009 修订 3）。"""

    with pytest.raises(ToolExecutionError) as failure:
        CalculatorTool().invoke({"expression": "[1, 2, 3]"})

    assert failure.value.retryable is False


def test_invalid_arguments_are_classified_non_retryable():
    with pytest.raises(ToolExecutionError) as failure:
        CalculatorTool().invoke({})

    assert failure.value.retryable is False


def test_sql_policy_violation_is_classified_non_retryable():
    with pytest.raises(ToolExecutionError) as failure:
        SqlQueryTool().invoke({"query": "delete from messages"})

    assert failure.value.retryable is False


class _FailingEngine:
    """连库即失败的 Engine 替身，用于验证 SQL 执行失败的分类。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def connect(self) -> None:
        raise self._error


@pytest.mark.parametrize(
    ("error", "expected_retryable"),
    [
        # SQL 写错（表/列不存在、语法错）→ 原样重试必然重复失败
        ("ProgrammingError", False),
        # 连接/暂态类 → 换个时刻可能成功
        ("OperationalError", True),
    ],
)
def test_sql_execution_failure_retryability_follows_cause(error, expected_retryable):
    from sqlalchemy.exc import OperationalError, ProgrammingError

    error_type = {"ProgrammingError": ProgrammingError, "OperationalError": OperationalError}[
        error
    ]
    engine = _FailingEngine(error_type("SELECT 1", {}, Exception("boom")))

    with pytest.raises(ToolExecutionError) as failure:
        SqlQueryTool(engine=engine).invoke({"query": "select 1"})

    assert failure.value.retryable is expected_retryable


# --- sql_query：静态只读校验 ------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "delete from metrics",
        "update metrics set value = 0",
        "insert into metrics values ('x', 1)",
        "drop table metrics",
        "truncate table metrics",
        "select 1; delete from metrics",
        "select * from metrics for update",
        "alter table metrics add column extra text",
    ],
)
def test_sql_rejects_non_read_only_statements(sql):
    with pytest.raises(ToolExecutionError):
        assert_read_only_statement(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "select metric_name from metrics where value > 1",
        "with ranked as (select 1 as n) select n from ranked",
        "select metric_name from metrics -- 尾部注释",
        "/* 前导注释 */ select metric_name from metrics",
    ],
)
def test_sql_accepts_read_only_statements(sql):
    assert assert_read_only_statement(sql)


def test_sql_normalizes_trailing_semicolon_and_comments():
    assert assert_read_only_statement("  select 1 ;  ") == "select 1"


def test_sql_ignores_keywords_inside_string_literals():
    """字面量里的 `update` 不该被当成写操作。"""

    assert assert_read_only_statement(
        "select metric_name from metrics where metric_name = 'update'"
    )


def test_sql_rejects_empty_statement():
    with pytest.raises(ToolExecutionError):
        assert_read_only_statement("   -- 只有注释\n")


# --- sql_query：只读执行 ---------------------------------------------------


def test_sql_executes_read_only_query(sql_engine):
    result = SqlQueryTool(engine=sql_engine).invoke({"query": "select * from metrics"})

    assert result["columns"] == ["metric_name", "value"]
    assert result["row_count"] == 5
    assert result["truncated"] is False
    assert result["rows"][0] == {"metric_name": "tool_calls", "value": 1.0}


def test_sql_enforces_row_limit_and_reports_truncation(sql_engine):
    result = SqlQueryTool(engine=sql_engine).invoke(
        {"query": "select * from metrics", "limit": 2}
    )

    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert len(result["rows"]) == 2


def test_sql_rejects_write_before_touching_the_database(sql_engine):
    with pytest.raises(ToolExecutionError):
        SqlQueryTool(engine=sql_engine).invoke({"query": "delete from metrics"})

    remaining = SqlQueryTool(engine=sql_engine).invoke({"query": "select * from metrics"})
    assert remaining["row_count"] == 5


def test_sql_wraps_execution_errors(sql_engine):
    with pytest.raises(ToolExecutionError) as failure:
        SqlQueryTool(engine=sql_engine).invoke({"query": "select * from missing_table"})

    assert "SQL 执行失败" in str(failure.value)


# --- 注册表 ---------------------------------------------------------------


def test_builtin_registry_exposes_the_four_builtin_tools():
    registry = BuiltinToolRegistry()

    assert [spec.name for spec in registry.list_tools()] == EXPECTED_TOOL_NAMES
    assert len(builtin_tools()) == 4


def test_builtin_registry_specs_carry_json_schema():
    for spec in BuiltinToolRegistry().list_tools():
        assert spec.description, spec.name
        assert spec.input_schema.get("type") == "object", spec.name


def test_builtin_registry_calls_tool_by_name():
    registry = BuiltinToolRegistry()

    output = registry.call(
        ToolCall(call_id="c1", tool_name="calculator", arguments={"expression": "6*7"})
    )

    assert output == {"expression": "6*7", "value": 42}
    assert registry.get("calculator") is not None


def test_builtin_registry_reports_unknown_tool():
    registry = BuiltinToolRegistry()

    with pytest.raises(ToolExecutionError) as failure:
        registry.call(ToolCall(call_id="c1", tool_name="drop_database", arguments={}))

    assert "未注册的工具" in str(failure.value)
    assert registry.get("drop_database") is None


# --- web_search：注入 fetcher，不发起真实网络请求 -----------------------------

def _duckduckgo_tool(**kwargs) -> WebSearchTool:
    """钉住 provider=duckduckgo：

    下面几个用例验的是 **DuckDuckGo 契约**，必须显式选出口——否则本机 `.env` 把
    `TOOL_SEARCH_PROVIDER` 设成 `volcengine` 时，注入的 `fetch_json` 会被绕过，
    单测就变成了打真实网络（ADR-037 引入 provider 开关时暴露的确定性缺口）。
    """

    settings = ToolSettings(_env_file=None, search_provider="duckduckgo")
    return WebSearchTool(settings=settings, **kwargs)


SEARCH_PAYLOAD = {
    "Heading": "多智能体系统",
    "AbstractText": "多智能体系统由多个协作的智能体组成。",
    "AbstractURL": "https://example.com/mas",
    "RelatedTopics": [
        {"FirstURL": "https://example.com/a", "Text": "条目 A - A 的摘要"},
        {"FirstURL": "https://example.com/a", "Text": "条目 A 的重复项"},
        {"Topics": [{"FirstURL": "https://example.com/b", "Text": "条目 B"}]},
    ],
}


def test_web_search_parses_instant_answer_and_related_topics():
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetcher(endpoint, params, timeout):
        calls.append((endpoint, params, timeout))
        return SEARCH_PAYLOAD

    result = _duckduckgo_tool(fetch_json=fetcher).invoke({"query": "多智能体"})

    assert result["query"] == "多智能体"
    assert [item["url"] for item in result["results"]] == [
        "https://example.com/mas",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["results"][0]["title"] == "多智能体系统"
    assert calls[0][1]["q"] == "多智能体"
    assert calls[0][1]["format"] == "json"


def test_web_search_deduplicates_urls_and_applies_limit():
    tool = _duckduckgo_tool(fetch_json=lambda *_: SEARCH_PAYLOAD)

    limited = tool.invoke({"query": "x", "max_results": 1})
    assert [item["url"] for item in limited["results"]] == ["https://example.com/mas"]

    full = tool.invoke({"query": "x", "max_results": 10})
    assert [item["url"] for item in full["results"]] == [
        "https://example.com/mas",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_web_search_rejects_non_object_payload():
    tool = _duckduckgo_tool(fetch_json=lambda *_: ["not", "an", "object"])

    with pytest.raises(ToolExecutionError):
        tool.invoke({"query": "x"})


def test_web_search_propagates_transport_failure():
    def failing_fetcher(endpoint, params, timeout):
        raise ToolExecutionError("搜索服务不可达: connection refused")

    with pytest.raises(ToolExecutionError) as failure:
        _duckduckgo_tool(fetch_json=failing_fetcher).invoke({"query": "x"})

    assert "不可达" in str(failure.value)

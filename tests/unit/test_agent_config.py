"""Agent 配置覆盖的合并、校验与表契约（`doc/api.md` §5.7、ADR-013）。"""

import logging

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import AgentSettings
from app.core import agent_config, checkpoint
from app.core.agent_config import TOOL_NAMES_MAX_ITEMS
from app.core.checkpoint import UNSET


def _settings(**overrides) -> AgentSettings:
    return AgentSettings(_env_file=None, **overrides)


def test_agent_configs_table_matches_data_model():
    table = checkpoint.Base.metadata.tables["agent_configs"]
    assert list(table.columns.keys()) == [
        "agent_id",
        "model",
        "temperature",
        "llm_model_id",
        "top_p",
        "max_output_tokens",
        "reasoning_type",
        "tool_names",
        "updated_by",
        "updated_at",
    ]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "agent_id VARCHAR(50) NOT NULL" in ddl
    assert "model VARCHAR(200)" in ddl
    assert "temperature FLOAT" in ddl
    assert "llm_model_id VARCHAR(80)" in ddl
    assert "top_p FLOAT" in ddl
    assert "max_output_tokens INTEGER" in ddl
    assert "reasoning_type VARCHAR(20)" in ddl
    assert "tool_names JSONB" in ddl
    assert "updated_by VARCHAR(100)" in ddl
    assert "updated_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert table.primary_key.columns.keys() == ["agent_id"]
    # `llm_model_id` 是逻辑引用：不建外键，条目删除时退化为未绑定（ADR-017）。
    assert table.columns["llm_model_id"].foreign_keys == set()


def test_resolve_applies_override(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "collector", "model": "custom:1b", "temperature": 0.9}],
    )

    resolved = agent_config.resolve_agent_settings(
        "collector",
        _settings(llm_provider="ollama", ollama_model="env:7b", temperature=0.2),
    )

    assert resolved.ollama_model == "custom:1b"
    assert resolved.temperature == 0.9
    # 未覆盖的 provider 字段保持环境配置
    assert resolved.llm_provider == "ollama"


def test_resolve_falls_back_per_field(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "analyst", "model": None, "temperature": 0.5}],
    )

    resolved = agent_config.resolve_agent_settings(
        "analyst",
        _settings(ollama_model="env:7b", temperature=0.2),
    )

    assert resolved.ollama_model == "env:7b"
    assert resolved.temperature == 0.5


def test_resolve_uses_openai_field_for_openai_provider(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "reporter", "model": "gpt-x", "temperature": None}],
    )

    resolved = agent_config.resolve_agent_settings(
        "reporter",
        _settings(llm_provider="openai", openai_model="gpt-env", temperature=0.2),
    )

    assert resolved.openai_model == "gpt-x"
    assert resolved.temperature == 0.2


def test_resolve_returns_base_when_agent_has_no_override(monkeypatch):
    monkeypatch.setattr(checkpoint, "list_agent_configs", lambda: [])
    base = _settings(ollama_model="env:7b")

    assert agent_config.resolve_agent_settings("collector", base) is base


def test_resolve_falls_back_when_read_fails(monkeypatch, caplog):
    def broken() -> list[dict]:
        raise OSError("database down")

    monkeypatch.setattr(checkpoint, "list_agent_configs", broken)
    base = _settings(ollama_model="env:7b")

    with caplog.at_level(logging.WARNING):
        resolved = agent_config.resolve_agent_settings("collector", base)

    assert resolved is base
    assert "event=config.read_fallback" in caplog.text


def _fake_upsert(captured: dict[str, object]):
    """记录写入字段的替身；默认值镜像 `checkpoint.upsert_agent_config` 的签名。"""

    def fake(
        agent_id,
        *,
        model=UNSET,
        temperature=UNSET,
        llm_model_id=UNSET,
        top_p=UNSET,
        max_output_tokens=UNSET,
        reasoning_type=UNSET,
        tool_names=UNSET,
        updated_by=None,
    ):
        captured.update(
            agent_id=agent_id,
            model=model,
            temperature=temperature,
            llm_model_id=llm_model_id,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            reasoning_type=reasoning_type,
            tool_names=tool_names,
            updated_by=updated_by,
        )
        return {"agent_id": agent_id}

    return fake


_UNTOUCHED = {
    "llm_model_id": UNSET,
    "top_p": UNSET,
    "max_output_tokens": UNSET,
    "reasoning_type": UNSET,
    "tool_names": UNSET,
    "updated_by": None,
}


def test_update_validates_and_writes(monkeypatch, caplog):
    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(checkpoint, "upsert_agent_config", _fake_upsert(captured))

    with caplog.at_level(logging.INFO):
        row = agent_config.update_agent_config(
            "collector", model="  new:1b  ", temperature=0.4, actor="req-1"
        )

    assert captured == {
        "agent_id": "collector",
        "model": "new:1b",  # 去空白后写入
        "temperature": 0.4,
        **_UNTOUCHED,
        "updated_by": "req-1",
    }
    assert row["agent_id"] == "collector"
    assert "event=config.agent.updated" in caplog.text


def test_update_passes_unset_and_none_through(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(checkpoint, "upsert_agent_config", _fake_upsert(captured))

    agent_config.update_agent_config("collector", model=None)
    assert captured == {
        "agent_id": "collector",
        "model": None,
        "temperature": UNSET,
        **_UNTOUCHED,
    }

    captured.clear()
    agent_config.update_agent_config("collector", temperature=0.1)
    assert captured == {
        "agent_id": "collector",
        "model": UNSET,
        "temperature": 0.1,
        **_UNTOUCHED,
    }


def test_update_writes_specialized_parameters(monkeypatch):
    """ADR-017 新增的四个覆盖字段：llm_model_id / top_p / max_output_tokens / reasoning_type。"""

    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(checkpoint, "upsert_agent_config", _fake_upsert(captured))
    monkeypatch.setattr(
        checkpoint, "get_llm_model", lambda model_id: {"id": model_id}
    )

    agent_config.update_agent_config(
        "analyst",
        llm_model_id=" gateway-main:gpt-4o ",
        top_p=0.9,
        max_output_tokens=2048,
        reasoning_type="openai",
    )

    assert captured["llm_model_id"] == "gateway-main:gpt-4o"  # 去空白后写入
    assert captured["top_p"] == 0.9
    assert captured["max_output_tokens"] == 2048
    assert captured["reasoning_type"] == "openai"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"llm_model_id": "   "},
        {"top_p": -0.1},
        {"top_p": 1.1},
        {"max_output_tokens": 0},
        {"max_output_tokens": 1.5},
        {"reasoning_type": "unknown"},
    ],
)
def test_update_rejects_invalid_specialized_parameters(monkeypatch, kwargs):
    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(
        checkpoint,
        "upsert_agent_config",
        lambda *args, **rest: pytest.fail("非法值不应写入"),
    )

    with pytest.raises(agent_config.AgentConfigError):
        agent_config.update_agent_config("collector", **kwargs)


def test_update_rejects_unknown_llm_model_id(monkeypatch):
    """`llm_model_id` 必须指向存在的条目，否则 422（§5.7）。"""

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(checkpoint, "get_llm_model", lambda model_id: None)
    monkeypatch.setattr(
        checkpoint,
        "upsert_agent_config",
        lambda *args, **rest: pytest.fail("悬空引用不应写入"),
    )

    with pytest.raises(agent_config.AgentConfigError):
        agent_config.update_agent_config("collector", llm_model_id="missing:1")


def test_effective_override_keys_lists_only_non_null_columns():
    row = {
        "agent_id": "collector",
        "model": None,
        "temperature": 0.3,
        "llm_model_id": "m-1",
        "top_p": None,
        "max_output_tokens": None,
        "reasoning_type": "none",
        # 空数组也是**已配置**：它与 NULL 是两种授权状态（取消全部 / 未限制）。
        "tool_names": [],
    }

    assert agent_config.effective_override_keys(row) == [
        "temperature",
        "llm_model_id",
        "reasoning_type",
        "tool_names",
    ]
    assert agent_config.effective_override_keys(None) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "   "},
        {"model": "x" * 201},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
    ],
)
def test_update_rejects_invalid_values(monkeypatch, kwargs):
    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(
        checkpoint,
        "upsert_agent_config",
        lambda *args, **rest: pytest.fail("非法值不应写入"),
    )

    with pytest.raises(agent_config.AgentConfigError):
        agent_config.update_agent_config("collector", **kwargs)
def test_resolve_agent_tools_distinguishes_unset_from_empty():
    """`NULL` 与 `[]` 是两种授权状态，必须分得开（ADR-034）。

    把空数组读成「未配置」会把用户刚做的收紧反向放大成放开——这是配置类字段里
    最容易出的那一类错，所以这条断言比它看起来要重要。
    """

    assert agent_config.resolve_agent_tools("collector", overrides={}) is None
    assert (
        agent_config.resolve_agent_tools(
            "collector", overrides={"collector": {"tool_names": None}}
        )
        is None
    )
    assert agent_config.resolve_agent_tools(
        "collector", overrides={"collector": {"tool_names": []}}
    ) == []
    assert agent_config.resolve_agent_tools(
        "collector",
        overrides={"collector": {"tool_names": ["calculator", "web_search"]}},
    ) == ["calculator", "web_search"]


def test_resolve_agent_tools_treats_non_list_as_unset():
    """覆盖值被写坏（不是数组）时按「未配置」处理，不让读侧崩在阶段执行里。"""

    assert (
        agent_config.resolve_agent_tools(
            "collector", overrides={"collector": {"tool_names": "calculator"}}
        )
        is None
    )


def test_resolve_agent_tools_reads_override_table_when_not_injected(monkeypatch):
    monkeypatch.setattr(
        checkpoint,
        "list_agent_configs",
        lambda: [{"agent_id": "analyst", "tool_names": ["sql_query"]}],
    )

    assert agent_config.resolve_agent_tools("analyst") == ["sql_query"]
    assert agent_config.resolve_agent_tools("collector") is None


def test_update_writes_deduped_tool_names(monkeypatch):
    """去重保序：同一份名单里重复出现的名字只留第一次的位置。"""

    captured: dict[str, object] = {}

    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(checkpoint, "upsert_agent_config", _fake_upsert(captured))

    agent_config.update_agent_config(
        "analyst", tool_names=["calculator", " calculator ", "sql_query"]
    )

    assert captured["tool_names"] == ["calculator", "sql_query"]


@pytest.mark.parametrize(
    "tool_names",
    [
        "calculator",  # 不是数组
        [1],  # 元素不是字符串
        [""],  # 空名
        ["calculator", "   "],  # 只有空白的名字
        ["x" * 121],  # 单名超长
        [f"tool-{index}" for index in range(TOOL_NAMES_MAX_ITEMS + 1)],  # 超量
    ],
)
def test_update_rejects_invalid_tool_names(monkeypatch, tool_names):
    monkeypatch.setattr(checkpoint, "get_agent_config", lambda agent_id: None)
    monkeypatch.setattr(
        checkpoint,
        "upsert_agent_config",
        lambda *args, **rest: pytest.fail("非法值不应写入"),
    )

    with pytest.raises(agent_config.AgentConfigError):
        agent_config.update_agent_config("collector", tool_names=tool_names)

"""集成层隔离契约：本目录默认看不到开发环境的运行期配置（2026-09-20 补）。

背景：`tests/integration/` 曾直连开发库与开发 Redis，`test_config_api.py` /
`test_inspection_api.py` 因此读到配置页保存的 `openai` / `deepseek-flash` 而失败（5 条）。
隔离由 `tests/integration/conftest.py` 提供，本用例把这条约束固化：把 conftest 的两半
（内存 DSN、内存 Redis 镜像）任去掉一半，这里立刻转红，失败模式不会被静默带回来。
"""

from __future__ import annotations

import os

from app.core.provider_config import provider_config_row
from app.core.storage import get_storage_settings


def test_provider_config_row_has_no_development_override() -> None:
    """默认运行下「无既有覆盖」：Redis 镜像与 PostgreSQL 事实源都读不到覆盖行。

    开发环境里保存过 provider/model（`provider:config` / `provider_configs`）时，
    这里必须仍为 None——被测代码据此回落 `AGENT_*` 环境配置。
    """

    assert provider_config_row() is None


def test_storage_dsn_defaults_to_isolated_database() -> None:
    """未显式指定 DSN 时，集成层连的是进程内 SQLite，而不是开发库。"""

    if os.getenv("MACP_INTEGRATION_DATABASE_URL"):
        return  # 显式指向真实服务：由使用者按 `doc/testing.md` §1 自备空库

    assert get_storage_settings().database_url == "sqlite+pysqlite:///:memory:"

"""E2E 回归网的隔离契约：默认看不到开发环境的运行期配置（2026-09-20 补）。

（文件名带 `regression_net` 是为了与 `tests/integration/test_isolation_contract.py`
区分：pytest 默认导入模式下，不同目录的同名测试模块会互相顶掉。）

背景：`tests/e2e/conftest.py` 早就把 DSN 钉成内存 SQLite，但 Provider 配置的
Redis 镜像一直连本机 Redis，于是回归网读到的是开发环境里保存的 `provider:config`
（如 `openai` / `deepseek-flash`）——语义上不再是「无覆盖」，Redis 不可达时这批用例
还会为每次连接重试各等十几秒（实测 6.63s → 80.09s）。隔离由 conftest 的 autouse
fixture 提供，本用例把这条约束固化：删掉那半个替身，这里立刻转红。
"""

from __future__ import annotations

import os

from app.core.provider_config import provider_config_row
from app.core.storage import get_storage_settings


def test_provider_config_row_has_no_development_override() -> None:
    """默认运行下「无既有覆盖」：Redis 镜像与 PostgreSQL 事实源都读不到覆盖行。"""

    row = provider_config_row()
    # 失败信息只带非敏感字段：覆盖行里含在用凭据，不该进测试输出
    assert row is None, (
        f"读到开发环境的运行期覆盖：provider={row.get('provider')} "
        f"model={row.get('model')}"
    )


def test_storage_dsn_defaults_to_isolated_database() -> None:
    """未显式指定 DSN 时，回归网连的是进程内 SQLite，而不是开发库。"""

    if os.getenv("MACP_E2E_DATABASE_URL"):
        return  # 显式指向真实服务：由使用者自备空库，见 `doc/testing.md` §1

    assert get_storage_settings().database_url == "sqlite+pysqlite:///:memory:"

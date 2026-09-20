"""`tests/integration` 共享替身：默认不读开发环境的活配置（2026-09-20 补）。

对齐 `doc/testing.md` §1：本目录用例断言的是「环境配置 + 无既有覆盖」的契约
（`test_config_api.py`、`test_inspection_api.py`），而运行期覆盖有两个**跨运行持久**的
存放点——PostgreSQL `provider_configs` 事实源与 Redis 镜像 `provider:config`
（`app/core/provider_config.py`）。本目录此前没有 conftest，两处都直连开发环境，
于是配置页里保存过的 provider/model（如 `openai` / `deepseek-flash`）会让 5 条用例转红：
失败原因是被测代码读到了开发环境的真实配置，不是代码缺陷。

两个替身与 `tests/unit/conftest.py` 同口径：

1. **数据库 DSN**：在导入 `app.*` 之前钉成内存 SQLite。`provider_configs` 查不到表
   → 走应用层既定的「没有覆盖行 → 回落 `AGENT_*` 环境配置」分支，语义与干净库一致；
   若指向不可达的 PostgreSQL，查询不是快速失败而是**阻塞在连接重试上**。
2. **Provider 配置的 Redis 客户端**：autouse fixture 注入内存替身，不再读本机
   `redis://localhost:6380/0` 里在用的 `provider:config` 镜像。

确需对着真实服务跑这一层时，设 `MACP_INTEGRATION_DATABASE_URL`（其次运行环境里已有的
`DATABASE_URL`），并自备空库——真实库里的覆盖行会让「无覆盖」类用例失败，见
`doc/testing.md` §1。
"""

from __future__ import annotations

import os

REGRESSION_DATABASE_URL = "sqlite+pysqlite:///:memory:"
"""集成回归的默认数据库：进程内 SQLite，不建连、不落盘。"""

# 必须在导入 `app.*` 之前设置：`app/core/storage.py` 的引擎与 session 工厂是
# 模块级 lru_cache，一旦用错 DSN 建过就换不回来了。
#
# 显式给出的 DSN 优先（`MACP_INTEGRATION_DATABASE_URL`，其次运行环境里已有的
# `DATABASE_URL`）：一次 pytest 进程会加载多个 conftest，若这里无条件覆盖，
# `DATABASE_URL=真实库 pytest tests` 会被静默改写，对着真实库的用例就再也连不上目标库。
_explicit_dsn = os.getenv("MACP_INTEGRATION_DATABASE_URL") or os.getenv("DATABASE_URL")
os.environ["DATABASE_URL"] = _explicit_dsn or REGRESSION_DATABASE_URL

# 以下导入一律晚于 DSN 设定（`app.core.storage` 的引擎是模块级缓存，必须先定 DSN）。
import pytest  # noqa: E402

from app.core.provider_config import set_redis_factory  # noqa: E402


class MemoryRedis:
    """最小 Redis 替身：Provider 配置镜像只用到 `get` / `set`
    （`app/core/provider_config.py::_read_mirror` / `_write_mirror`）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture(autouse=True)
def memory_provider_cache() -> MemoryRedis:
    """把 Provider 配置的 Redis 镜像换成内存替身，用例之间互不影响。"""

    redis = MemoryRedis()
    set_redis_factory(lambda: redis)
    yield redis
    set_redis_factory(None)

"""内置工具配置（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3「示例工具集」——
计算器、网络搜索（调用公开搜索 API）、代码执行、只读 SQL 查询。

环境变量统一使用 `TOOL_` 前缀，例如 `TOOL_SEARCH_ENDPOINT`、`TOOL_SQL_DSN`。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SearchProvider = Literal["duckduckgo", "volcengine"]

DUCKDUCKGO_SEARCH_ENDPOINT = "https://api.duckduckgo.com/"
"""DuckDuckGo Instant Answer 端点；ADR-032 的本地网关返回同一 JSON 契约。"""

DOUBAO_SEARCH_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
"""火山引擎豆包搜索（Custom）端点（ADR-037）：POST + Bearer key，按量付费。"""


class ToolSettings(BaseSettings):
    """内置工具运行参数。"""

    model_config = SettingsConfigDict(
        env_prefix="TOOL_",
        env_file=".env",
        extra="ignore",
    )

    search_provider: SearchProvider = "duckduckgo"
    """搜索出口的实现（ADR-037）：

    - `duckduckgo`（默认）：DuckDuckGo Instant Answer 的 JSON 契约，ADR-032 的本地网关同样适用；
    - `volcengine`：火山引擎豆包搜索 Custom（POST + PascalCase 字段 + Bearer key）。
    """

    search_endpoint: str = DUCKDUCKGO_SEARCH_ENDPOINT
    """搜索 API 端点；解析契约由 `search_provider` 决定，不显式给就按 provider 取默认。"""

    search_api_key: str = ""
    """豆包搜索的按量付费 API key；只从环境 / `.env` 读，**不进仓库、不进日志**（ADR-037 §2）。

    provider=volcengine 且这里为空时直接报配置错（非 retryable），不发出无鉴权请求。
    """

    search_timeout_seconds: float = 8.0
    search_max_results: int = 5

    sql_dsn: str = ""
    """只读 SQL 工具的数据源；留空时使用平台自身的 PostgreSQL（`StorageSettings.database_url`）。"""

    sql_timeout_seconds: int = 5

    sql_default_limit: int = 50
    """模型没在入参里给 `limit` 时的返回行数上限。

    生效点见 `app/tools/sql.py::SqlQueryTool.run`：入参留空取本值，
    并统一被 `MAX_ROWS` 夹住（本值超过上限时按上限执行）。
    """

    session_files_enabled: bool = True
    """是否给执行中的 Agent 挂上「读本次会话附件」的两个工具（`app/tools/session_files.py`）。

    默认开启：这是「Agent 能自己读文件」在**不放宽沙箱策略**前提下唯一能落地的形态。
    置 false 时角色节点只剩四个内置工具，行为退回到只依赖提示词里的附件内容。
    """

    session_file_max_chars: int = 20_000
    """单次读取返回的正文上限。与 `app/attachments/spec.py` 的单文件注入上限同量级：
    工具输出会原样回填给模型，读一份 20 万字的 PDF 会把上下文直接顶爆。"""

    session_file_list_limit: int = 50
    """`list_session_files` 返回的条数上限，避免异常会话把清单本身变成大载荷。"""

    @model_validator(mode="after")
    def _default_endpoint_follows_provider(self) -> "ToolSettings":
        """端点默认值跟随 provider；显式给过 `TOOL_SEARCH_ENDPOINT` 就尊重显式值。"""

        if (
            self.search_provider == "volcengine"
            and "search_endpoint" not in self.model_fields_set
        ):
            self.search_endpoint = DOUBAO_SEARCH_ENDPOINT
        return self


@lru_cache
def get_tool_settings() -> ToolSettings:
    return ToolSettings()

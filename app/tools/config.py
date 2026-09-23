"""内置工具配置（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 3「示例工具集」——
计算器、网络搜索（调用公开搜索 API）、代码执行、只读 SQL 查询。

环境变量统一使用 `TOOL_` 前缀，例如 `TOOL_SEARCH_ENDPOINT`、`TOOL_SQL_DSN`。

环境配置是**兜底默认**：搜索渠道可在运行期被 `tool_configs` 单行表覆盖（ADR-039），
合并规则与生效点在 `app/tools/search_config.py`。**基线**用 `env_tool_settings()`，
**生效值**用 `get_tool_settings()` —— 两者别混用。
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


BUNDLED_GATEWAY_HOST = "search-gateway"
"""compose 里那个本地搜索网关的服务名（ADR-032）。

它走的是 **DuckDuckGo 契约**（Bing RSS → `Abstract`/`RelatedTopics`）。因此这个地址
只对 `duckduckgo` 渠道成立——这也是 `_default_endpoint_follows_provider` 要纠的那一类错。
"""


def _is_bundled_gateway(endpoint: str) -> bool:
    """端点是否指向内置网关（按主机名判，忽略端口与路径）。"""

    from urllib.parse import urlsplit

    try:
        host = (urlsplit(endpoint).hostname or "").lower()
    except ValueError:  # 端口非数字等：交给后续校验，这里不判
        return False
    return host == BUNDLED_GATEWAY_HOST


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
        """端点默认值跟随 provider；显式给了非空值就尊重显式值。

        **空串按"没给"处理**：compose 里 `TOOL_SEARCH_ENDPOINT: ${TOOL_SEARCH_ENDPOINT:-}`
        传空是常态（见 ADR-037），若把空串当成显式值，provider 默认端点就会被它覆盖掉。
        """

        provided = "search_endpoint" in self.model_fields_set
        if not provided or not self.search_endpoint.strip():
            self.search_endpoint = (
                DOUBAO_SEARCH_ENDPOINT
                if self.search_provider == "volcengine"
                else DUCKDUCKGO_SEARCH_ENDPOINT
            )
        # 端点与渠道的**契约**不匹配时**不在这里纠正**：校验器分不出「有意把这条渠道接到
        # 自建网关上」和「端点只是取了默认值」，替人改会踩掉显式值优先的契约
        # （`test_search_tool_volcengine.py::test_explicit_endpoint_wins_over_provider_default`）。
        # 错配改用 `endpoint_provider_mismatch()` 诊断，由 `WebSearchTool` 构造时记警告。
        return self

    def endpoint_provider_mismatch(self) -> str:
        """端点与渠道的解析契约不匹配时返回一句人话，配得上就返回空串（ADR-039）。

        典型成因：compose 的 `TOOL_SEARCH_ENDPOINT: ${TOOL_SEARCH_ENDPOINT:-…}` 在变量为
        **空串**时也取默认值，于是「把 `TOOL_SEARCH_PROVIDER` 改成 volcengine、端点不动」会拿到
        一个只认 DuckDuckGo 契约的地址。照用只会以**解析错**失败，而报错信息指向完全无关的地方
        ——所以至少要留下一条能检索的线索。

        只判**内置网关**这一个主机名：自建网关 / 代理是操作者的显式选择，不judge。
        """

        if self.search_provider != "volcengine":
            return ""
        if not _is_bundled_gateway(self.search_endpoint):
            return ""
        return (
            f"搜索渠道是 volcengine（POST + Bearer），端点却指向内置的 DuckDuckGo 网关"
            f"（{self.search_endpoint}）：契约不匹配。请在「工具与配置 → 内部工具 → 搜索渠道」"
            f"里选中豆包并保存，或显式设置 TOOL_SEARCH_ENDPOINT"
        )


def env_tool_settings() -> ToolSettings:
    """纯环境配置：只读 `TOOL_*` / `.env`，**不**叠加运行期覆盖（ADR-039）。

    这是**基线**：`app/tools/search_config.py` 拿它跟覆盖行合并，
    `GET /api/v1/config/search` 也用它回答「环境里配的是什么」。
    """

    return ToolSettings()


@lru_cache
def get_tool_settings() -> ToolSettings:
    """生效配置：环境配置叠加运行期覆盖（ADR-039）。

    覆盖读不到时 `search_config` 自己降级成「没有覆盖」，所以这里不 try/except ——
    存储不可用最多让搜索渠道退回环境配置，不该把工具注册表的构建带崩。
    """

    from app.tools.search_config import effective_search_settings

    return effective_search_settings(env_tool_settings())


def reset_tool_settings_cache() -> None:
    """丢弃生效配置的进程缓存，让下一次构造工具重新读覆盖值（ADR-039）。

    写入搜索渠道后由 API 层调用。只清这一个缓存**不够**：`WebSearchTool.__init__`
    在构造时就把当时的 settings 存进了实例，而 `build_tool_registry()` 又把已构造好的
    实例按进程缓存（`app/mcp/registry.py`）。所以调用方还必须同时调
    `app.mcp.registry.reset_tool_registry_cache()`；两处一起做由
    `app/tools/search_config.py::_invalidate_tool_caches` 收口。
    """

    get_tool_settings.cache_clear()

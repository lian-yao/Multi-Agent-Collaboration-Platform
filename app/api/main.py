from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, FiniteFloat, field_validator, model_validator
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.store import SqlApiStore
from app.api.inspection import InspectionStore
from app.config import get_settings
from app.core import mcp_registry, model_registry
from app.core.agent_config import (
    AgentConfigError,
    OVERRIDE_FIELDS,
    agent_config_overrides,
    effective_override_keys,
    resolve_agent_settings,
    update_agent_config,
)
from app.core.checkpoint import UNSET
from app.core.mcp_registry import (
    McpDiscoveryError,
    McpRegistryError,
    McpServerNotFoundError,
)
from app.core.model_discovery import ModelDiscoveryError
from app.core.model_registry import (
    DuplicateEntryError,
    ModelNotFoundError,
    ModelRegistryError,
    ProviderInUseError,
    ProviderNotFoundError,
)
from app.core.provider_config import (
    ProviderConfigError,
    effective_provider_view,
    resolve_provider_settings,
    sanitize_base_url,
    update_provider_config,
)
from app.mcp.registry import tool_catalog
from app.observability.logging import get_logger, log_event
from app.observability.metrics import render_prometheus_metrics
from app.workflows.pipeline import WorkflowTask
from app.workflows.service import get_workflow_service

logger = get_logger("api.inspection")

# 覆盖值读取器：默认读 `agent_configs`，测试可替换为固定表（与 api_store 同一模式）。
agent_config_reader: Callable[[], dict[str, dict[str, Any]]] = agent_config_overrides

# —— 注册表接线（ADR-017）——
# 读写一律经 `app.core.model_registry` / `app.core.mcp_registry`：校验、错误类型与
# 出参脱敏都在那一层，API 只做「契约映射 + 错误码翻译」。
#
# 下面两个是**协议侧**钩子：远端发现会发起真实出站请求（模型清单 / MCP 握手），
# 因此保留可注入点，让测试用离线替身覆盖，而不必碰网络。
model_discovery_fetcher: Callable[..., Any] | None = None
"""`GET /api/v1/config/providers/{id}/models/discover` 的 HTTP 取数函数（`doc/api.md` §5.10）。"""

mcp_server_discoverer: Callable[..., dict[str, Any]] | None = None
"""`POST /api/v1/config/mcp/servers/{id}/discover` 的握手函数（`doc/api.md` §5.11）。"""


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code


class SessionCreateRequest(BaseModel):
    user_id: str | None = None


class SessionResponse(BaseModel):
    id: str
    user_id: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class SessionSummaryResponse(BaseModel):
    """历史会话列表项（`doc/api.md` §5.13）。

    `title` 为该会话首条用户消息的截断摘要；`latest_workflow_status` 为最近一次
    执行终态，可能为 None（尚无任何任务）。
    """

    id: str
    user_id: str | None = None
    status: str
    title: str
    latest_workflow_status: str | None = None
    latest_workflow_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    items: list[SessionSummaryResponse]
    page: int
    page_size: int
    total: int


class MessageRequest(BaseModel):
    content: str = Field(min_length=1)


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    agent_run_id: str | None = None
    status: str
    created_at: datetime


class MessageAcceptedResponse(BaseModel):
    message_id: str
    session_id: str
    agent_run_id: str
    workflow_id: str
    status: str


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    page: int
    page_size: int
    total: int


class WorkflowResponse(BaseModel):
    id: str
    session_id: str | None = None
    agent_run_id: str | None = None
    status: str
    current_step: str | None = None
    checkpoint: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class SessionActionResponse(BaseModel):
    session: SessionResponse
    workflow: WorkflowResponse | None = None


class AgentResponse(BaseModel):
    """生效的 Agent 配置（`doc/api.md` §5.2、§5.7）。

    `override_keys` 列出该角色**当前被显式覆盖**的字段名：前端据此区分「显式覆盖」
    与「回退值」，不必猜测某个值来自哪一层（ADR-013、ADR-017）。
    """

    id: str
    name: str
    role: str
    model: str
    provider: str = "ollama"
    provider_name: str | None = None
    llm_model_id: str | None = None
    temperature: float = 0.2
    top_p: float | None = None
    max_output_tokens: int | None = None
    reasoning_type: str = "none"
    status: str
    override_keys: list[str] = Field(default_factory=list)


class AgentListResponse(BaseModel):
    items: list[AgentResponse]


class AvailableModelResponse(BaseModel):
    """可在 §5.7 里绑定给角色的模型条目（只含 `enabled=true`）。"""

    id: str
    provider_id: str
    model: str
    name: str | None = None
    enabled: bool


class AgentConfigListResponse(BaseModel):
    """`GET /api/v1/config/agents` 的响应（`doc/api.md` §5.7）。"""

    items: list[AgentResponse]
    available_models: list[AvailableModelResponse] = Field(default_factory=list)


class AgentConfigPatchRequest(BaseModel):
    """`PATCH /api/v1/config/agents/{agent_id}` 的请求体（doc/api.md §5.7）。

    字段缺省 = 不改动；显式 `null` = 清除覆盖、回退下一层配置。
    """

    llm_model_id: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_type: str | None = Field(default=None, max_length=20)

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        resolved = value.strip()
        if not resolved:
            raise ValueError("model 不能为空")
        return resolved

    @model_validator(mode="after")
    def _require_any_field(self) -> AgentConfigPatchRequest:
        if not self.model_fields_set:
            raise ValueError("至少需要提供 llm_model_id/model/temperature/top_p/max_output_tokens/reasoning_type 之一")
        return self


class ProviderResponse(BaseModel):
    id: str
    name: str
    model: str
    base_url: str | None = None
    status: str
    temperature: float


class ProviderListResponse(BaseModel):
    items: list[ProviderResponse]


class ProviderConfigResponse(BaseModel):
    """`GET/PUT /api/v1/config/provider` 的响应（doc/api.md §5.8）。

    永不包含 `api_key` 原值，只回 `api_key_configured`。
    `default_llm_model_id` 是注册表默认路由（ADR-017）：非空时它指向的模型条目
    优先于本表的 legacy 五列，`llm_model_id` / `provider_name` / `preset_type` /
    `api_type` 是路由生效后解析出来的实际来源。
    """

    provider: str
    model: str
    base_url: str | None = None
    temperature: float
    top_p: float | None = None
    max_tokens: int | None = None
    api_key_configured: bool
    default_llm_model_id: str | None = None
    llm_model_id: str | None = None
    provider_name: str | None = None
    preset_type: str = "openai-compatible"
    api_type: str = "openai-compatible"
    reasoning_type: str = "none"
    updated_by: str | None = None
    updated_at: datetime | None = None


class ProviderConfigUpdateRequest(BaseModel):
    """`PUT /api/v1/config/provider` 的请求体（doc/api.md §5.8）。

    字段缺省 = 不改动；显式 `null` = 清除覆盖、回退环境配置。
    """

    provider: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    default_llm_model_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _require_any_field(self) -> ProviderConfigUpdateRequest:
        if not self.model_fields_set:
            raise ValueError(
                "至少需要提供 provider/model/base_url/api_key/temperature/default_llm_model_id 之一"
            )
        return self


# --------------------------------------------------------------------------- #
# 注册表契约（doc/api.md §5.9–§5.12、ADR-017）
# --------------------------------------------------------------------------- #


class _RequireAnyFieldMixin(BaseModel):
    """`PATCH` 请求体的共同约束：至少要给一个字段（全空 body 无意义）。"""

    @model_validator(mode="after")
    def _require_any_field(self):
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个可修改字段")
        return self


class ProviderRegistryResponse(BaseModel):
    """Provider 条目出参（`doc/api.md` §5.9）。`api_key` 永不回传。"""

    id: str
    name: str
    preset_type: str
    api_type: str
    base_url: str | None = None
    api_key_configured: bool = False
    custom_headers: dict[str, str] = Field(default_factory=dict)
    additional_settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    model_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class ProviderRegistryListResponse(BaseModel):
    items: list[ProviderRegistryResponse]
    total: int


class ProviderRegistryCreateRequest(BaseModel):
    """`POST /api/v1/config/providers` 的请求体（`doc/api.md` §5.9）。"""

    id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    preset_type: str = Field(default="openai-compatible", max_length=50)
    api_type: str | None = Field(default=None, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    custom_headers: dict[str, str] = Field(default_factory=dict)
    additional_settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _strip_id(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("id 不能为空")
        return resolved


class ProviderRegistryUpdateRequest(_RequireAnyFieldMixin):
    """`PATCH /api/v1/config/providers/{provider_id}`（`doc/api.md` §5.9）。

    缺省 = 不改动；显式 `null` = 回退默认值。`api_key` 的空串表示**不修改**。
    """

    name: str | None = Field(default=None, max_length=100)
    preset_type: str | None = Field(default=None, max_length=50)
    api_type: str | None = Field(default=None, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    custom_headers: dict[str, str] | None = None
    additional_settings: dict[str, Any] | None = None
    enabled: bool | None = None


class CustomParameterModel(BaseModel):
    """模型的特化参数条目（`doc/api.md` §5.10）。

    `value` 恒为字符串，`type` 说明如何解释它（`number` / `boolean` / `json` 在构造
    模型时按对应类型解析）——与参考实现「自定义参数 = 纯文本键值 + 类型标注」一致。
    """

    key: str = Field(min_length=1, max_length=100)
    value: str = ""
    type: Literal["text", "number", "boolean", "json"] = "text"


class ModelRegistryResponse(BaseModel):
    """模型条目出参（`doc/api.md` §5.10）。"""

    id: str
    provider_id: str
    model: str
    name: str | None = None
    enabled: bool = True
    reasoning_type: str = "none"
    temperature: float | None = None
    top_p: float | None = None
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    custom_parameters: list[dict[str, Any]] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class ModelRegistryListResponse(BaseModel):
    items: list[ModelRegistryResponse]
    total: int


class ProviderRegistryDetailResponse(ProviderRegistryResponse):
    """`GET /api/v1/config/providers/{provider_id}`：附带该 Provider 下的模型条目。"""

    models: list[ModelRegistryResponse] = Field(default_factory=list)


class ModelRegistryCreateRequest(BaseModel):
    """`POST /api/v1/config/models` 的请求体（`doc/api.md` §5.10）。"""

    provider_id: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    id: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    reasoning_type: str | None = Field(default=None, max_length=20)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_context_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    custom_parameters: list[CustomParameterModel] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("model 不能为空")
        return resolved


class ModelRegistryUpdateRequest(_RequireAnyFieldMixin):
    """`PATCH /api/v1/config/models/{model_id}`（`doc/api.md` §5.10）。

    缺省 = 不改动；显式 `null` = 回到「未设置」。`provider_id` 不可修改。
    """

    model: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    reasoning_type: str | None = Field(default=None, max_length=20)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_context_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    custom_parameters: list[CustomParameterModel] | None = None
    modalities: list[str] | None = None


class ModelBatchImportRequest(BaseModel):
    """`POST /api/v1/config/models/batch` 的请求体（`doc/api.md` §5.10）。

    已存在的 `(provider_id, model)` 计入 `skipped` 而不报错，因此重复提交幂等。
    """

    provider_id: str = Field(min_length=1, max_length=50)
    models: list[str] = Field(min_length=1, max_length=200)
    name_prefix: str = Field(default="", max_length=200)
    enabled: bool = True
    defaults: dict[str, Any] | None = None


class ModelBatchSkippedResponse(BaseModel):
    model: str
    reason: str


class ModelBatchImportResponse(BaseModel):
    provider_id: str
    created: list[str] = Field(default_factory=list)
    skipped: list[ModelBatchSkippedResponse] = Field(default_factory=list)
    total_requested: int


class DiscoveredModelResponse(BaseModel):
    id: str
    name: str
    owned_by: str | None = None


class ModelDiscoveryResponse(BaseModel):
    """`GET /api/v1/config/providers/{id}/models/discover`（`doc/api.md` §5.10）。"""

    provider_id: str
    source: str = "remote"
    source_url: str | None = None
    items: list[DiscoveredModelResponse] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)
    total: int = 0


class McpToolOptionModel(BaseModel):
    disabled: bool | None = None
    allowAutoExecution: bool | None = None


class McpServerResponse(BaseModel):
    """MCP Server 条目出参（`doc/api.md` §5.11）。"""

    id: str
    name: str
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tool_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tool_count: int = 0
    discovered_at: str | None = None
    server_info: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class McpServerListResponse(BaseModel):
    items: list[McpServerResponse]
    total: int


class McpServerCreateRequest(BaseModel):
    """`POST /api/v1/config/mcp/servers` 的请求体（`doc/api.md` §5.11）。

    `transport` 决定必填字段：`stdio` 要 `command`，`http`/`sse`/`ws` 要 `url`；
    两者错配由核心层校验成 422。
    """

    id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    transport: str = Field(max_length=20)
    command: str | None = Field(default=None, max_length=500)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] | None = None
    enabled: bool = True
    tool_options: dict[str, McpToolOptionModel] | None = None


class McpServerUpdateRequest(_RequireAnyFieldMixin):
    """`PATCH /api/v1/config/mcp/servers/{server_id}`（`doc/api.md` §5.11）。"""

    name: str | None = Field(default=None, max_length=100)
    transport: str | None = Field(default=None, max_length=20)
    command: str | None = Field(default=None, max_length=500)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    tool_options: dict[str, McpToolOptionModel] | None = None


class McpDiscoveredToolResponse(BaseModel):
    name: str
    description: str = ""


class McpDiscoveryResponse(BaseModel):
    """`POST /api/v1/config/mcp/servers/{id}/discover`（`doc/api.md` §5.11）。"""

    server_id: str
    server_info: dict[str, Any] = Field(default_factory=dict)
    tools: list[McpDiscoveredToolResponse] = Field(default_factory=list)
    total: int = 0
    discovered_at: str | None = None


class McpCompactToolResponse(BaseModel):
    """紧凑工具卡片（`doc/api.md` §5.11）：**不内联** `input_schema`。"""

    server_id: str
    server_name: str
    enabled: bool
    name: str
    description: str = ""
    tool_enabled: bool = True
    available: bool = True


class McpCompactServerResponse(BaseModel):
    id: str
    name: str
    transport: str
    enabled: bool
    tool_count: int = 0
    discovered_at: str | None = None


class McpCompactToolListResponse(BaseModel):
    items: list[McpCompactToolResponse]
    total: int
    servers: list[McpCompactServerResponse] = Field(default_factory=list)


class ProviderPresetResponse(BaseModel):
    preset_type: str
    label: str
    monogram: str | None = None
    tint: str | None = None
    category: str | None = None
    default_api_type: str
    supported_api_types: list[str] = Field(default_factory=list)
    default_base_url: str | None = None
    requires_api_key: bool = False
    api_key_url: str | None = None
    supports_model_discovery: bool = True


class ProviderPresetCategoryResponse(BaseModel):
    id: str
    label: str


class ProviderPresetCatalogResponse(BaseModel):
    items: list[ProviderPresetResponse]
    categories: list[ProviderPresetCategoryResponse] = Field(default_factory=list)


class ToolResponse(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    status: str


class ToolListResponse(BaseModel):
    items: list[ToolResponse]
    page: int
    page_size: int
    total: int
    availability: str = "not_integrated"


class ToolCallResponse(BaseModel):
    id: str
    run_id: str
    workflow_run_id: str | None = None
    tool_name: str
    input: dict[str, Any]
    output: Any | None = None
    status: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ToolCallListResponse(BaseModel):
    items: list[ToolCallResponse]
    page: int
    page_size: int
    total: int
    availability: str = "not_integrated"


class MetricResponse(BaseModel):
    metric_name: str
    value: FiniteFloat
    labels: dict[str, Any]
    recorded_at: datetime


class MetricListResponse(BaseModel):
    items: list[MetricResponse]
    page: int
    page_size: int
    total: int
    availability: str = "not_integrated"


app = FastAPI(
    title="Multi-Agent Collaboration Platform",
    version="0.1.0",
)

# The worker process uses the SQL adapter. Tests can replace this value with
# InMemoryApiStore without changing route behavior or requiring PostgreSQL.
api_store: Any = SqlApiStore()


def _tool_catalog() -> list[dict[str, Any]]:
    """注册表目录读取失败统一归一化为 503（doc/api.md §5.3）。

    MCP 传输（stdio/http）与注册表构建会抛出各不相同的异常类型，这里统一转成
    契约里的 `DATA_SOURCE_UNAVAILABLE`，避免把「工具源不可用」表现为 500。
    """

    try:
        return tool_catalog()
    except Exception as exc:
        log_event(
            logger,
            "tools.catalog_failed",
            level=logging.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise ApiError("DATA_SOURCE_UNAVAILABLE", "工具注册表读取失败，请稍后重试", 503) from exc


# 工具目录接线（doc/api.md §5.3）：注册表由成员 C 提供，API 只读取，不建表、不调用工具。
inspection_store = InspectionStore(tool_catalog=_tool_catalog)


def _inspection_read(read, **kwargs):
    try:
        return read(**kwargs)
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:
        raise ApiError("DATA_SOURCE_UNAVAILABLE", "数据源读取失败，请稍后重试", 503) from exc


# 注册表异常族：都是 `ModelRegistryError` / `McpRegistryError` 的子类，
# 顺序敏感——`_registry_api_error` 里必须先判子类再判基类。
_REGISTRY_ERRORS = (
    ProviderNotFoundError,
    ModelNotFoundError,
    ProviderInUseError,
    DuplicateEntryError,
    ModelDiscoveryError,
    McpServerNotFoundError,
    McpDiscoveryError,
    ModelRegistryError,
    McpRegistryError,
)


def _registry_api_error(exc: Exception) -> ApiError:
    """注册表异常 → 契约错误码（`doc/api.md` §5.9–§5.11）。"""

    if isinstance(exc, ProviderNotFoundError):
        return ApiError("PROVIDER_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)
    if isinstance(exc, ModelNotFoundError):
        return ApiError("MODEL_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)
    if isinstance(exc, McpServerNotFoundError):
        return ApiError("MCP_SERVER_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)
    if isinstance(exc, ProviderInUseError):
        return ApiError("PROVIDER_IN_USE", str(exc), status.HTTP_409_CONFLICT)
    if isinstance(exc, DuplicateEntryError):
        # 同类条目已存在：契约里归到 409 VALIDATION_ERROR（§5.9 / §5.10）。
        return ApiError("VALIDATION_ERROR", str(exc), status.HTTP_409_CONFLICT)
    if isinstance(exc, ModelDiscoveryError):
        return ApiError(
            "PROVIDER_DISCOVERY_FAILED", str(exc), status.HTTP_502_BAD_GATEWAY
        )
    if isinstance(exc, McpDiscoveryError):
        return ApiError("MCP_DISCOVERY_FAILED", str(exc), status.HTTP_502_BAD_GATEWAY)
    if isinstance(exc, (ModelRegistryError, McpRegistryError)):
        return ApiError(
            "VALIDATION_ERROR", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    return ApiError("DATA_SOURCE_UNAVAILABLE", "注册表读写失败，请稍后重试", 503)


def _registry_call(call: Callable[[], Any], *, failure_message: str) -> Any:
    """执行一次注册表调用并翻译异常；存储类异常统一归为 503。"""

    try:
        return call()
    except _REGISTRY_ERRORS as exc:
        raise _registry_api_error(exc) from exc
    except IntegrityError as exc:
        # 竞态下的唯一致命冲突：上一层的「先查后写」没拦住，这里兜底成 409。
        raise ApiError("VALIDATION_ERROR", "条目已存在", status.HTTP_409_CONFLICT) from exc
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:
        raise ApiError("DATA_SOURCE_UNAVAILABLE", failure_message, 503) from exc


@app.exception_handler(ApiError)
def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "request_id": request_id},
    )


def _session_or_404(session_id: str) -> dict[str, Any]:
    try:
        session = api_store.get_session(session_id)
    except (ValueError, TypeError):
        session = None
    if session is None:
        raise ApiError("SESSION_NOT_FOUND", "会话不存在", status.HTTP_404_NOT_FOUND)
    return session


def _workflow_response(row: dict[str, Any]) -> WorkflowResponse:
    return WorkflowResponse(
        id=row["id"],
        session_id=row.get("session_id"),
        agent_run_id=row.get("agent_run_id"),
        status=row["status"],
        current_step=row.get("current_step"),
        checkpoint=row.get("checkpoint"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row.get("completed_at"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/metrics",
    summary="Prometheus 文本指标（doc/api.md §5.6）",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
def prometheus_metrics() -> Response:
    """输出进程内 Prometheus 注册表文本；不读数据库，表缺失也不影响。"""

    return Response(content=render_prometheus_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/sessions", response_model=SessionResponse, status_code=201)
def create_session(payload: SessionCreateRequest) -> SessionResponse:
    return SessionResponse.model_validate(api_store.create_session(payload.user_id))


@app.get("/api/v1/sessions", response_model=SessionListResponse)
def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> SessionListResponse:
    items, total = api_store.list_sessions(page=page, page_size=page_size)
    return SessionListResponse(
        items=[SessionSummaryResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@app.get("/api/v1/sessions/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    return SessionResponse.model_validate(_session_or_404(session_id))


@app.delete("/api/v1/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    """删除会话及其关联数据（消息 / 运行记录 / 工具调用 / 观测采样）。

    - 会话不存在返回 `404 SESSION_NOT_FOUND`；
    - 删除成功返回 `204`（无正文）。
    """

    _session_or_404(session_id)
    api_store.delete_session(session_id)
    return Response(status_code=204)


@app.post(
    "/api/v1/sessions/{session_id}/messages",
    response_model=MessageAcceptedResponse,
    status_code=202,
)
def send_message(session_id: str, payload: MessageRequest) -> MessageAcceptedResponse:
    session = _session_or_404(session_id)
    if session["status"] == "paused":
        raise ApiError("SESSION_PAUSED", "会话已暂停，不能接收新消息", status.HTTP_409_CONFLICT)

    agent_run = api_store.create_agent_run(session_id)
    message = api_store.create_message(
        session_id,
        content=payload.content,
        agent_run_id=agent_run["id"],
    )
    workflow_id = str(uuid.uuid4())
    api_store.create_workflow(
        workflow_id,
        session_id=session_id,
        agent_run_id=agent_run["id"],
    )
    try:
        api_store.update_workflow(workflow_id, status="running")
        api_store.update_agent_run_status(agent_run["id"], "running")
        api_store.update_message_status(message["id"], "running")
        get_workflow_service().schedule(
            WorkflowTask(
                workflow_id=workflow_id,
                task=payload.content,
                session_id=session_id,
                agent_run_id=agent_run["id"],
                message_id=message["id"],
            )
        )
    except Exception as exc:
        api_store.update_agent_run_status(agent_run["id"], "failed")
        api_store.update_workflow(workflow_id, status="failed")
        api_store.update_message_status(message["id"], "failed")
        raise ApiError("INTERNAL_ERROR", "Workflow 调度失败", status.HTTP_500_INTERNAL_SERVER_ERROR) from exc

    return MessageAcceptedResponse(
        message_id=message["id"],
        session_id=session_id,
        agent_run_id=agent_run["id"],
        workflow_id=workflow_id,
        status="pending",
    )


@app.get("/api/v1/sessions/{session_id}/messages", response_model=MessageListResponse)
def list_session_messages(
    session_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> MessageListResponse:
    _session_or_404(session_id)
    items, total = api_store.list_messages(session_id, page=page, page_size=page_size)
    return MessageListResponse(
        items=[MessageResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@app.post("/api/v1/sessions/{session_id}/pause", response_model=SessionActionResponse)
def pause_session(session_id: str) -> SessionActionResponse:
    session = _session_or_404(session_id)
    if session["status"] == "paused":
        return SessionActionResponse(session=SessionResponse.model_validate(session))

    workflow = api_store.latest_workflow(session_id, statuses={"running"})
    if workflow is not None:
        try:
            get_workflow_service().pause(workflow["id"])
        except Exception as exc:
            raise ApiError(
                "WORKFLOW_NOT_PAUSABLE", "Workflow 当前状态不允许暂停", status.HTTP_409_CONFLICT
            ) from exc
        api_store.update_workflow(workflow["id"], status="paused")
        api_store.update_agent_run_status(workflow["agent_run_id"], "paused")

    updated = api_store.update_session_status(session_id, "paused")
    latest = api_store.get_workflow(workflow["id"]) if workflow else None
    return SessionActionResponse(
        session=SessionResponse.model_validate(updated),
        workflow=_workflow_response(latest) if latest else None,
    )


@app.post("/api/v1/sessions/{session_id}/resume", response_model=SessionActionResponse)
def resume_session(session_id: str) -> SessionActionResponse:
    _session_or_404(session_id)
    workflow = api_store.latest_workflow(session_id, statuses={"paused"})
    if workflow is not None:
        try:
            get_workflow_service().resume(workflow["id"])
        except Exception as exc:
            raise ApiError(
                "WORKFLOW_NOT_PAUSABLE", "Workflow 当前状态不允许恢复", status.HTTP_409_CONFLICT
            ) from exc
        api_store.update_workflow(workflow["id"], status="running")
        api_store.update_agent_run_status(workflow["agent_run_id"], "running")

    updated = api_store.update_session_status(session_id, "active")
    latest = api_store.get_workflow(workflow["id"]) if workflow else None
    return SessionActionResponse(
        session=SessionResponse.model_validate(updated),
        workflow=_workflow_response(latest) if latest else None,
    )


@app.get("/api/v1/workflows/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(workflow_id: str) -> WorkflowResponse:
    try:
        workflow = api_store.get_workflow(workflow_id)
    except (ValueError, TypeError):
        workflow = None
    if workflow is None:
        raise ApiError("WORKFLOW_NOT_FOUND", "Workflow 不存在", status.HTTP_404_NOT_FOUND)
    return _workflow_response(workflow)


@app.get("/api/v1/agents", response_model=AgentListResponse)
def list_agents() -> AgentListResponse:
    """Return the fixed D5-D6 team represented by the three pipeline roles."""
    return AgentListResponse(
        items=[_agent_response(agent_id) for agent_id in _AGENT_NAMES]
    )


_AGENT_NAMES = {
    "collector": "信息收集 Agent",
    "analyst": "数据分析 Agent",
    "reporter": "报告生成 Agent",
}


def _agent_response(agent_id: str) -> AgentResponse:
    """角色的生效配置 + 「哪些字段被显式覆盖」。"""

    if agent_id not in _AGENT_NAMES:
        raise ApiError("AGENT_NOT_FOUND", "Agent 不存在", status.HTTP_404_NOT_FOUND)
    # 覆盖表读一次喂给解析与 override_keys，避免同一请求读两遍存储。
    overrides = agent_config_reader()
    settings = resolve_agent_settings(agent_id, get_settings(), overrides=overrides)
    return AgentResponse(
        id=agent_id,
        name=_AGENT_NAMES[agent_id],
        role=agent_id,
        model=settings.ollama_model if settings.llm_provider == "ollama" else settings.openai_model,
        provider=settings.llm_provider,
        provider_name=settings.provider_name or None,
        llm_model_id=settings.llm_model_id or None,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_output_tokens=settings.max_tokens,
        reasoning_type=settings.reasoning_type,
        status="idle",
        override_keys=effective_override_keys(overrides.get(agent_id)),
    )


def _available_models() -> list[dict[str, Any]]:
    """供 §5.7 角色绑定选择器使用的模型清单：只含启用条目，注册表不可用时为空。

    列表读失败不阻断配置页渲染（配置读取不阻断业务，ADR-013/014 同构）；
    因此这里吞掉异常并返回空表，让「模型清单」退化成「暂时没有可选模型」。
    """

    try:
        items = model_registry.list_models(enabled=True)["items"]
    except Exception as exc:  # pragma: no cover - 取决于存储可用性
        log_event(
            logger,
            "config.agents.model_catalog_failed",
            level=logging.WARNING,
            error=f"{type(exc).__name__}: {exc}",
            hint="返回空模型清单，不影响角色配置展示",
        )
        return []
    # checkpoint 已按 (provider_id, model) 排序，这里只挑出契约承诺的字段。
    return [
        {
            "id": item["id"],
            "provider_id": item["provider_id"],
            "model": item["model"],
            "name": item.get("name"),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in items
    ]


def _patch_field(payload: BaseModel, field: str) -> Any:
    """把「字段缺省」映射成 UNSET、「显式 null」映射成 None（清除覆盖）。"""

    return getattr(payload, field) if field in payload.model_fields_set else UNSET


@app.patch(
    "/api/v1/config/agents/{agent_id}",
    response_model=AgentResponse,
)
def patch_agent_config(
    agent_id: str,
    payload: AgentConfigPatchRequest,
    request: Request,
) -> AgentResponse:
    """修改角色的 model / temperature 覆盖值，下一次阶段执行即生效。"""

    if agent_id not in _AGENT_NAMES:
        raise ApiError("AGENT_NOT_FOUND", "Agent 不存在", status.HTTP_404_NOT_FOUND)
    try:
        update_agent_config(
            agent_id,
            llm_model_id=_patch_field(payload, "llm_model_id"),
            model=_patch_field(payload, "model"),
            temperature=_patch_field(payload, "temperature"),
            top_p=_patch_field(payload, "top_p"),
            max_output_tokens=_patch_field(payload, "max_output_tokens"),
            reasoning_type=_patch_field(payload, "reasoning_type"),
            actor=request.headers.get("X-Request-ID"),
        )
    except AgentConfigError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:
        raise ApiError("DATA_SOURCE_UNAVAILABLE", "配置写入失败，请稍后重试", 503) from exc
    return _agent_response(agent_id)


@app.get("/api/v1/config/agents", response_model=AgentConfigListResponse)
def list_agent_configs() -> AgentConfigListResponse:
    """一次取回全部角色的生效配置与可选模型清单（`doc/api.md` §5.7）。

    配置页据此渲染「角色 → 模型绑定 + 调参」表单，不需要 N+1 次请求。
    """

    return AgentConfigListResponse(
        items=[_agent_response(agent_id) for agent_id in _AGENT_NAMES],
        available_models=[
            AvailableModelResponse(**item) for item in _available_models()
        ],
    )


@app.get("/api/v1/config/provider", response_model=ProviderConfigResponse)
def get_model_provider_config() -> ProviderConfigResponse:
    """读取生效的模型 Provider 配置（不返回密钥，见 doc/api.md §5.8）。"""

    settings = resolve_provider_settings(get_settings())
    return ProviderConfigResponse(**effective_provider_view(settings))


@app.put("/api/v1/config/provider", response_model=ProviderConfigResponse)
def put_model_provider_config(
    payload: ProviderConfigUpdateRequest,
    request: Request,
) -> ProviderConfigResponse:
    """写入 Provider 覆盖值（PostgreSQL 事实源 + Redis 镜像），下一次任务即生效。"""

    try:
        update_provider_config(
            provider=_patch_field(payload, "provider"),
            model=_patch_field(payload, "model"),
            base_url=_patch_field(payload, "base_url"),
            api_key=_patch_field(payload, "api_key"),
            temperature=_patch_field(payload, "temperature"),
            default_llm_model_id=_patch_field(payload, "default_llm_model_id"),
            actor=request.headers.get("X-Request-ID"),
        )
    except ProviderConfigError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:
        raise ApiError("DATA_SOURCE_UNAVAILABLE", "配置写入失败，请稍后重试", 503) from exc
    settings = resolve_provider_settings(get_settings())
    return ProviderConfigResponse(**effective_provider_view(settings))


@app.get("/api/v1/providers", response_model=ProviderListResponse)
def list_providers() -> ProviderListResponse:
    settings = resolve_provider_settings(get_settings())
    if settings.llm_provider == "ollama":
        model = settings.ollama_model
        base_url = settings.ollama_base_url
    else:
        model = settings.openai_model
        base_url = settings.openai_base_url
    base_url = sanitize_base_url(base_url)
    model_status = "configured" if model else "missing_model"
    return ProviderListResponse(
        items=[
            ProviderResponse(
                id=settings.llm_provider,
                name="Ollama" if settings.llm_provider == "ollama" else "OpenAI",
                model=model,
                base_url=base_url,
                status=model_status,
                temperature=settings.temperature,
            )
        ]
    )


@app.get("/api/v1/agents/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str) -> AgentResponse:
    return _agent_response(agent_id)


@app.get("/api/v1/tools", response_model=ToolListResponse)
def list_tools(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ToolListResponse:
    return ToolListResponse.model_validate(
        _inspection_read(inspection_store.tools, page=page, page_size=page_size)
    )


@app.get(
    "/api/v1/workflows/{workflow_id}/tool-calls",
    response_model=ToolCallListResponse,
)
def list_workflow_tool_calls(
    workflow_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ToolCallListResponse:
    try:
        workflow = api_store.get_workflow(workflow_id)
    except (ValueError, TypeError):
        workflow = None
    if workflow is None:
        raise ApiError("WORKFLOW_NOT_FOUND", "Workflow 不存在", status.HTTP_404_NOT_FOUND)
    return ToolCallListResponse.model_validate(
        _inspection_read(inspection_store.tool_calls, workflow=workflow, page=page, page_size=page_size)
    )


@app.get("/api/v1/metrics", response_model=MetricListResponse)
def list_metrics(
    workflow_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> MetricListResponse:
    if workflow_id is not None:
        get_workflow(workflow_id)
    return MetricListResponse.model_validate(
        _inspection_read(inspection_store.metrics, page=page, page_size=page_size, workflow_id=workflow_id)
    )


# --------------------------------------------------------------------------- #
# §5.12 Provider 预设目录
# --------------------------------------------------------------------------- #


@app.get("/api/v1/config/provider-presets", response_model=ProviderPresetCatalogResponse)
def get_provider_presets() -> ProviderPresetCatalogResponse:
    """返回内置预设族目录，供 Provider 表单预填（`doc/api.md` §5.12）。

    纯静态数据：不读数据库、不需要凭据，因此即使存储不可用也能渲染配置页。
    """

    return ProviderPresetCatalogResponse.model_validate(
        model_registry.provider_preset_catalog()
    )


# --------------------------------------------------------------------------- #
# §5.9 Provider 注册表
# --------------------------------------------------------------------------- #


@app.get("/api/v1/config/providers", response_model=ProviderRegistryListResponse)
def list_provider_registry(enabled: bool | None = None) -> ProviderRegistryListResponse:
    """列出全部 Provider 条目（`doc/api.md` §5.9）。`api_key` 永不回传。"""

    data = _registry_call(
        lambda: model_registry.list_providers(),
        failure_message="Provider 注册表读取失败，请稍后重试",
    )
    if enabled is not None:
        items = [item for item in data["items"] if item["enabled"] is enabled]
        data = {"items": items, "total": len(items)}
    return ProviderRegistryListResponse.model_validate(data)


@app.post(
    "/api/v1/config/providers",
    response_model=ProviderRegistryResponse,
    status_code=201,
)
def create_provider_registry(
    payload: ProviderRegistryCreateRequest,
    request: Request,
) -> ProviderRegistryResponse:
    """登记一个 Provider 端点与凭据（`doc/api.md` §5.9）。

    同一 id 重复登记返回 `409 VALIDATION_ERROR`；`api_key` 只写入、不回读。
    """

    data = _registry_call(
        lambda: model_registry.create_provider(
            provider_id=payload.id,
            name=payload.name,
            preset_type=payload.preset_type,
            api_type=payload.api_type if payload.api_type is not None else UNSET,
            # 空串/未填 = 用预设默认端点，而不是「显式设置空 base_url」。
            base_url=payload.base_url or UNSET,
            api_key=payload.api_key or UNSET,
            custom_headers=payload.custom_headers or UNSET,
            additional_settings=payload.additional_settings or UNSET,
            enabled=payload.enabled,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="Provider 注册表写入失败，请稍后重试",
    )
    return ProviderRegistryResponse.model_validate(data)


@app.get(
    "/api/v1/config/providers/{provider_id}",
    response_model=ProviderRegistryDetailResponse,
)
def get_provider_registry(provider_id: str) -> ProviderRegistryDetailResponse:
    """Provider 详情，附带该 Provider 下的模型条目（`doc/api.md` §5.9）。"""

    data = _registry_call(
        lambda: model_registry.get_provider(provider_id),
        failure_message="Provider 注册表读取失败，请稍后重试",
    )
    return ProviderRegistryDetailResponse.model_validate(data)


@app.patch(
    "/api/v1/config/providers/{provider_id}",
    response_model=ProviderRegistryResponse,
)
def patch_provider_registry(
    provider_id: str,
    payload: ProviderRegistryUpdateRequest,
    request: Request,
) -> ProviderRegistryResponse:
    """部分更新 Provider（`doc/api.md` §5.9）：缺省 = 不改动，显式 `null` = 回退默认值。"""

    data = _registry_call(
        lambda: model_registry.update_provider(
            provider_id,
            name=_patch_field(payload, "name"),
            preset_type=_patch_field(payload, "preset_type"),
            api_type=_patch_field(payload, "api_type"),
            base_url=_patch_field(payload, "base_url"),
            api_key=_patch_field(payload, "api_key"),
            custom_headers=_patch_field(payload, "custom_headers"),
            additional_settings=_patch_field(payload, "additional_settings"),
            enabled=_patch_field(payload, "enabled"),
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="Provider 注册表写入失败，请稍后重试",
    )
    return ProviderRegistryResponse.model_validate(data)


@app.delete("/api/v1/config/providers/{provider_id}", status_code=204)
def delete_provider_registry(
    provider_id: str,
    request: Request,
    force: bool = Query(default=False),
) -> Response:
    """删除 Provider 及其模型条目（`doc/api.md` §5.9）。

    默认拒绝仍有启用模型的 Provider（`409 PROVIDER_IN_USE`）；`?force=true` 才级联删除。
    """

    _registry_call(
        lambda: model_registry.delete_provider(
            provider_id,
            force=force,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="Provider 注册表写入失败，请稍后重试",
    )
    return Response(status_code=204)


@app.get(
    "/api/v1/config/providers/{provider_id}/models/discover",
    response_model=ModelDiscoveryResponse,
)
def discover_provider_model_catalog(provider_id: str) -> ModelDiscoveryResponse:
    """从 Provider 远端拉取模型清单（`doc/api.md` §5.10）。

    由**服务端**发起出站请求：浏览器直连会撞 CORS 且会外泄凭据。该接口不写数据库，
    失败统一 `502 PROVIDER_DISCOVERY_FAILED`。
    """

    data = _registry_call(
        lambda: model_registry.discover_provider_models(
            provider_id, fetcher=model_discovery_fetcher
        ),
        # 远端不可达属于上游问题，不走 503（那是本站存储故障）。
        failure_message="Provider 模型清单读取失败，请稍后重试",
    )
    return ModelDiscoveryResponse.model_validate(data)


# --------------------------------------------------------------------------- #
# §5.10 模型注册表与批量引入
# --------------------------------------------------------------------------- #


@app.get("/api/v1/config/models", response_model=ModelRegistryListResponse)
def list_model_registry(
    provider_id: str | None = None,
    enabled: bool | None = None,
) -> ModelRegistryListResponse:
    """列出模型条目，可按 Provider 与启用状态过滤（`doc/api.md` §5.10）。"""

    data = _registry_call(
        lambda: model_registry.list_models(provider_id=provider_id, enabled=enabled),
        failure_message="模型注册表读取失败，请稍后重试",
    )
    return ModelRegistryListResponse.model_validate(data)


@app.post("/api/v1/config/models", response_model=ModelRegistryResponse, status_code=201)
def create_model_registry(
    payload: ModelRegistryCreateRequest,
    request: Request,
) -> ModelRegistryResponse:
    """登记单个模型条目并设置特化调参（`doc/api.md` §5.10）。"""

    data = _registry_call(
        lambda: model_registry.create_model(
            provider_id=payload.provider_id,
            model=payload.model,
            model_id=payload.id or UNSET,
            name=payload.name if payload.name is not None else UNSET,
            enabled=payload.enabled,
            # 条目表的 reasoning_type 非空：显式 null 按「无推理」落库。
            reasoning_type=payload.reasoning_type or "none",
            temperature=payload.temperature,
            top_p=payload.top_p,
            max_context_tokens=payload.max_context_tokens,
            max_output_tokens=payload.max_output_tokens,
            custom_parameters=[
                item.model_dump() for item in payload.custom_parameters
            ],
            modalities=payload.modalities,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="模型注册表写入失败，请稍后重试",
    )
    return ModelRegistryResponse.model_validate(data)


@app.post(
    "/api/v1/config/models/batch",
    response_model=ModelBatchImportResponse,
)
def batch_import_model_registry(
    payload: ModelBatchImportRequest,
    request: Request,
) -> ModelBatchImportResponse:
    """批量引入模型（`doc/api.md` §5.10）。

    已存在的 `(provider_id, model)` 计入 `skipped` 而不报错——重复提交天然幂等，
    因此前端可以放心地把「发现结果全选」再提交一次。
    """

    data = _registry_call(
        lambda: model_registry.batch_import_models(
            provider_id=payload.provider_id,
            models=payload.models,
            name_prefix=payload.name_prefix,
            enabled=payload.enabled,
            defaults=payload.defaults if payload.defaults is not None else UNSET,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="模型注册表写入失败，请稍后重试",
    )
    return ModelBatchImportResponse.model_validate(data)


@app.patch("/api/v1/config/models/{model_id}", response_model=ModelRegistryResponse)
def patch_model_registry(
    model_id: str,
    payload: ModelRegistryUpdateRequest,
    request: Request,
) -> ModelRegistryResponse:
    """调整模型条目的开关与特化调参（`doc/api.md` §5.10）。

    `provider_id` 不可修改：换 Provider 等于换端点与凭据，应新建条目再删除旧的。
    """

    data = _registry_call(
        lambda: model_registry.update_model(
            model_id,
            model=_patch_field(payload, "model"),
            name=_patch_field(payload, "name"),
            enabled=_patch_field(payload, "enabled"),
            # 同上：非空列，显式 null 归一到 "none"。
            reasoning_type=(
                "none"
                if "reasoning_type" in payload.model_fields_set
                and payload.reasoning_type is None
                else _patch_field(payload, "reasoning_type")
            ),
            temperature=_patch_field(payload, "temperature"),
            top_p=_patch_field(payload, "top_p"),
            max_context_tokens=_patch_field(payload, "max_context_tokens"),
            max_output_tokens=_patch_field(payload, "max_output_tokens"),
            custom_parameters=(
                [item.model_dump() for item in payload.custom_parameters]
                if payload.custom_parameters is not None
                else _patch_field(payload, "custom_parameters")
            ),
            modalities=_patch_field(payload, "modalities"),
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="模型注册表写入失败，请稍后重试",
    )
    return ModelRegistryResponse.model_validate(data)


@app.delete("/api/v1/config/models/{model_id}", status_code=204)
def delete_model_registry(model_id: str, request: Request) -> Response:
    """删除模型条目（`doc/api.md` §5.10）。

    引用它的 `agent_configs.llm_model_id` / `provider_configs.default_llm_model_id`
    退化为「未绑定」而不报错——逻辑引用不建外键（ADR-017）。
    """

    _registry_call(
        lambda: model_registry.delete_model(
            model_id, actor=request.headers.get("X-Request-ID")
        ),
        failure_message="模型注册表写入失败，请稍后重试",
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# §5.11 MCP Server 注册表与工具目录
# --------------------------------------------------------------------------- #


@app.get("/api/v1/config/mcp/servers", response_model=McpServerListResponse)
def list_mcp_server_registry() -> McpServerListResponse:
    """列出 MCP Server 条目（`doc/api.md` §5.11）。"""

    data = _registry_call(
        lambda: mcp_registry.list_servers(),
        failure_message="MCP 注册表读取失败，请稍后重试",
    )
    return McpServerListResponse.model_validate(data)


@app.post(
    "/api/v1/config/mcp/servers",
    response_model=McpServerResponse,
    status_code=201,
)
def create_mcp_server_registry(
    payload: McpServerCreateRequest,
    request: Request,
) -> McpServerResponse:
    """登记一个 MCP Server（`doc/api.md` §5.11）。

    传输方式与参数字段的匹配由核心层校验：`stdio` 要 `command`、远程要 `url`，
    错配返回 `422 VALIDATION_ERROR`。
    """

    tool_options = (
        None
        if payload.tool_options is None
        else {
            name: {key: value for key, value in options.model_dump().items() if value is not None}
            for name, options in payload.tool_options.items()
        }
    )
    data = _registry_call(
        lambda: mcp_registry.create_server(
            server_id=payload.id,
            name=payload.name,
            transport=payload.transport,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            cwd=payload.cwd,
            url=payload.url,
            headers=payload.headers,
            enabled=payload.enabled,
            tool_options=tool_options,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="MCP 注册表写入失败，请稍后重试",
    )
    return McpServerResponse.model_validate(data)


@app.get("/api/v1/config/mcp/tools", response_model=McpCompactToolListResponse)
def list_mcp_compact_tools() -> McpCompactToolListResponse:
    """按 Server 分组的**紧凑**工具目录（`doc/api.md` §5.11）。

    刻意不内联 `input_schema`：Schema 体积大，且多数时候不影响「这个工具要不要开」
    的判断。需要完整 Schema 时走 §5.3 的 `GET /api/v1/tools`。
    """

    data = _registry_call(
        lambda: mcp_registry.compact_tool_catalog(),
        failure_message="MCP 工具目录读取失败，请稍后重试",
    )
    return McpCompactToolListResponse.model_validate(data)


@app.get(
    "/api/v1/config/mcp/servers/{server_id}",
    response_model=McpServerResponse,
)
def get_mcp_server_registry(server_id: str) -> McpServerResponse:
    data = _registry_call(
        lambda: mcp_registry.get_server(server_id),
        failure_message="MCP 注册表读取失败，请稍后重试",
    )
    return McpServerResponse.model_validate(data)


@app.patch(
    "/api/v1/config/mcp/servers/{server_id}",
    response_model=McpServerResponse,
)
def patch_mcp_server_registry(
    server_id: str,
    payload: McpServerUpdateRequest,
    request: Request,
) -> McpServerResponse:
    """部分更新 MCP Server（`doc/api.md` §5.11）：缺省 = 不改动，显式 `null` = 清除。"""

    tool_options = _patch_field(payload, "tool_options")
    if tool_options is not None and tool_options is not UNSET:
        tool_options = {
            name: {key: value for key, value in options.items() if value is not None}
            for name, options in tool_options.items()
        }
    data = _registry_call(
        lambda: mcp_registry.update_server(
            server_id,
            name=_patch_field(payload, "name"),
            transport=_patch_field(payload, "transport"),
            command=_patch_field(payload, "command"),
            args=_patch_field(payload, "args"),
            env=_patch_field(payload, "env"),
            cwd=_patch_field(payload, "cwd"),
            url=_patch_field(payload, "url"),
            headers=_patch_field(payload, "headers"),
            enabled=_patch_field(payload, "enabled"),
            tool_options=tool_options,
            actor=request.headers.get("X-Request-ID"),
        ),
        failure_message="MCP 注册表写入失败，请稍后重试",
    )
    return McpServerResponse.model_validate(data)


@app.delete("/api/v1/config/mcp/servers/{server_id}", status_code=204)
def delete_mcp_server_registry(server_id: str, request: Request) -> Response:
    """删除 MCP Server 条目（`doc/api.md` §5.11）。"""

    _registry_call(
        lambda: mcp_registry.delete_server(
            server_id, actor=request.headers.get("X-Request-ID")
        ),
        failure_message="MCP 注册表写入失败，请稍后重试",
    )
    return Response(status_code=204)


@app.post(
    "/api/v1/config/mcp/servers/{server_id}/discover",
    response_model=McpDiscoveryResponse,
)
def discover_mcp_server(server_id: str) -> McpDiscoveryResponse:
    """连接 Server、握手并缓存工具目录（`doc/api.md` §5.11）。

    只写 `discovered` 缓存，不改 `enabled`；失败统一 `502 MCP_DISCOVERY_FAILED`，
    错误信息不含凭据。由用户显式触发，不在页面加载时自动调用。
    """

    data = _registry_call(
        lambda: mcp_registry.discover_server(
            server_id, discoverer=mcp_server_discoverer
        ),
        failure_message="MCP Server 发现失败，请稍后重试",
    )
    return McpDiscoveryResponse.model_validate(data)

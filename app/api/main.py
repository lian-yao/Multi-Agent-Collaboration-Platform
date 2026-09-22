from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Literal
from urllib.parse import quote

from dapr.clients.exceptions import DaprGrpcError
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, FiniteFloat, field_validator, model_validator
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.stage_trace import read_stage_traces
from app.api.store import SqlApiStore
from app.api.inspection import InspectionStore
from app.attachments import (
    MAX_FILES_PER_MESSAGE,
    AttachmentRejected,
    prepare_upload,
)
from app.config import get_settings
from app.core import mcp_registry, model_registry
from app.workspace import service as workspace_service
from app.workspace import (
    WorkspaceApprovalRequired,
    WorkspaceDisabled,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceRootUnavailable,
)
from app.core.agent_config import (
    AgentConfigError,
    OVERRIDE_FIELDS,
    agent_config_overrides,
    create_agent_registry,
    delete_agent_registry,
    effective_override_keys,
    list_agent_registry,
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
from app.memory import MessageRole, MessageStatus, SessionMessage
from app.memory.runtime import conversation_memory
from app.observability.logging import get_logger, log_event
from app.observability.metrics import render_prometheus_metrics
from app.sandbox import build_sandbox, get_sandbox_settings
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
    content: str = ""
    """消息正文。**可以只带附件不带文字**（截图提问是很常见的用法），
    因此这里不再要求 `min_length=1`，改由下面的校验保证二者不全空。"""

    attachment_ids: list[str] = Field(
        default_factory=list, max_length=MAX_FILES_PER_MESSAGE
    )
    """先经 `POST /api/v1/attachments` 上传得到的附件 id（`doc/api.md` §5.16）。

    只传 id 不传内容：附件内容可能是一张 5 MB 的图片，塞进请求体会同时顶爆
    Dapr 的活动输入体积上限与模型请求体积上限。
    """

    orchestration_mode: Literal["static", "dynamic"] | None = None
    """单次执行的编排模式覆盖（ADR-019）。

    省略时用服务端 `AGENT_ORCHESTRATION_MODE`（默认 `static`）。
    """

    @model_validator(mode="after")
    def _require_content_or_attachment(self) -> MessageRequest:
        if not self.content.strip() and not self.attachment_ids:
            raise ValueError("content 与 attachment_ids 不能同时为空。")
        return self


class AttachmentResponse(BaseModel):
    id: str
    session_id: str | None = None
    message_id: str | None = None
    name: str
    mime: str = ""
    size_bytes: int = 0
    kind: str
    """`image` / `text` / `document`——按「怎么被模型消费」分类，不是文件类型。"""

    status: str
    """`ready` / `failed`。`failed` 是**上传成功但解析失败**（例如扫描版 PDF）：
    附件仍然在，但正文取不出来，前端要显式标注，不能让用户以为它被用上了。"""

    error: str | None = None
    has_original: bool = False
    """原件字节是否还在库里（ADR-024）。

    `False` 只出现在 ADR-024 之前落库的文本/文档附件上（那时字节用完即弃）。
    界面据此决定要不要给「下载原件」入口——不显示一个必然 404 的链接。
    """

    created_at: datetime


class AttachmentUploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mime: str = Field(default="", max_length=120)
    data_base64: str = ""
    """base64 编码的原始字节（可带 `data:` 前缀，服务端会剥掉）。

    用 JSON + base64 而不是 multipart：`python-multipart` 在本项目里只是
    `mcp` 的传递依赖，把它变成上传链路的一等依赖需要改 `pyproject.toml` 与锁文件；
    base64 有约 33% 的体积开销，但换来零新增依赖与前后端统一的 JSON 契约。

    这里**不设** `min_length`：空文件由 `prepare_upload` 报 `ATTACHMENT_EMPTY`，
    那个错误码比 "String should have at least 1 character" 对用户有意义得多。
    """


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    agent_run_id: str | None = None
    status: str
    created_at: datetime
    attachments: list[AttachmentResponse] = Field(default_factory=list)


class MessageAcceptedResponse(BaseModel):
    message_id: str
    session_id: str
    agent_run_id: str
    workflow_id: str
    status: str
    attachments: list["AttachmentResponse"] = Field(default_factory=list)
    unattached_attachment_ids: list[str] = Field(default_factory=list)
    """请求里带了、但没能挂上这条消息的附件 id（不存在 / 已被别的消息挂走 / 格式非法）。

    单独回一个字段而不是并进错误码：消息本身是发成功的，附件缺一个是**部分失败**，
    报成 4xx 会让前端把已经发出去的消息当成没发出去。前端据此提示并保留本地文件。
    """


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


class WorkflowListResponse(BaseModel):
    """会话的协作工作流列表（`doc/api.md` §5.18）。

    **升序**返回，前端据此给每个对话编号（第 1 / 2 / 3 个对话）；`total` 是本次会话
    实际的工作流条数，不是分页总数——这个列表不分页，一个会话的工作流条数就是它的
    对话轮数，量级天然很小。
    """

    items: list[WorkflowResponse]
    total: int


class AgentResponse(BaseModel):
    """生效的 Agent 配置（`doc/api.md` §5.2、§5.7）。

    `override_keys` 列出该角色**当前被显式覆盖**的字段名：前端据此区分「显式覆盖」
    与「回退值」，不必猜测某个值来自哪一层（ADR-013、ADR-017）。
    `builtin` / `description` / `enabled` 来自角色目录（`agent_registry`），
    用于区分「内置流水线角色」与「自定义角色」。
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
    builtin: bool = False
    description: str | None = None
    enabled: bool = True


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


class AgentRegistryCreateRequest(BaseModel):
    """`POST /api/v1/config/agents` 的请求体（`doc/api.md` §5.7）。

    新建一个自定义角色条目；`id` 不可与内置角色（collector/analyst/reporter）冲突。
    """

    id: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, max_length=8000)
    enabled: bool = True


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


class StageToolCallResponse(BaseModel):
    """阶段轨迹里的单次工具调用（`doc/api.md` §5.17）。

    `input` / `output` 与 §5.4 的 `tool_calls` 表同形；超长时换成
    `{"truncated": true, "bytes": n, "preview": "…"}`——调用方据此显示「已截断」，
    而不是拿到一段被静默剪掉的内容还以为看到了全部。
    """

    call_id: str
    tool_name: str
    status: str
    input: Any = None
    output: Any = None
    error: str | None = None


class StageTraceItemResponse(BaseModel):
    """单个 Agent 阶段的执行轨迹。

    `reason` 与「有轨迹」互斥：没有轨迹时它说明**为什么没有**（还没轮到 / 正在跑 /
    状态已被清理 / 载荷损坏）。四种原因给的是四种不同的下一步动作，不能合并成一句
    「暂无数据」。
    """

    stage: str
    role: str
    input: str | None = None
    input_from: str | None = None
    output: str | None = None
    tool_calls: list[StageToolCallResponse] = Field(default_factory=list)
    truncated: bool = False
    reason: str | None = None


class WorkflowStageTraceResponse(BaseModel):
    workflow_id: str
    mode: str
    task: str | None = None
    availability: str = "not_integrated"
    reason: str | None = None
    items: list[StageTraceItemResponse] = Field(default_factory=list)


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


class SandboxLimitsResponse(BaseModel):
    """当前生效的沙箱限额，逐项对应 `SandboxSettings`（`doc/api.md` §5.15）。"""

    timeout_seconds: int
    memory_limit: str
    cpu_limit: float
    pids_limit: int
    network_enabled: bool
    output_limit_chars: int
    max_code_chars: int


class SandboxStatusResponse(BaseModel):
    """敏感工具执行边界的只读视图。

    **只读是刻意的**：这些参数是部署期安全边界（cgroup 限额、网络开关、后端选择）。
    做成运行时可改的界面等于让 Web 操作者放宽自己容器的隔离——那不是一个功能，
    是一个缺口。运行期唯一该被看见的信息是「现在到底能不能用、为什么不能用」。
    """

    backend: str
    image: str
    available: bool
    reason: str | None = None
    limits: SandboxLimitsResponse


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


# 工作区错误族（`doc/api.md` §5.19、ADR-033）：顺序敏感——先判子类再判基类。
_WORKSPACE_ERRORS = (
    WorkspaceNotFoundError,
    WorkspaceExistsError,
    WorkspaceDisabled,
    WorkspaceRootUnavailable,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceApprovalRequired,
    WorkspaceError,
)


def _workspace_api_error(exc: Exception) -> ApiError:
    """工作区异常 → 契约错误码（`doc/api.md` §5.19）。"""

    if isinstance(exc, WorkspaceNotFoundError):
        return ApiError("WORKSPACE_NOT_FOUND", str(exc), status.HTTP_404_NOT_FOUND)
    if isinstance(exc, WorkspaceExistsError):
        return ApiError("WORKSPACE_EXISTS", str(exc), status.HTTP_409_CONFLICT)
    if isinstance(exc, WorkspaceDisabled):
        return ApiError("WORKSPACE_DISABLED", str(exc), 503)
    if isinstance(exc, WorkspaceRootUnavailable):
        return ApiError("WORKSPACE_ROOT_UNAVAILABLE", str(exc), 503)
    if isinstance(exc, WorkspacePathError):
        return ApiError("WORKSPACE_PATH_REJECTED", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    if isinstance(exc, WorkspaceQuotaExceeded):
        return ApiError(
            "WORKSPACE_QUOTA_EXCEEDED", str(exc), status.HTTP_409_CONFLICT
        )
    if isinstance(exc, WorkspaceApprovalRequired):
        return ApiError(
            "WORKSPACE_APPROVAL_REQUIRED", str(exc), status.HTTP_409_CONFLICT
        )
    if isinstance(exc, WorkspaceError):
        return ApiError(
            "VALIDATION_ERROR", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    return ApiError("DATA_SOURCE_UNAVAILABLE", "工作区服务不可用，请稍后重试", 503)


def _workspace_call(call: Callable[[], Any]) -> Any:
    """执行一次工作区调用并翻译异常；存储与文件系统故障统一归为 503。"""

    try:
        return call()
    except _WORKSPACE_ERRORS as exc:
        raise _workspace_api_error(exc) from exc
    except IntegrityError as exc:
        # `ux_workspaces_path` 竞态：先查后写没拦住，这里兜底成 409。
        raise ApiError("WORKSPACE_EXISTS", "该路径已登记", status.HTTP_409_CONFLICT) from exc
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:
        raise ApiError(
            "DATA_SOURCE_UNAVAILABLE", "工作区服务不可用，请稍后重试", 503
        ) from exc


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
    "/api/v1/attachments",
    response_model=AttachmentResponse,
    status_code=201,
    summary="上传附件（doc/api.md §5.16，ADR-021）",
)
def upload_attachment(payload: AttachmentUploadRequest) -> AttachmentResponse:
    """登记一个附件：分类 → 解析正文 → 落库，返回元数据。

    **不要求会话已存在**：草稿态下会话还不存在（`doc/api.md` §4.2），
    而用户往往是先选文件再写文字。归属在发消息时才回填（`link_attachments`）。
    """

    raw = payload.data_base64.strip()
    if "," in raw[:80] and raw.lstrip().startswith("data:"):
        # 容忍前端直接给 FileReader 的 data URL。
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(
            "ATTACHMENT_INVALID_BASE64",
            "附件内容不是合法的 base64。",
            status.HTTP_400_BAD_REQUEST,
        ) from exc

    try:
        prepared = prepare_upload(payload.name, data, payload.mime)
    except AttachmentRejected as exc:
        raise ApiError(exc.code, exc.message, status.HTTP_400_BAD_REQUEST) from exc
    return AttachmentResponse.model_validate(api_store.create_attachment(**prepared))


@app.get(
    "/api/v1/attachments/{attachment_id}/content",
    summary="下载附件原件（doc/api.md §5.16，ADR-024）",
    response_class=Response,
)
def download_attachment(attachment_id: str) -> Response:
    """回附件**原始字节**，供气泡里的缩略图、查看原图与下载原件使用。

    原件对**所有类型**都留档（ADR-024）：用户在历史消息里点开附件，期望拿到的是他当初
    传的那份文件，而不是我们抽取出来的纯文本。解析失败的附件（扫描版 PDF）同样有原件——
    读不出正文不代表它不该能下载。

    图片用 `inline`（缩略图与 `<img>` 要能直接渲染），其余用 `attachment`
    （docx/xlsx 在浏览器里没有渲染器，`inline` 只会开出一个空白页）。

    没有字节时（ADR-024 之前落库的文本/文档行）回 404 而不是空响应：空响应会被前端
    当成一份有效内容渲染出来，「没内容」和「内容是空的」在这里是两件事。
    """

    row = api_store.get_attachment_content(attachment_id)
    if row is None or row.get("data") is None:
        raise ApiError(
            "ATTACHMENT_CONTENT_UNAVAILABLE",
            "该附件没有可下载的原件（早于原件留档策略落库的附件不含字节）。",
            status.HTTP_404_NOT_FOUND,
        )
    name = str(row.get("name") or "attachment")
    disposition = "inline" if str(row.get("kind") or "") == "image" else "attachment"
    return Response(
        content=row["data"],
        media_type=row.get("mime") or "application/octet-stream",
        headers={
            # 文件名含中文时用 RFC 5987 形式，避免头部按 latin-1 编码报错。
            "Content-Disposition": f"{disposition}; filename*=UTF-8''" + quote(name, safe="")
        },
    )


@app.delete("/api/v1/attachments/{attachment_id}", status_code=204)
def remove_attachment(attachment_id: str) -> Response:
    """删除尚未发出的附件（用户在输入区点「移除」）。

    已归属消息的附件**不允许**在这里删：那会让历史消息里的附件引用变成空洞，
    历史记录该是只读的。删会话时由外键级联清掉（`AttachmentRecord`）。
    """

    row = api_store.get_attachment(attachment_id)
    if row is None:
        raise ApiError("ATTACHMENT_NOT_FOUND", "附件不存在。", status.HTTP_404_NOT_FOUND)
    if row.get("message_id"):
        raise ApiError(
            "ATTACHMENT_ALREADY_SENT",
            "该附件已随消息发出，不能单独删除。",
            status.HTTP_409_CONFLICT,
        )
    api_store.delete_attachment(attachment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    # 附件归属在消息落库后回填；没挂上的 id 直接回给调用方核对，
    # 不静默吞掉——「传了但没用上」是用户最容易被误导的一类失败（ADR-021）。
    linked: list[str] = []
    unattached: list[str] = []
    if payload.attachment_ids:
        linked = api_store.link_attachments(
            payload.attachment_ids,
            message_id=message["id"],
            session_id=session_id,
        )
        linked_set = set(linked)
        unattached = [
            value for value in payload.attachment_ids if value not in linked_set
        ]
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
        # 用户消息进会话记忆，供同一会话的后续轮次做上下文继承（F-06 / ADR-019）。
        # Redis 不可用时由实现层降级为无操作，不影响消息受理。
        conversation_memory().append_message(
            session_id,
            SessionMessage(
                session_id=session_id,
                role=MessageRole.USER,
                content=payload.content,
                id=message["id"],
                agent_run_id=agent_run["id"],
                status=MessageStatus.RUNNING,
            ),
        )
        get_workflow_service().schedule(
            WorkflowTask(
                workflow_id=workflow_id,
                task=payload.content,
                session_id=session_id,
                agent_run_id=agent_run["id"],
                message_id=message["id"],
                orchestration_mode=payload.orchestration_mode,
                # 只传 id：附件内容可能是一张 5 MB 的图片，塞进工作流输入会顶爆
                # Dapr 活动载荷上限；执行阶段按 id 取正文（ADR-021）。
                attachment_ids=list(linked),
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
        attachments=[AttachmentResponse.model_validate(item) for item in api_store.list_attachments_for_messages([message["id"]]).get(message["id"], [])],
        unattached_attachment_ids=unattached,
    )


@app.get("/api/v1/sessions/{session_id}/messages", response_model=MessageListResponse)
def list_session_messages(
    session_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> MessageListResponse:
    _session_or_404(session_id)
    items, total = api_store.list_messages(session_id, page=page, page_size=page_size)
    grouped = api_store.list_attachments_for_messages([item["id"] for item in items])
    return MessageListResponse(
        items=[
            MessageResponse.model_validate(
                {**item, "attachments": grouped.get(item["id"], [])}
            )
            for item in items
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@app.get("/api/v1/sessions/{session_id}/workflows", response_model=WorkflowListResponse)
def list_session_workflows(session_id: str) -> WorkflowListResponse:
    """只读：这个会话里每一次对话各自跑出的协作工作流（`doc/api.md` §5.18）。

    全屏协作画布用它把「对话 1 / 2 / 3」列出来并支持回看：编号就是列表下标 + 1，
    所以顺序必须是**创建时间升序**（由存储层保证，见 `list_workflows_for_session`）。
    """

    _session_or_404(session_id)
    rows = api_store.list_workflows(session_id)
    return WorkflowListResponse(
        items=[_workflow_response(row) for row in rows],
        total=len(rows),
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
    """角色的生效配置 + 「哪些字段被显式覆盖」。

    角色元数据（name/role/description/builtin/enabled）优先从角色目录读，
    目录缺失该条目时回退到内置 `_AGENT_NAMES`（兼容未跑种子迁移的旧环境）。
    """

    # 覆盖表读一次喂给解析与 override_keys，避免同一请求读两遍存储。
    overrides = agent_config_reader()
    registry = _agent_registry_map()
    entry = registry.get(agent_id)

    if entry is None and agent_id not in _AGENT_NAMES:
        raise ApiError("AGENT_NOT_FOUND", "Agent 不存在", status.HTTP_404_NOT_FOUND)

    settings = resolve_agent_settings(agent_id, get_settings(), overrides=overrides)
    return AgentResponse(
        id=agent_id,
        name=(entry["name"] if entry else _AGENT_NAMES[agent_id]),
        role=(entry["role"] if entry else agent_id),
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
        builtin=bool(entry["builtin"]) if entry else True,
        description=entry["description"] if entry else None,
        enabled=bool(entry["enabled"]) if entry else True,
    )


def _agent_registry_map() -> dict[str, dict[str, Any]]:
    """角色目录的 id → 条目映射；读取失败返回空表（回退 `_AGENT_NAMES`）。"""

    try:
        return {row["id"]: row for row in list_agent_registry()}
    except Exception:  # pragma: no cover - 存储不可用时回退
        return {}


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

    角色列表来自 `agent_registry`（内置 + 自定义）；目录为空时回退到内置三角色。
    """

    registry = _agent_registry_map()
    agent_ids = list(registry) if registry else list(_AGENT_NAMES)
    return AgentConfigListResponse(
        items=[_agent_response(agent_id) for agent_id in agent_ids],
        available_models=[
            AvailableModelResponse(**item) for item in _available_models()
        ],
    )


@app.post(
    "/api/v1/config/agents",
    response_model=AgentResponse,
    status_code=201,
)
def create_agent_registry_entry(
    payload: AgentRegistryCreateRequest,
    request: Request,
) -> AgentResponse:
    """登记一个自定义角色（`doc/api.md` §5.7）。

    内置角色 id（collector/analyst/reporter）不可重复登记。
    """

    if payload.id in _AGENT_NAMES:
        raise ApiError("VALIDATION_ERROR", "该 id 属于内置流水线角色，不能重复登记", status.HTTP_409_CONFLICT)
    try:
        create_agent_registry(
            agent_id=payload.id,
            name=payload.name,
            role=payload.role,
            description=payload.description,
            system_prompt=payload.system_prompt,
            enabled=payload.enabled,
        )
    except SQLAlchemyError as exc:
        raise ApiError("VALIDATION_ERROR", "角色 id 已存在", status.HTTP_409_CONFLICT) from exc
    return _agent_response(payload.id)


@app.delete("/api/v1/config/agents/{agent_id}", status_code=204)
def delete_agent_registry_entry(agent_id: str) -> Response:
    """删除自定义角色（`doc/api.md` §5.7）。

    内置角色返回 `409 AGENT_BUILTIN`；不存在的角色返回 `404`。
    """

    if agent_id in _AGENT_NAMES:
        raise ApiError("AGENT_BUILTIN", "内置流水线角色不可删除", status.HTTP_409_CONFLICT)
    deleted = delete_agent_registry(agent_id)
    if not deleted:
        raise ApiError("AGENT_NOT_FOUND", "Agent 不存在", status.HTTP_404_NOT_FOUND)
    return Response(status_code=204)


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


def _sandbox_availability(settings) -> tuple[bool, str | None]:
    """探测沙箱后端是否真的可用，并给出**具体**原因。

    原因由后端自己给出（`Sandbox.unavailable_reason()`），不在这里猜：套接字没挂、
    镜像不在宿主机、Docker SDK 没装上，是三件需要三种不同处理的事，界面上要分得开。
    """

    try:
        detail = build_sandbox(settings).unavailable_reason()
    except Exception as exc:  # 构建后端自身失败（例如没装 docker 包）
        return False, f"探测沙箱时出错：{type(exc).__name__}: {exc}"
    return (True, None) if detail is None else (False, detail)


@app.get("/api/v1/config/sandbox", response_model=SandboxStatusResponse)
def get_sandbox_status() -> SandboxStatusResponse:
    """只读：敏感工具执行边界（`doc/api.md` §5.15）。

    没有对应的写接口——沙箱限额属于部署期安全边界，刻意不做运行时可改。
    """

    settings = get_sandbox_settings()
    available, reason = _sandbox_availability(settings)
    log_event(
        logger,
        "sandbox.status",
        backend=settings.backend,
        available=available,
        reason=reason,
    )
    return SandboxStatusResponse(
        backend=settings.backend,
        image=settings.image,
        available=available,
        reason=reason,
        limits=SandboxLimitsResponse(
            timeout_seconds=settings.timeout_seconds,
            memory_limit=settings.memory_limit,
            cpu_limit=settings.cpu_limit,
            pids_limit=settings.pids_limit,
            network_enabled=settings.network_enabled,
            output_limit_chars=settings.output_limit_chars,
            max_code_chars=settings.max_code_chars,
        ),
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


STATE_STORE_ERRORS: tuple[type[BaseException], ...] = (
    DaprGrpcError,
    OSError,
    ConnectionError,
    TimeoutError,
    ValueError,
    KeyError,
)
"""读状态存储会遇到的失败类型（`doc/api.md` §5.17）。

`DaprGrpcError` 单独列出来是因为它**不在** `OSError` 家族里（它继承 `grpc.RpcError`）：
漏掉它，「sidecar 没起来」就会表现成 500 而不是 `DATA_SOURCE_UNAVAILABLE` 503。
"""


@app.get(
    "/api/v1/workflows/{workflow_id}/stages",
    response_model=WorkflowStageTraceResponse,
)
def list_workflow_stage_traces(workflow_id: str) -> WorkflowStageTraceResponse:
    """只读：本次执行里各个 Agent 实际做了什么（`doc/api.md` §5.17）。

    这是执行台卡片弹窗的数据源。三个语义要分清：

    - **404**：Workflow 不存在；
    - **200 + `reason`**：这个阶段确实没有轨迹（还没轮到 / 正在跑 / 状态已被清理），
      原因逐条写明，前端直接照着显示；
    - **503**：状态存储（Dapr sidecar）读不到。**不**把它伪装成「这个阶段没有轨迹」——
      前者是环境没起来，后者是任务还没跑到，两者的下一步动作完全不同。
    """

    workflow = get_workflow(workflow_id)
    try:
        data = read_stage_traces(workflow_id, checkpoint=workflow.checkpoint)
    except STATE_STORE_ERRORS as exc:
        log_event(
            logger,
            "workflow.stage_trace_unavailable",
            level=logging.WARNING,
            workflow_id=workflow_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise ApiError(
            "DATA_SOURCE_UNAVAILABLE",
            "阶段执行轨迹来自 Dapr 状态存储，当前读不到（请确认后端与 dapr-sidecar 都在运行）",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return WorkflowStageTraceResponse.model_validate(data)


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


# model_id 由 `{provider_id}:{model}` 派生（§5.10），model 名常含 `/`（如 BAAI/bge-m3）。
# :path 转换器允许 id 里的斜杠命中路由——客户端把 id 编码成 %2F 后，ASGI 解码成 `/`，
# 普通 {model_id} 段匹配会直接 404（这就是「模型开关关不掉」的根因）。
@app.patch("/api/v1/config/models/{model_id:path}", response_model=ModelRegistryResponse)
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


@app.delete("/api/v1/config/models/{model_id:path}", status_code=204)
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


# --------------------------------------------------------------------------- #
# §5.19 工作区（ADR-033）
#
# 阶段 2：`workspace_write` 档位可用，放开**非破坏性**写操作（新建 / 写入 / 建目录 /
# 移动）。覆盖与删除按 ADR-033 §6 要人工审批，与审批链路一起在阶段 3，因此现在既没有
# `delete_work_entry`，`write_work_file(overwrite=true)` 也会被拒（409
# `WORKSPACE_APPROVAL_REQUIRED`）。**Agent 没有提权通道**：档位只能由人走 PATCH 调整。
# --------------------------------------------------------------------------- #


class WorkspaceQuota(BaseModel):
    max_file_bytes: int
    max_total_bytes: int
    max_entries: int


class WorkspaceUsage(BaseModel):
    """用量视图；根被挪走或目录被删时 `available=false` 并给出原因，而不是整体 500。"""

    available: bool = True
    total_bytes: int = 0
    entries: int = 0
    truncated: bool = False
    scan_limit: int | None = None
    reason: str | None = None


class WorkspaceResponse(BaseModel):
    id: str
    session_id: str | None = None
    path: str = ""
    mode: Literal["read_only", "workspace_write"]
    name: str | None = None
    quota: WorkspaceQuota
    usage: WorkspaceUsage | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    total: int


class WorkspaceEntry(BaseModel):
    name: str
    path: str
    kind: Literal["file", "dir", "symlink", "other"]
    outside: bool = False
    size_bytes: int | None = None
    modified_at: str | None = None
    children: list[WorkspaceEntry] | None = None


WorkspaceEntry.model_rebuild()


class WorkspaceTreeResponse(BaseModel):
    workspace_id: str
    path: str = ""
    depth: int
    entries: list[WorkspaceEntry]
    truncated: bool
    limit: int


class WorkspaceCreateRequest(BaseModel):
    """`POST /api/v1/workspaces` 的请求体（`doc/api.md` §5.19）。

    `path` 是**相对工作区根**的路径；留空时默认绑到 `sessions/<session_id>/`。
    """

    session_id: str | None = None
    path: str | None = Field(default=None, max_length=500)
    mode: Literal["read_only", "workspace_write"] = "read_only"
    name: str | None = Field(default=None, max_length=100)


class WorkspacePatchRequest(BaseModel):
    """`PATCH /api/v1/workspaces/{workspace_id}`（`doc/api.md` §5.19）。

    这是**人的动作**（ADR-033 §3）：Agent 没有提权通道，提档只能从这里或 Web UI 发起。
    """

    mode: Literal["read_only", "workspace_write"] | None = None
    name: str | None = Field(default=None, max_length=100)


@app.get("/api/v1/workspaces", response_model=WorkspaceListResponse)
def list_workspace_registry(
    session_id: str | None = Query(default=None),
) -> WorkspaceListResponse:
    """列出工作区（`doc/api.md` §5.19）；给 `session_id` 时只列该会话绑定的。

    会话存在性先校验：非法或未知的会话 id 返回 404 `SESSION_NOT_FOUND`，
    而不是把存储层的 UUID 解析错误冒成 500。
    """

    if session_id:
        _session_or_404(session_id)
    data = _workspace_call(
        lambda: workspace_service.list_workspaces(session_id=session_id)
    )
    return WorkspaceListResponse.model_validate(data)


@app.post("/api/v1/workspaces", response_model=WorkspaceResponse, status_code=201)
def create_workspace_registry(
    payload: WorkspaceCreateRequest,
    request: Request,
) -> WorkspaceResponse:
    """登记一个工作区（`doc/api.md` §5.19）。

    会话必须已存在（否则 404 `SESSION_NOT_FOUND`）；目录不存在时由平台创建。
    路径越界、符号链接逃逸返回 422 `WORKSPACE_PATH_REJECTED`；
    `mode=workspace_write` 当前返回 422 `WORKSPACE_MODE_UNAVAILABLE`。
    """

    if payload.session_id:
        _session_or_404(payload.session_id)
    data = _workspace_call(
        lambda: workspace_service.create_workspace(
            session_id=payload.session_id,
            path=payload.path,
            mode=payload.mode,
            name=payload.name,
            actor=request.headers.get("X-Request-ID"),
        )
    )
    return WorkspaceResponse.model_validate(data)


@app.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def read_workspace_registry(workspace_id: str) -> WorkspaceResponse:
    """读取一个工作区（含配额与当前用量）。"""

    data = _workspace_call(lambda: workspace_service.get_workspace(workspace_id))
    return WorkspaceResponse.model_validate(data)


@app.get("/api/v1/workspaces/{workspace_id}/tree", response_model=WorkspaceTreeResponse)
def read_workspace_tree(
    workspace_id: str,
    path: str = Query(default="", max_length=500),
    depth: int = Query(default=1, ge=1, le=8),
) -> WorkspaceTreeResponse:
    """列出工作区内的目录树；`path` 相对工作区，符号链接越界只标记不跟随。"""

    data = _workspace_call(
        lambda: workspace_service.workspace_tree(workspace_id, path=path, depth=depth)
    )
    return WorkspaceTreeResponse.model_validate(data)


@app.patch("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def patch_workspace_registry(
    workspace_id: str,
    payload: WorkspacePatchRequest,
    request: Request,
) -> WorkspaceResponse:
    """调整档位或名称（`doc/api.md` §5.19）。

    提档（`read_only` → `workspace_write`）会让该会话的 Agent 多出三个写工具，
    因此这是一次**显式的人工授权**：记 `workspace.updated` 日志并带操作者。
    """

    data = _workspace_call(
        lambda: workspace_service.update_workspace(
            workspace_id,
            mode=payload.mode,
            name=payload.name,
            actor=request.headers.get("X-Request-ID"),
        )
    )
    return WorkspaceResponse.model_validate(data)


@app.delete("/api/v1/workspaces/{workspace_id}", status_code=204)
def delete_workspace_registry(workspace_id: str, request: Request) -> Response:
    """解除登记（`doc/api.md` §5.19）。**不删宿主文件**。"""

    _workspace_call(
        lambda: workspace_service.delete_workspace(
            workspace_id, actor=request.headers.get("X-Request-ID")
        )
    )
    return Response(status_code=204)

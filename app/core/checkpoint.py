import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    desc,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.storage import get_session_factory

ALLOWED_WORKFLOW_RUN_STATUSES = frozenset(
    {"pending", "running", "paused", "completed", "failed", "cancelled"}
)

ALLOWED_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_sessions_user_id", "user_id"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_agent_runs_session", "session_id", "created_at"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_messages_session_created", "session_id", "created_at"),)


class AttachmentRecord(Base):
    """消息附件（`doc/data-model.md` §3.2，ADR-021）。

    为什么放数据库而不是文件系统：附件要么与消息同生共死（删会话就该一起没），
    要么就得引入一套孤儿清理与卷挂载；前者用 `ON DELETE CASCADE` 一行解决，
    后者要动 `deploy/`。代价是库体积，因此上传侧对单文件大小与单消息数量都有硬上限
    （`app/attachments/spec.py`）。

    `session_id` 可空是**故意的**：草稿态下会话还不存在（`doc/api.md` §4.2），
    附件必须先能上传；等首条消息落库时再回填 `session_id` / `message_id`。
    """

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mime: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # 原始字节，**所有类型都留档**（ADR-024）：图片要转 base64 进模型请求；文本与文档
    # 除了提示词里用的正文（`text_content`）之外，原件本身也要能下载回来。
    # 代价是库体积，由 `app/attachments/spec.py` 的单文件 / 单消息硬上限约束。
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_attachments_message", "message_id"),
        Index("idx_attachments_session", "session_id"),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    instance_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_workflow_runs_agent_run_id"),
        Index("idx_workflow_runs_session", "session_id"),
        Index("idx_workflow_runs_instance", "instance_id"),
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="running", nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_tool_calls_run", "run_id", "created_at"),)


class MetricRecord(Base):
    """`metrics` 表：观测采样落库（`doc/data-model.md` §3）。

    写入方是 `app/observability/metrics.py::PostgresMetricSink`（按列名反射插入），
    读取方是只读接口 `GET /api/v1/metrics`；建表只走 `init_checkpoint_schema()`，
    采样与读取都不建表。
    """

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_metrics_name_time", "metric_name", desc("recorded_at")),
    )


UNSET = object()
"""哨兵：区分「调用方没有传该字段」与「显式传 null 清除覆盖」（`doc/api.md` §5.7、§5.8）。"""

PROVIDER_CONFIG_ID = "default"
"""`provider_configs` 单行表的主键固定值（`doc/data-model.md` §3）。"""


class AgentConfigRecord(Base):
    """`agent_configs` 表：Agent 模型的覆盖配置（`doc/data-model.md` §3）。

    只存**被覆盖的字段**：列值为 NULL 表示回退下一层配置（ADR-013、ADR-017 §2）。
    写入方是 `PATCH /api/v1/config/agents/{agent_id}`（`doc/api.md` §5.7），
    读取方是 `app/core/agent_config.py::resolve_agent_settings`（API 与阶段活动共用）。

    `llm_model_id` 是**逻辑**引用（不建外键，ADR-017）：模型条目被删除时退化为
    「未绑定」并按 `model` 列回退，不让一次删除把配置读取打挂。
    """

    __tablename__ = "agent_configs"

    agent_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_model_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class AgentRegistryRecord(Base):
    """`agent_registry` 表：Agent 角色目录（ADR-017 的 `chatModels` 同构扩展）。

    把角色从「代码写死的 `_AGENT_NAMES`」提升为可增删启停的注册表条目，
    供 Agent 配置页按 obsidian-yolo 的方式管理。三个内置流水线角色
    （collector / analyst / reporter）作为 `builtin=True` 的种子数据，
    不可删除（删除会破坏固定三步流水线拓扑）；自定义角色 `builtin=False`，
    可自由增删。

    `role` 是流水线语义键（collector 等），自定义角色为自由文本键，
    仅作标识，不接入固定流水线（意图路由属后续架构演进）。
    """

    __tablename__ = "agent_registry"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ProviderConfigRecord(Base):
    """`provider_configs` 表：默认模型路由 + 直连覆盖（`doc/data-model.md` §3）。

    单行表（`id='default'`），只存**被覆盖的字段**：列值为 NULL 表示回退 API 进程的
    环境配置。写入方是 `PUT /api/v1/config/provider`（`doc/api.md` §5.8），
    读取方是 `app/core/provider_config.py`（API 与阶段活动共用）。

    `default_llm_model_id`（ADR-017）指向 `llm_models.id`，优先级高于
    `provider` / `model` / `base_url` / `api_key` / `temperature` 五个直连列；
    它同样是逻辑引用，悬空时按未设置处理。

    `api_key` 以明文存储，属于运行期凭据：不回传、不落日志，访问边界由数据库权限与
    部署网络保证（ADR-014、ADR-015）。
    """

    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_llm_model_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class LlmProviderRecord(Base):
    """`llm_providers` 表：模型 Provider 注册表（`doc/data-model.md` §3、ADR-017）。

    多行表，一行一个端点。`preset_type` 是配置层的预设族，`api_type` 是运行期的协议族；
    二者正交，运行期只按 `api_type` 选择实现。`api_key` 只写不回读。
    """

    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    preset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    api_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    additional_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (Index("idx_llm_providers_enabled", "enabled"),)


class LlmModelRecord(Base):
    """`llm_models` 表：模型注册表与特化调参（`doc/data-model.md` §3、ADR-017）。

    `(provider_id, model)` 唯一，批量引入据此幂等（`ON CONFLICT DO NOTHING`）。
    `custom_parameters` 与 `modalities` 为 JSONB 数组载荷。
    """

    __tablename__ = "llm_models"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("llm_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reasoning_type: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_parameters: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    modalities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("provider_id", "model", name="uq_llm_models_provider_model"),
        Index("idx_llm_models_provider", "provider_id"),
    )


class McpServerRecord(Base):
    """`mcp_server_registry` 表：MCP Server 注册表（`doc/data-model.md` §3、ADR-017）。

    传输方式由条目自身的 `transport` 决定；`discovered` 缓存最近一次发现结果，
    使工具目录成为**配置的函数**而不是连接状态的函数（离线 Server 的工具仍在目录里）。
    """

    __tablename__ = "mcp_server_registry"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    args: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    env: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    cwd: Mapped[str | None] = mapped_column(String(500), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    headers: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tool_options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    discovered: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class WorkspaceRecord(Base):
    """`workspaces` 表：工作区登记（`doc/data-model.md` §3.3、ADR-033）。

    `path` **只存相对工作区根的路径**：宿主绝对路径由部署层的 `WORKSPACE_HOST_ROOT`
    决定，不进库——否则改一次部署根就会让所有历史行失效。
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="read_only", nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quota: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_workspaces_session", "session_id"),
        UniqueConstraint("path", name="ux_workspaces_path"),
    )


def _provider_config_to_dict(row: ProviderConfigRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "api_key": row.api_key,
        "temperature": row.temperature,
        "default_llm_model_id": row.default_llm_model_id,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def get_provider_config() -> dict[str, Any] | None:
    """返回默认路由覆盖行；没有写入过返回 None。"""

    with get_session_factory()() as session:
        row = session.get(ProviderConfigRecord, PROVIDER_CONFIG_ID)
        return _provider_config_to_dict(row) if row else None


def upsert_provider_config(
    *,
    provider: Any = UNSET,
    model: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    temperature: Any = UNSET,
    default_llm_model_id: Any = UNSET,
    updated_by: str | None = None,
) -> dict[str, Any]:
    """写入默认路由覆盖值；未传的字段保持原值，显式传 None 表示清除该字段。"""

    with get_session_factory()() as session:
        row = session.get(ProviderConfigRecord, PROVIDER_CONFIG_ID)
        if row is None:
            row = ProviderConfigRecord(id=PROVIDER_CONFIG_ID)
            session.add(row)
        values = {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "temperature": temperature,
            "default_llm_model_id": default_llm_model_id,
        }
        for field, value in values.items():
            if value is not UNSET:
                setattr(row, field, value)
        if updated_by is not None:
            row.updated_by = updated_by
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _provider_config_to_dict(row)


def _agent_config_to_dict(row: AgentConfigRecord) -> dict[str, Any]:
    return {
        "agent_id": row.agent_id,
        "model": row.model,
        "temperature": row.temperature,
        "llm_model_id": row.llm_model_id,
        "top_p": row.top_p,
        "max_output_tokens": row.max_output_tokens,
        "reasoning_type": row.reasoning_type,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def get_agent_config(agent_id: str) -> dict[str, Any] | None:
    """按角色返回覆盖配置；没有覆盖行返回 None。"""

    with get_session_factory()() as session:
        row = session.get(AgentConfigRecord, agent_id)
        return _agent_config_to_dict(row) if row else None


def list_agent_configs() -> list[dict[str, Any]]:
    """返回全部覆盖行（角色数固定，不分页）。"""

    with get_session_factory()() as session:
        rows = session.scalars(select(AgentConfigRecord)).all()
        return [_agent_config_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# agent_registry：Agent 角色目录（可增删启停的注册表，ADR-017 同构）
# ---------------------------------------------------------------------------

# 三个内置流水线角色作为种子数据（对齐 app/agents/roles.py 的展示名与角色键）。
BUILTIN_AGENT_SEED: tuple[dict[str, Any], ...] = (
    {
        "id": "collector",
        "name": "信息收集 Agent",
        "role": "collector",
        "description": "收集、检索并整理任务主题相关的事实与要点，输出结构化信息清单。",
    },
    {
        "id": "analyst",
        "name": "数据分析 Agent",
        "role": "analyst",
        "description": "基于信息清单进行归纳、对比与提炼，识别关键结论、趋势与风险。",
    },
    {
        "id": "reporter",
        "name": "报告生成 Agent",
        "role": "reporter",
        "description": "整合分析摘要，生成结构清晰、可读的正式报告。",
    },
)


def _agent_registry_to_dict(row: AgentRegistryRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "role": row.role,
        "description": row.description,
        "system_prompt": row.system_prompt,
        "builtin": row.builtin,
        "enabled": row.enabled,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_agent_registry() -> list[dict[str, Any]]:
    """返回全部角色目录条目（内置在前，自定义在后，按 id 排序）。"""

    with get_session_factory()() as session:
        rows = session.scalars(
            select(AgentRegistryRecord).order_by(
                AgentRegistryRecord.builtin.desc(), AgentRegistryRecord.id
            )
        ).all()
        return [_agent_registry_to_dict(row) for row in rows]


def get_agent_registry(agent_id: str) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(AgentRegistryRecord, agent_id)
        return _agent_registry_to_dict(row) if row else None


def create_agent_registry(
    *,
    agent_id: str,
    name: str,
    role: str,
    description: str | None = None,
    system_prompt: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """插入自定义角色；id 重复抛 `IntegrityError`（上层转 409）。"""

    row = AgentRegistryRecord(
        id=agent_id,
        name=name,
        role=role,
        description=description,
        system_prompt=system_prompt,
        builtin=False,
        enabled=enabled,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _agent_registry_to_dict(row)


def delete_agent_registry(agent_id: str) -> bool:
    """删除自定义角色；内置角色返回 False（由上层区分 409/404）。

    同时清理该角色的覆盖行（`agent_configs` 逻辑关联，无外键）。
    """

    with get_session_factory()() as session:
        row = session.get(AgentRegistryRecord, agent_id)
        if row is None:
            return False
        if row.builtin:
            return False
        session.delete(row)
        # 覆盖行随角色一起清掉，不留孤儿。
        session.query(AgentConfigRecord).filter(
            AgentConfigRecord.agent_id == agent_id
        ).delete(synchronize_session=False)
        session.commit()
        return True


def seed_builtin_agents() -> int:
    """把三个内置角色写入目录（幂等：已存在则跳过），返回新增条数。"""

    with get_session_factory()() as session:
        created = 0
        for seed in BUILTIN_AGENT_SEED:
            if session.get(AgentRegistryRecord, seed["id"]) is not None:
                continue
            session.add(
                AgentRegistryRecord(
                    id=seed["id"],
                    name=seed["name"],
                    role=seed["role"],
                    description=seed["description"],
                    system_prompt=None,
                    builtin=True,
                    enabled=True,
                )
            )
            created += 1
        session.commit()
        return created


def upsert_agent_config(
    agent_id: str,
    *,
    model: Any = UNSET,
    temperature: Any = UNSET,
    llm_model_id: Any = UNSET,
    top_p: Any = UNSET,
    max_output_tokens: Any = UNSET,
    reasoning_type: Any = UNSET,
    updated_by: str | None = None,
) -> dict[str, Any]:
    """写入覆盖值；未传的字段保持原值，显式传 None 表示清除该字段的覆盖。"""

    with get_session_factory()() as session:
        row = session.get(AgentConfigRecord, agent_id)
        if row is None:
            row = AgentConfigRecord(agent_id=agent_id)
            session.add(row)
        values = {
            "model": model,
            "temperature": temperature,
            "llm_model_id": llm_model_id,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "reasoning_type": reasoning_type,
        }
        for field, value in values.items():
            if value is not UNSET:
                setattr(row, field, value)
        if updated_by is not None:
            row.updated_by = updated_by
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _agent_config_to_dict(row)


def _llm_provider_to_dict(row: LlmProviderRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "preset_type": row.preset_type,
        "api_type": row.api_type,
        "base_url": row.base_url,
        "api_key": row.api_key,
        "custom_headers": row.custom_headers or {},
        "additional_settings": row.additional_settings or {},
        "enabled": row.enabled,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    }


def list_llm_providers() -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        rows = session.scalars(
            select(LlmProviderRecord).order_by(LlmProviderRecord.id)
        ).all()
        return [_llm_provider_to_dict(row) for row in rows]


def get_llm_provider(provider_id: str) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(LlmProviderRecord, provider_id)
        return _llm_provider_to_dict(row) if row else None


def create_llm_provider(
    *,
    provider_id: str,
    name: str,
    preset_type: str,
    api_type: str,
    base_url: str | None = None,
    api_key: str | None = None,
    custom_headers: dict[str, str] | None = None,
    additional_settings: dict[str, Any] | None = None,
    enabled: bool = True,
    updated_by: str | None = None,
) -> dict[str, Any]:
    """插入 Provider；id 重复时抛 `sqlalchemy.exc.IntegrityError`（由上层转为 409）。"""

    row = LlmProviderRecord(
        id=provider_id,
        name=name,
        preset_type=preset_type,
        api_type=api_type,
        base_url=base_url,
        api_key=api_key,
        custom_headers=custom_headers or {},
        additional_settings=additional_settings or {},
        enabled=enabled,
        updated_by=updated_by,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _llm_provider_to_dict(row)


def update_llm_provider(
    provider_id: str,
    *,
    name: Any = UNSET,
    preset_type: Any = UNSET,
    api_type: Any = UNSET,
    base_url: Any = UNSET,
    api_key: Any = UNSET,
    custom_headers: Any = UNSET,
    additional_settings: Any = UNSET,
    enabled: Any = UNSET,
    updated_by: str | None = None,
) -> dict[str, Any] | None:
    """部分更新；条目不存在返回 None。"""

    with get_session_factory()() as session:
        row = session.get(LlmProviderRecord, provider_id)
        if row is None:
            return None
        values = {
            "name": name,
            "preset_type": preset_type,
            "api_type": api_type,
            "base_url": base_url,
            "api_key": api_key,
            "custom_headers": custom_headers,
            "additional_settings": additional_settings,
            "enabled": enabled,
        }
        for field, value in values.items():
            if value is not UNSET:
                setattr(row, field, value)
        if updated_by is not None:
            row.updated_by = updated_by
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _llm_provider_to_dict(row)


def delete_llm_provider(provider_id: str) -> bool:
    """删除 Provider 及其模型条目（模型随外键 CASCADE 一并删除）。"""

    with get_session_factory()() as session:
        row = session.get(LlmProviderRecord, provider_id)
        if row is None:
            return False
        # 显式清理子行：外键 CASCADE 只在真实 PostgreSQL 上生效，
        # SQLite 等替身（测试用）默认不启用 PRAGMA foreign_keys。
        session.query(LlmModelRecord).filter(
            LlmModelRecord.provider_id == provider_id
        ).delete(synchronize_session=False)
        session.delete(row)
        session.commit()
        return True


def count_enabled_models(provider_id: str) -> int:
    """该 Provider 下启用中的模型数，用于 `PROVIDER_IN_USE` 判断。"""

    from sqlalchemy import func

    with get_session_factory()() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(LlmModelRecord)
                .where(
                    LlmModelRecord.provider_id == provider_id,
                    LlmModelRecord.enabled.is_(True),
                )
            )
            or 0
        )


def _llm_model_to_dict(row: LlmModelRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model": row.model,
        "name": row.name,
        "enabled": row.enabled,
        "reasoning_type": row.reasoning_type,
        "temperature": row.temperature,
        "top_p": row.top_p,
        "max_context_tokens": row.max_context_tokens,
        "max_output_tokens": row.max_output_tokens,
        "custom_parameters": row.custom_parameters or [],
        "modalities": row.modalities or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    }


def list_llm_models(
    *,
    provider_id: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    statement = select(LlmModelRecord).order_by(
        LlmModelRecord.provider_id, LlmModelRecord.model
    )
    if provider_id is not None:
        statement = statement.where(LlmModelRecord.provider_id == provider_id)
    if enabled is not None:
        statement = statement.where(LlmModelRecord.enabled.is_(enabled))
    with get_session_factory()() as session:
        rows = session.scalars(statement).all()
        return [_llm_model_to_dict(row) for row in rows]


def get_llm_model(model_id: str) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(LlmModelRecord, model_id)
        return _llm_model_to_dict(row) if row else None


def find_llm_model_by_provider_and_name(
    provider_id: str, model: str
) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.scalars(
            select(LlmModelRecord).where(
                LlmModelRecord.provider_id == provider_id,
                LlmModelRecord.model == model,
            )
        ).first()
        return _llm_model_to_dict(row) if row else None


def create_llm_model(
    *,
    model_id: str,
    provider_id: str,
    model: str,
    name: str | None = None,
    enabled: bool = True,
    reasoning_type: str = "none",
    temperature: float | None = None,
    top_p: float | None = None,
    max_context_tokens: int | None = None,
    max_output_tokens: int | None = None,
    custom_parameters: list[dict[str, Any]] | None = None,
    modalities: list[str] | None = None,
    updated_by: str | None = None,
) -> dict[str, Any]:
    row = LlmModelRecord(
        id=model_id,
        provider_id=provider_id,
        model=model,
        name=name,
        enabled=enabled,
        reasoning_type=reasoning_type,
        temperature=temperature,
        top_p=top_p,
        max_context_tokens=max_context_tokens,
        max_output_tokens=max_output_tokens,
        custom_parameters=custom_parameters or [],
        modalities=modalities or [],
        updated_by=updated_by,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _llm_model_to_dict(row)


def create_llm_models_bulk(rows: list[dict[str, Any]]) -> list[str]:
    """批量插入模型条目，返回**实际新建**的 id 列表。

    用 `ON CONFLICT (provider_id, model) DO NOTHING` 让重复提交幂等（ADR-017 §4）；
    调用方先自行过滤已存在项，这里的冲突兜底并发写入。
    """

    if not rows:
        return []
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    created: list[str] = []
    with get_session_factory()() as session:
        for row in rows:
            statement = (
                pg_insert(LlmModelRecord)
                .values(**row)
                .on_conflict_do_nothing(
                    index_elements=["provider_id", "model"]
                )
                .returning(LlmModelRecord.id)
            )
            if session.execute(statement).scalar() is not None:
                created.append(row["id"])
        session.commit()
    return created


def update_llm_model(model_id: str, **fields: Any) -> dict[str, Any] | None:
    """部分更新；条目不存在返回 None。`UNSET` 字段保持原值。"""

    editable = (
        "model",
        "name",
        "enabled",
        "reasoning_type",
        "temperature",
        "top_p",
        "max_context_tokens",
        "max_output_tokens",
        "custom_parameters",
        "modalities",
        "updated_by",
    )
    with get_session_factory()() as session:
        row = session.get(LlmModelRecord, model_id)
        if row is None:
            return None
        for field in editable:
            value = fields.get(field, UNSET)
            if value is not UNSET:
                setattr(row, field, value)
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _llm_model_to_dict(row)


def delete_llm_model(model_id: str) -> bool:
    with get_session_factory()() as session:
        row = session.get(LlmModelRecord, model_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def _mcp_server_to_dict(row: McpServerRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "command": row.command,
        "args": row.args or [],
        "env": row.env or {},
        "cwd": row.cwd,
        "url": row.url,
        "headers": row.headers or {},
        "enabled": row.enabled,
        "tool_options": row.tool_options or {},
        "discovered": row.discovered,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    }


def list_mcp_servers(*, enabled: bool | None = None) -> list[dict[str, Any]]:
    statement = select(McpServerRecord).order_by(McpServerRecord.id)
    if enabled is not None:
        statement = statement.where(McpServerRecord.enabled.is_(enabled))
    with get_session_factory()() as session:
        rows = session.scalars(statement).all()
        return [_mcp_server_to_dict(row) for row in rows]


def get_mcp_server(server_id: str) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(McpServerRecord, server_id)
        return _mcp_server_to_dict(row) if row else None


def create_mcp_server(
    *,
    server_id: str,
    name: str,
    transport: str,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    enabled: bool = True,
    tool_options: dict[str, Any] | None = None,
    updated_by: str | None = None,
) -> dict[str, Any]:
    row = McpServerRecord(
        id=server_id,
        name=name,
        transport=transport,
        command=command,
        args=args or [],
        env=env or {},
        cwd=cwd,
        url=url,
        headers=headers or {},
        enabled=enabled,
        tool_options=tool_options or {},
        updated_by=updated_by,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _mcp_server_to_dict(row)


def update_mcp_server(server_id: str, **fields: Any) -> dict[str, Any] | None:
    editable = (
        "name",
        "transport",
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "headers",
        "enabled",
        "tool_options",
        "discovered",
        "updated_by",
    )
    with get_session_factory()() as session:
        row = session.get(McpServerRecord, server_id)
        if row is None:
            return None
        for field in editable:
            value = fields.get(field, UNSET)
            if value is not UNSET:
                setattr(row, field, value)
        row.updated_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _mcp_server_to_dict(row)


def delete_mcp_server(server_id: str) -> bool:
    with get_session_factory()() as session:
        row = session.get(McpServerRecord, server_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def _workspace_to_dict(row: WorkspaceRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id) if row.session_id else None,
        "path": row.path,
        "mode": row.mode,
        "name": row.name,
        "quota": row.quota or {},
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_workspaces(
    *, session_id: str | uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """按登记时间列出工作区；给 `session_id` 时只列该会话绑定的那些。"""

    statement = select(WorkspaceRecord).order_by(
        WorkspaceRecord.created_at.asc(), WorkspaceRecord.id.asc()
    )
    if session_id is not None:
        statement = statement.where(WorkspaceRecord.session_id == _as_uuid(session_id))
    with get_session_factory()() as session:
        return [_workspace_to_dict(row) for row in session.scalars(statement).all()]


def get_workspace(workspace_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(WorkspaceRecord, _as_uuid(workspace_id))
        return _workspace_to_dict(row) if row else None


def find_workspace_by_path(path: str) -> dict[str, Any] | None:
    statement = select(WorkspaceRecord).where(WorkspaceRecord.path == path)
    with get_session_factory()() as session:
        row = session.scalars(statement).first()
        return _workspace_to_dict(row) if row else None


def create_workspace(
    *,
    workspace_id: str | uuid.UUID,
    path: str,
    mode: str,
    session_id: str | uuid.UUID | None = None,
    name: str | None = None,
    quota: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    record = WorkspaceRecord(
        id=_as_uuid(workspace_id),
        session_id=_as_uuid(session_id) if session_id else None,
        path=path,
        mode=mode,
        name=name,
        quota=quota or {},
        created_by=created_by,
    )
    with get_session_factory()() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return _workspace_to_dict(record)


def delete_workspace(workspace_id: str | uuid.UUID) -> bool:
    """只解除登记，**不删宿主文件**（`doc/api.md` §5.19）。"""

    with get_session_factory()() as session:
        row = session.get(WorkspaceRecord, _as_uuid(workspace_id))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def _tool_call_to_dict(row: ToolCall) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "workflow_run_id": (
            str(row.workflow_run_id) if row.workflow_run_id else None
        ),
        "tool_name": row.tool_name,
        "input": row.input,
        "output": row.output,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }

def _row_to_dict(row: WorkflowRun) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id) if row.session_id else None,
        "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
        "instance_id": row.instance_id,
        "status": row.status,
        "checkpoint": row.checkpoint,
        "current_step": row.current_step,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
    }


def _session_to_dict(row: SessionRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_id": row.user_id,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _agent_run_to_dict(row: AgentRun) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _message_to_dict(row: Message) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "role": row.role,
        "content": row.content,
        "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
        "status": row.status,
        "created_at": row.created_at,
    }


def create_session(user_id: str | None = None) -> dict[str, Any]:
    row = SessionRecord(id=uuid.uuid4(), user_id=user_id, status="active")
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def get_session(session_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(SessionRecord, _as_uuid(session_id))
        return _session_to_dict(row) if row else None


def list_sessions(
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """按 `updated_at` 倒序分页列出会话，每项附带摘要字段。

    摘要由「首条用户消息 + 最新一次 workflow 终态」构成，供前端历史列表直接
    渲染，无需逐会话二次请求（`doc/api.md` §5.13）。

    **只返回至少有一条 `messages` 的会话**（接口层兜底，`doc/api.md` §5.13 /
    `doc/data-model.md` §3 生命周期不变量）：前端在首条消息提交时才建会话，
    绕过前端直接 `POST /sessions` 造出的空行不该堆在历史列表里。`total` 与
    `rows` 用同一条过滤条件，否则分页会错位。判定用「存在消息」而非「存在
    `role=user` 的消息」——消息先于 workflow 写入（§4.4），所以跑过任务的
    会话必然命中，不会被误过滤。

    返回 `(items, total)`；`items` 每项为 `_session_to_dict` 基础上额外注入
    `title`（首条用户消息截断）与 `latest_workflow_status`（可空）。
    """

    from sqlalchemy import func

    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    with get_session_factory()() as session:
        # 用 `IN (SELECT session_id FROM messages)` 而不是 `EXISTS(关联子查询)`：
        # 后者会被 SQLAlchemy 编译成自带 FROM 的非关联子查询（`FROM messages, sessions`），
        # 语义退化成「只要库里存在任意一条消息就全部通过」，过滤形同虚设。
        has_message = SessionRecord.id.in_(select(Message.session_id))
        total = (
            session.scalar(
                select(func.count())
                .select_from(SessionRecord)
                .where(has_message)
            )
            or 0
        )
        rows = session.scalars(
            select(SessionRecord)
            .where(has_message)
            .order_by(SessionRecord.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        session_ids = [row.id for row in rows]

        # 首条用户消息：每个会话取最早一条 role='user' 的消息作为标题。
        title_by_session: dict[uuid.UUID, str] = {}
        if session_ids:
            user_rows = session.scalars(
                select(Message)
                .where(
                    Message.session_id.in_(session_ids),
                    Message.role == "user",
                )
                .order_by(Message.created_at.asc())
            ).all()
            seen: set[uuid.UUID] = set()
            for msg in user_rows:
                if msg.session_id in seen:
                    continue
                seen.add(msg.session_id)
                content = (msg.content or "").strip()
                title_by_session[msg.session_id] = (
                    content[:60] + ("…" if len(content) > 60 else "")
                ) if content else "（无文本消息）"

        # 最新一次 workflow 终态：每会话取 created_at 最新的一条 workflow。
        status_by_session: dict[uuid.UUID, str] = {}
        workflow_id_by_session: dict[uuid.UUID, str] = {}
        if session_ids:
            wf_rows = session.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.session_id.in_(session_ids))
                .order_by(WorkflowRun.created_at.desc())
            ).all()
            seen_wf: set[uuid.UUID] = set()
            for wf in wf_rows:
                if wf.session_id in seen_wf or wf.session_id is None:
                    continue
                seen_wf.add(wf.session_id)
                status_by_session[wf.session_id] = wf.status
                workflow_id_by_session[wf.session_id] = str(wf.id)

        items = []
        for row in rows:
            item = _session_to_dict(row)
            item["title"] = title_by_session.get(row.id, "（暂无消息）")
            item["latest_workflow_status"] = status_by_session.get(row.id)
            item["latest_workflow_id"] = workflow_id_by_session.get(row.id)
            items.append(item)
        return (items, int(total))


def delete_session(session_id: str | uuid.UUID) -> bool:
    """删除会话及其关联数据，返回是否真的删除了（`False` = 会话不存在）。

    关联数据按依赖顺序自底向上清理（`workflow_runs.session_id` 与
    `agent_runs.session_id`/`messages.session_id` 有 FK CASCADE，`workflow_runs`
    的 `session_id` 无 FK；SQLite 测试替身默认不启用 PRAGMA foreign_keys，
    所以这里统一显式删，不依赖数据库级联）：

    - `tool_calls`（按该会话的 `workflow_run_id` 与 `run_id`）
    - `metrics`（按 `labels.workflow_id`，尽力而为，失败不阻断）
    - `workflow_runs` / `messages` / `agent_runs`（按 `session_id`）
    - `sessions` 本身
    """

    sid = _as_uuid(session_id)
    with get_session_factory()() as session:
        row = session.get(SessionRecord, sid)
        if row is None:
            return False

        workflow_ids = [
            r[0]
            for r in session.query(WorkflowRun.id)
            .filter(WorkflowRun.session_id == sid)
            .all()
        ]
        run_ids = [
            r[0]
            for r in session.query(AgentRun.id)
            .filter(AgentRun.session_id == sid)
            .all()
        ]

        # 工具调用：挂在本会话的 workflow_run 或 agent_run 之下。
        if workflow_ids or run_ids:
            q = session.query(ToolCall)
            if workflow_ids:
                q = q.filter(
                    (ToolCall.workflow_run_id.in_(workflow_ids))
                    | (ToolCall.run_id.in_(run_ids))
                )
            else:
                q = q.filter(ToolCall.run_id.in_(run_ids))
            q.delete(synchronize_session=False)

        # 观测采样：labels 里带 workflow_id 的才属于本会话。JSONB 查询只在
        # PostgreSQL 上可靠，SQLite 替身下跳过（孤儿采样可接受，不阻断删除）。
        if workflow_ids:
            try:
                session.query(MetricRecord).filter(
                    MetricRecord.labels["workflow_id"].as_string().in_(
                        [str(w) for w in workflow_ids]
                    )
                ).delete(synchronize_session=False)
            except Exception:
                pass

        session.query(WorkflowRun).filter(WorkflowRun.session_id == sid).delete(
            synchronize_session=False
        )
        session.query(Message).filter(Message.session_id == sid).delete(
            synchronize_session=False
        )
        session.query(AgentRun).filter(AgentRun.session_id == sid).delete(
            synchronize_session=False
        )
        session.delete(row)
        session.commit()
        return True


def update_session_status(session_id: str | uuid.UUID, status: str) -> dict[str, Any]:
    if status not in {"active", "paused"}:
        raise ValueError(f"unknown session status: {status}")
    with get_session_factory()() as session:
        row = session.get(SessionRecord, _as_uuid(session_id))
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def create_agent_run(
    session_id: str | uuid.UUID,
    *,
    agent_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = AgentRun(
        id=uuid.uuid4(),
        session_id=_as_uuid(session_id),
        agent_id=_as_uuid(agent_id) if agent_id else None,
        status="queued",
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _agent_run_to_dict(row)


def update_agent_run_status(
    agent_run_id: str | uuid.UUID,
    status: str,
) -> dict[str, Any]:
    if status not in {"queued", "running", "paused", "completed", "failed"}:
        raise ValueError(f"unknown agent run status: {status}")
    with get_session_factory()() as session:
        row = session.get(AgentRun, _as_uuid(agent_run_id))
        if row is None:
            raise KeyError(f"agent run not found: {agent_run_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _agent_run_to_dict(row)


def update_message_status(
    message_id: str | uuid.UUID,
    status: str,
) -> dict[str, Any]:
    if status not in {"queued", "running", "completed", "failed"}:
        raise ValueError(f"unknown message status: {status}")
    with get_session_factory()() as session:
        row = session.get(Message, _as_uuid(message_id))
        if row is None:
            raise KeyError(f"message not found: {message_id}")
        row.status = status
        session.commit()
        session.refresh(row)
        return _message_to_dict(row)


def create_message(
    session_id: str | uuid.UUID,
    *,
    content: str,
    role: str = "user",
    status: str = "queued",
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = Message(
        id=uuid.uuid4(),
        session_id=_as_uuid(session_id),
        role=role,
        content=content,
        status=status,
        agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _message_to_dict(row)


def upsert_message(
    session_id: str | uuid.UUID,
    *,
    message_id: str | uuid.UUID,
    content: str,
    role: str,
    status: str = "queued",
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """按固定 ID 写入消息，重复调用返回已有记录。

    Dapr 活动允许重放，因此终态写入的报告消息必须幂等：ID 由调用方按
    Workflow 派生，这里用 ``ON CONFLICT DO NOTHING`` 保证同一执行只留一条
    （见 ADR-008）。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    message_uuid = _as_uuid(message_id)
    with get_session_factory()() as session:
        session.execute(
            pg_insert(Message)
            .values(
                id=message_uuid,
                session_id=_as_uuid(session_id),
                role=role,
                content=content,
                status=status,
                agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()
        row = session.get(Message, message_uuid)
        if row is None:
            raise KeyError(f"message upsert failed: {message_id}")
        return _message_to_dict(row)


def list_messages(
    session_id: str | uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    from sqlalchemy import func, select

    session_uuid = _as_uuid(session_id)
    with get_session_factory()() as session:
        total = session.scalar(
            select(func.count()).select_from(Message).where(Message.session_id == session_uuid)
        ) or 0
        rows = session.scalars(
            select(Message)
            .where(Message.session_id == session_uuid)
            .order_by(Message.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return ([_message_to_dict(row) for row in reversed(rows)], int(total))


def _attachment_meta_columns() -> tuple[Any, ...]:
    """列表回读用的列集合：**刻意排除 `data` 与 `text_content`**。

    两者都是大字段：`data` 最坏是一份 5 MB 的原件，一份 4 附件的消息回读列表时
    等于把 20 MB 字节从库搬到进程里再扔掉。`has_original` 交给数据库算——
    `data IS NOT NULL` 是一个布尔表达式，不需要把 `data` 本身取出来。
    """

    return (
        AttachmentRecord.id,
        AttachmentRecord.session_id,
        AttachmentRecord.message_id,
        AttachmentRecord.name,
        AttachmentRecord.mime,
        AttachmentRecord.size_bytes,
        AttachmentRecord.kind,
        AttachmentRecord.status,
        AttachmentRecord.error,
        AttachmentRecord.created_at,
        AttachmentRecord.data.is_not(None).label("has_original"),
    )


def _attachment_meta(
    row: AttachmentRecord, has_original: bool | None = None
) -> dict[str, Any]:
    """附件的展示字段。**不含** `data` / `text_content`：列表与消息回读都走这里，
    字节只在下载与执行阶段按 id 单独取。

    `has_original` 是给界面用的：没有字节的行（ADR-024 之前落库的文本/文档附件）
    不该显示一个必然 404 的下载入口。走 `_attachment_meta_columns()` 取行时由数据库
    算好并显式传入；直接拿 ORM 对象时（单行读取，`data` 本来就在手边）自行推导。
    """

    if has_original is None:
        has_original = getattr(row, "data", None) is not None

    return {
        "id": str(row.id),
        "session_id": str(row.session_id) if row.session_id else None,
        "message_id": str(row.message_id) if row.message_id else None,
        "name": row.name,
        "mime": row.mime,
        "size_bytes": int(row.size_bytes or 0),
        "kind": row.kind,
        "status": row.status,
        "error": row.error,
        "has_original": has_original,
        "created_at": row.created_at,
    }


def create_attachment(
    *,
    name: str,
    mime: str,
    kind: str,
    status: str,
    size_bytes: int,
    data: bytes | None = None,
    text_content: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    row = AttachmentRecord(
        id=uuid.uuid4(),
        name=name,
        mime=mime,
        kind=kind,
        status=status,
        size_bytes=size_bytes,
        data=data,
        text_content=text_content,
        error=error,
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _attachment_meta(row)


def get_attachment(attachment_id: str | uuid.UUID) -> dict[str, Any] | None:
    """取附件元数据；不含字节与正文。"""

    with get_session_factory()() as session:
        row = session.get(AttachmentRecord, _as_uuid(attachment_id))
        return _attachment_meta(row) if row else None


def get_attachment_content(attachment_id: str | uuid.UUID) -> dict[str, Any] | None:
    """取附件**含字节与正文**的完整行，供下载与执行阶段使用。"""

    with get_session_factory()() as session:
        row = session.get(AttachmentRecord, _as_uuid(attachment_id))
        if row is None:
            return None
        payload = _attachment_meta(row)
        payload["data"] = bytes(row.data) if row.data is not None else None
        payload["text_content"] = row.text_content
        return payload


def delete_attachment(attachment_id: str | uuid.UUID) -> bool:
    with get_session_factory()() as session:
        row = session.get(AttachmentRecord, _as_uuid(attachment_id))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def link_attachments(
    attachment_ids: list[str],
    *,
    message_id: str | uuid.UUID,
    session_id: str | uuid.UUID,
) -> list[str]:
    """把尚未归属任何消息的附件挂到消息上，返回真正挂上的 id。

    **只接受 `message_id IS NULL` 的行**：附件 id 是可猜的（uuid4 只是难以枚举，
    不是权限），若允许改挂已归属的附件，任何人都能把别人消息里的附件挪走。
    没挂上的 id 会出现在返回值里（缺失）而不抛错——由接口层决定怎么报，
    因为「id 不存在」与「已经挂过了」对用户是同一件事：这个附件用不上。
    """

    wanted: list[uuid.UUID] = []
    for value in attachment_ids:
        try:
            wanted.append(_as_uuid(value))
        except (ValueError, AttributeError):
            continue
    if not wanted:
        return []

    linked: list[str] = []
    with get_session_factory()() as session:
        rows = session.scalars(
            select(AttachmentRecord).where(
                AttachmentRecord.id.in_(wanted),
                AttachmentRecord.message_id.is_(None),
            )
        ).all()
        message_uuid = _as_uuid(message_id)
        session_uuid = _as_uuid(session_id)
        for row in rows:
            row.message_id = message_uuid
            row.session_id = session_uuid
            linked.append(str(row.id))
        session.commit()

    order = {value: index for index, value in enumerate(attachment_ids)}
    return sorted(linked, key=lambda item: order.get(item, len(order)))


def _attachment_meta_statement(message_ids: list[uuid.UUID]) -> Any:
    """`list_attachments_for_messages` 的唯一查询语句。

    单独抽出来是为了能被测试直接编译断言：**这条语句绝不能出现 `attachments.data`**。
    """

    return (
        select(*_attachment_meta_columns())
        .where(AttachmentRecord.message_id.in_(message_ids))
        .order_by(AttachmentRecord.created_at)
    )


def list_attachments_for_messages(
    message_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """一次取多条消息的附件，按 `message_id` 分组；顺序按上传时间。

    只 SELECT 元数据列（见 `_attachment_meta_columns`）：消息列表是整页回读，
    把每份附件的 `data` 一起读出来会白白搬运几十 MB。
    """

    if not message_ids:
        return {}
    wanted: list[uuid.UUID] = []
    for value in message_ids:
        try:
            wanted.append(_as_uuid(value))
        except (ValueError, AttributeError):
            continue
    if not wanted:
        return {}

    with get_session_factory()() as session:
        rows = session.execute(_attachment_meta_statement(wanted)).all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.message_id), []).append(
            _attachment_meta(row, row.has_original)
        )
    return grouped


def list_attachments_for_session(session_id: str | uuid.UUID) -> list[dict[str, Any]]:
    """会话内全部附件的元数据（不含字节与正文），按上传时间。

    与 `list_attachments_for_messages` 同口径、同列：只取元数据列，
    `has_original` 由库侧 `data IS NOT NULL` 算出。
    """

    try:
        wanted = _as_uuid(session_id)
    except (ValueError, AttributeError):
        return []

    with get_session_factory()() as session:
        rows = session.execute(
            select(*_attachment_meta_columns())
            .where(AttachmentRecord.session_id == wanted)
            .order_by(AttachmentRecord.created_at)
        ).all()
    return [_attachment_meta(row, row.has_original) for row in rows]


def load_attachment_payloads(attachment_ids: list[str]) -> list[dict[str, Any]]:
    """按传入顺序取附件的完整内容（含字节与正文），供执行阶段构造模型输入。

    顺序即用户上传顺序——提示词里附件的编号与界面上的顺序必须一致，
    否则用户在界面上看到「附件 1 是发票」，模型读到的却是另一份。
    """

    if not attachment_ids:
        return []
    wanted: list[uuid.UUID] = []
    for value in attachment_ids:
        try:
            wanted.append(_as_uuid(value))
        except (ValueError, AttributeError):
            continue
    if not wanted:
        return []

    with get_session_factory()() as session:
        rows = session.scalars(
            select(AttachmentRecord).where(AttachmentRecord.id.in_(wanted))
        ).all()

    by_id = {str(row.id): row for row in rows}
    payloads: list[dict[str, Any]] = []
    for value in attachment_ids:
        row = by_id.get(str(value))
        if row is None:
            continue
        item = _attachment_meta(row)
        item["data"] = bytes(row.data) if row.data is not None else None
        item["text_content"] = row.text_content
        payloads.append(item)
    return payloads


def get_latest_workflow(
    session_id: str | uuid.UUID,
    *,
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    from sqlalchemy import select

    statement = (
        select(WorkflowRun)
        .where(WorkflowRun.session_id == _as_uuid(session_id))
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    if statuses:
        statement = statement.where(WorkflowRun.status.in_(statuses))
    with get_session_factory()() as session:
        row = session.scalars(statement).first()
        return _row_to_dict(row) if row else None


def get_workflow_run(workflow_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(WorkflowRun, _as_uuid(workflow_id))
        return _row_to_dict(row) if row else None


def list_workflows_for_session(
    session_id: str | uuid.UUID,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按创建时间**升序**列出会话的全部 Workflow（`doc/api.md` §5.18）。

    升序是语义要求而不是排序偏好：前端按顺序给每个对话编号（第 1 / 2 / 3 个对话）。
    倒序会让既有对话的编号随着新对话一起跳动，用户就没法用「对话 3」指代那一次工作流了。
    """

    from sqlalchemy import select

    statement = (
        select(WorkflowRun)
        .where(WorkflowRun.session_id == _as_uuid(session_id))
        .order_by(WorkflowRun.created_at.asc())
        .limit(limit)
    )
    with get_session_factory()() as session:
        rows = session.scalars(statement).all()
        return [_row_to_dict(row) for row in rows]


_REGISTRY_COLUMN_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    # ADR-017 给两张既有表新增的列。`create_all` 只建缺失的**表**，不会给已存在的表补列，
    # 因此升级一个跑过旧版本的环境时必须显式补。
    "agent_configs": (
        ("llm_model_id", "VARCHAR(80)"),
        ("top_p", "FLOAT"),
        ("max_output_tokens", "INTEGER"),
        ("reasoning_type", "VARCHAR(20)"),
    ),
    "provider_configs": (("default_llm_model_id", "VARCHAR(80)"),),
}
"""表 → (列名, 列类型) 的补列清单，列类型与 ORM 在 PostgreSQL 上的渲染保持一致。"""


def _registry_migration_statements(dialect_name: str) -> list[str]:
    """生成幂等补列语句；非 PostgreSQL 方言返回空表。

    `ADD COLUMN IF NOT EXISTS` 是 PostgreSQL 语法（SQLite 不支持），而本项目的事实源
    只有 PostgreSQL；测试用的临时 SQLite 引擎不经过本函数。
    """

    if dialect_name != "postgresql":
        return []
    return [
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        for table, columns in _REGISTRY_COLUMN_MIGRATIONS.items()
        for column, ddl_type in columns
    ]


def _apply_registry_migrations(engine: Any) -> list[str]:
    """执行补列语句，返回已应用的语句列表（幂等，可重复调用）。"""

    statements = _registry_migration_statements(engine.dialect.name)
    if not statements:
        return []
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
    return statements


def init_checkpoint_schema() -> None:
    from app.core.storage import get_engine

    engine = get_engine()
    Base.metadata.create_all(engine)
    # ADR-017：补齐既有表的新增列（新库由 `create_all` 直接建全，此处为幂等空操作）。
    _apply_registry_migrations(engine)
    # 内置流水线角色种子（幂等）。
    seed_builtin_agents()


def create_workflow_run(
    *,
    workflow_id: str | uuid.UUID,
    session_id: str | uuid.UUID | None = None,
    agent_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    row = WorkflowRun(
        id=_as_uuid(workflow_id),
        session_id=_as_uuid(session_id) if session_id else None,
        agent_run_id=_as_uuid(agent_run_id) if agent_run_id else None,
        status="pending",
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def _validate_transition(current_status: str, next_status: str | None) -> None:
    if next_status is None or next_status == current_status:
        return
    if current_status not in ALLOWED_TRANSITIONS:
        raise ValueError(f"unknown workflow status: {current_status}")
    if next_status not in ALLOWED_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid workflow status transition: {current_status} -> {next_status}"
        )


def update_workflow_run(
    workflow_id: str | uuid.UUID,
    *,
    status: str | None = None,
    current_step: str | None = None,
    checkpoint: dict[str, Any] | None = None,
    instance_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    workflow_uuid = _as_uuid(workflow_id)
    with get_session_factory()() as session:
        row = session.get(WorkflowRun, workflow_uuid)
        if row is None:
            raise KeyError(f"workflow run not found: {workflow_id}")
        _validate_transition(row.status, status)
        if status is not None:
            row.status = status
        if current_step is not None:
            row.current_step = current_step
        if checkpoint is not None:
            row.checkpoint = checkpoint
        if instance_id is not None:
            row.instance_id = instance_id
        if error is not None:
            row.error = error
        if status in {"completed", "failed", "cancelled"} and row.completed_at is None:
            row.completed_at = _utcnow()
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def get_tool_call(call_id: str | uuid.UUID) -> dict[str, Any] | None:
    with get_session_factory()() as session:
        row = session.get(ToolCall, _as_uuid(call_id))
        return _tool_call_to_dict(row) if row else None


def create_tool_call(
    *,
    call_id: str | uuid.UUID,
    run_id: str | uuid.UUID,
    tool_name: str,
    tool_input: dict[str, Any],
    workflow_run_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    tool_name = tool_name.strip()
    if not tool_name:
        raise ValueError('tool_name must not be empty')
    call_uuid = _as_uuid(call_id)
    values = {
        "id": call_uuid,
        "run_id": _as_uuid(run_id),
        "workflow_run_id": (
            _as_uuid(workflow_run_id) if workflow_run_id else None
        ),
        "tool_name": tool_name,
        "input": tool_input,
        "status": "running",
    }
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with get_session_factory()() as session:
        session.execute(
            pg_insert(ToolCall)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call upsert failed: {call_id}")
        return _tool_call_to_dict(row)


def mark_tool_call_running(call_id: str | uuid.UUID) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        if row.status == "succeeded":
            return _tool_call_to_dict(row)
        row.status = "running"
        row.output = None
        row.error = None
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def complete_tool_call(
    call_id: str | uuid.UUID,
    *,
    output: Any,
) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        row.status = "succeeded"
        row.output = output
        row.error = None
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def fail_tool_call(
    call_id: str | uuid.UUID,
    *,
    error: str,
) -> dict[str, Any]:
    call_uuid = _as_uuid(call_id)
    with get_session_factory()() as session:
        row = session.get(ToolCall, call_uuid)
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        row.status = "failed"
        row.output = None
        row.error = error
        session.commit()
        session.refresh(row)
        return _tool_call_to_dict(row)


def list_tool_calls(
    *,
    run_id: str | uuid.UUID | None = None,
    workflow_run_id: str | uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    statement = select(ToolCall).order_by(ToolCall.created_at.desc()).limit(limit)
    if run_id is not None:
        statement = statement.where(ToolCall.run_id == _as_uuid(run_id))
    if workflow_run_id is not None:
        statement = statement.where(
            ToolCall.workflow_run_id == _as_uuid(workflow_run_id)
        )
    with get_session_factory()() as session:
        rows = session.scalars(statement).all()
        return [_tool_call_to_dict(row) for row in rows]

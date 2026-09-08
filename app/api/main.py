from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.store import SqlApiStore
from app.workflows.pipeline import WorkflowTask
from app.workflows.service import get_workflow_service


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


app = FastAPI(
    title="Multi-Agent Collaboration Platform",
    version="0.1.0",
)

# The worker process uses the SQL adapter. Tests can replace this value with
# InMemoryApiStore without changing route behavior or requiring PostgreSQL.
api_store: Any = SqlApiStore()


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


@app.post("/api/v1/sessions", response_model=SessionResponse, status_code=201)
def create_session(payload: SessionCreateRequest) -> SessionResponse:
    return SessionResponse.model_validate(api_store.create_session(payload.user_id))


@app.get("/api/v1/sessions/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    return SessionResponse.model_validate(_session_or_404(session_id))


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
        get_workflow_service().schedule(
            WorkflowTask(
                workflow_id=workflow_id,
                task=payload.content,
                session_id=session_id,
                agent_run_id=agent_run["id"],
            )
        )
        api_store.update_workflow(workflow_id, status="running")
        api_store.update_agent_run_status(agent_run["id"], "running")
    except Exception as exc:
        api_store.update_agent_run_status(agent_run["id"], "failed")
        api_store.update_workflow(workflow_id, status="failed")
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

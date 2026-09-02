from fastapi import FastAPI
from pydantic import BaseModel

from app.orchestration.graph import run_agent


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


app = FastAPI(
    title="Multi-Agent Collaboration Platform",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/sessions/{session_id}/messages", response_model=ChatResponse)
def send_message(session_id: str, payload: ChatRequest) -> ChatResponse:
    messages = run_agent(payload.message)
    return ChatResponse(reply=messages[-1].content)

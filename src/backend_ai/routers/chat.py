from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from src.backend_ai.agents.graph import agent
from src.backend_ai.dtos.chat import ChatRequest, ChatResponse
from src.shared.dependencies.auth import CurrentUser, require_permission

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: Annotated[CurrentUser, Depends(require_permission("applications.read_own"))],
) -> ChatResponse:
    """Chat với AI agent."""
    _ = current_user
    try:
        result = await agent.ainvoke({"query": request.message})
        return ChatResponse(
            response=result.get("response", ""),
            analysis=result.get("analysis", ""),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def agent_status():
    """Kiểm tra trạng thái agent."""
    return {"status": "ready", "agent": "LangGraph Agent v1.0"}

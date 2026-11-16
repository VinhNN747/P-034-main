from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendClient, get_ai_backend_client
from src.backend.services.remote_support_synthesizer import RemoteSupportResponseSynthesizer
from src.backend.services.remote_support_understanding import RemoteSupportUnderstanding
from src.backend.services.support_chat_service import (
    SupportChatAccessError,
    SupportChatNotFoundError,
    SupportChatPermissionError,
)
from src.backend.services.support_conversation_service import SupportChatRateLimitError, SupportConversationService
from src.backend.services.support_data_service import SupportDataUnavailableError
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_permission
from src.shared.dtos.support_chat import (
    SupportAnalyticsData,
    SupportChatRequest,
    SupportChatResponse,
    SupportChatSession,
    SupportChatSessionDetail,
    SupportChatSessionPage,
    SupportChatSessionUpdate,
)


def private_chat_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/support", tags=["support-chat"], dependencies=[Depends(private_chat_response)])
SupportUser = Annotated[CurrentUser, Depends(require_permission("applications.read_own"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/sessions", response_model=SupportChatSessionPage)
async def list_support_sessions(
    current_user: SupportUser,
    session: DbSession,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> SupportChatSessionPage:
    try:
        return await SupportConversationService(session).list_sessions(current_user, offset=offset, limit=limit)
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc


@router.post("/sessions", response_model=SupportChatSession, status_code=status.HTTP_201_CREATED)
async def create_support_session(current_user: SupportUser, session: DbSession) -> SupportChatSession:
    try:
        return await SupportConversationService(session).create_session(current_user)
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc


@router.get("/sessions/{session_id}", response_model=SupportChatSessionDetail)
async def get_support_session(
    session_id: str,
    current_user: SupportUser,
    session: DbSession,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> SupportChatSessionDetail:
    try:
        return await SupportConversationService(session).get_session(
            current_user, session_id, offset=offset, limit=limit
        )
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc
    except SupportChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc


@router.patch("/sessions/{session_id}", response_model=SupportChatSession)
async def update_support_session(
    session_id: str,
    payload: SupportChatSessionUpdate,
    current_user: SupportUser,
    session: DbSession,
) -> SupportChatSession:
    try:
        return await SupportConversationService(session).update_session(current_user, session_id, payload)
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc
    except SupportChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_support_session(session_id: str, current_user: SupportUser, session: DbSession) -> Response:
    try:
        await SupportConversationService(session).delete_session(current_user, session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc
    except SupportChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc


@router.post("/chat", response_model=SupportChatResponse)
async def support_chat(
    payload: SupportChatRequest,
    current_user: SupportUser,
    session: DbSession,
    ai_client: Annotated[AIBackendClient, Depends(get_ai_backend_client)],
) -> SupportChatResponse:
    try:
        return await SupportConversationService(
            session,
            RemoteSupportResponseSynthesizer(ai_client),
            RemoteSupportUnderstanding(ai_client),
        ).answer(
            question=payload.message,
            current_user=current_user,
            context=payload.context,
            session_id=payload.session_id,
        )
    except SupportDataUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Không thể đọc số liệu lúc này. Hãy thử lại.") from exc
    except SupportChatRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail="Bạn gửi quá nhiều câu hỏi. Hãy thử lại sau một phút.",
            headers={"Retry-After": "60"},
        ) from exc
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc
    except SupportChatPermissionError as exc:
        raise HTTPException(status_code=403, detail="Insufficient permission for the requested support data") from exc
    except SupportChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/charts/{chart_id}", response_model=SupportAnalyticsData)
async def support_chart(
    chart_id: UUID, current_user: SupportUser, session: DbSession, response: Response
) -> SupportAnalyticsData:
    try:
        data = await SupportConversationService(session).chart_data(current_user, str(chart_id))
        if data.operation == "failures" or not data.rows or all(row.value is None for row in data.rows):
            raise HTTPException(status_code=422, detail="Không có dữ liệu để vẽ biểu đồ.")
        response.headers["Cache-Control"] = "private, no-store"
        return data
    except SupportChatAccessError as exc:
        raise HTTPException(status_code=403, detail="Chatbot is not available for this workspace") from exc
    except SupportChatPermissionError as exc:
        raise HTTPException(status_code=403, detail="Insufficient permission for the requested support data") from exc
    except SupportChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chart not found") from exc

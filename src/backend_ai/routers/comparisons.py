from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.services.comparison_service import ComparisonExecutionService, ComparisonValidationError
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.dtos.comparison import ComparisonAccepted, ComparisonCreate

router = APIRouter(prefix="/gateway/comparisons", tags=["gateway-comparisons"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("", response_model=ComparisonAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_comparison(
    payload: ComparisonCreate,
    user: AuthUser,
    session: DbSession,
) -> ComparisonAccepted:
    try:
        return await ComparisonExecutionService(session).create(payload, user)
    except ComparisonValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

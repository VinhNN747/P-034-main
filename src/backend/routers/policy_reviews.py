from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.policy_reviews import PolicyDecisionCreate, PolicyReviewPage, PolicyReviewRead
from src.backend.services.policy_review_service import (
    PolicyReviewConflictError,
    PolicyReviewNotFoundError,
    PolicyReviewService,
)
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_permission

router = APIRouter(prefix="/policy-reviews", tags=["policy-reviews"])
ReadUser = Annotated[CurrentUser, Depends(require_permission("routing_policies.read"))]
ManageUser = Annotated[CurrentUser, Depends(require_permission("routing_policies.manage"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=PolicyReviewPage)
async def list_policy_reviews(
    _: ReadUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PolicyReviewPage:
    return await PolicyReviewService(session).list(
        application_id=application_id, status=status, page=page, page_size=page_size
    )


@router.post("/{review_id}/decision", response_model=PolicyReviewRead)
async def decide_policy_review(
    review_id: str,
    payload: PolicyDecisionCreate,
    user: ManageUser,
    session: DbSession,
) -> PolicyReviewRead:
    try:
        return await PolicyReviewService(session).decide(user.id, review_id, payload)
    except PolicyReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PolicyReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

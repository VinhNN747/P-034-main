from typing import Annotated

from fastapi import APIRouter, Depends

from src.shared.dependencies.auth import CurrentUser, require_permission

router = APIRouter(prefix="/platform", tags=["platform-admin"])


@router.get("/status")
async def platform_status(current_user: Annotated[CurrentUser, Depends(require_permission("incidents.read"))]):
    _ = current_user
    return {"status": "ready", "scope": "platform"}

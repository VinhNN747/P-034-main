from typing import Annotated

from fastapi import APIRouter, Depends

from src.shared.dependencies.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/authorization", tags=["authorization"])


@router.get("/me")
async def authorization_me(current_user: Annotated[CurrentUser, Depends(get_current_user)]):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "authentication_complete": current_user.authentication_complete,
        "roles": current_user.roles,
        "permissions": sorted(current_user.permissions),
    }

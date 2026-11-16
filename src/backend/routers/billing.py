from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_permission
from src.shared.dtos.billing import (
    BillingAccountRead,
    BillingSettingsRead,
    BillingSettingsUpdate,
    BillingTopupCreate,
    BillingTopupRead,
    BillingTransactionPage,
    BillingTransactionRead,
)
from src.shared.models.billing import BillingAccountRecord, BillingTransactionRecord
from src.shared.services.billing_service import BillingConflictError, BillingService, BillingValidationError

router = APIRouter(prefix="/billing", tags=["billing"])
ReadUser = Annotated[CurrentUser, Depends(require_permission("billing.read"))]
ManageUser = Annotated[CurrentUser, Depends(require_permission("billing.manage"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def transaction_read(row: BillingTransactionRecord) -> BillingTransactionRead:
    return BillingTransactionRead(
        id=row.id,
        type=row.transaction_type,
        amount=Decimal(row.amount),
        currency=row.currency,
        status=row.status,
        timestamp=row.created_at,
        reference=row.reference_id or row.idempotency_key or row.source_request_id,
        description=row.description,
        simulation=bool((row.metadata_json or {}).get("simulation", False)),
    )


def account_read(
    account: BillingAccountRecord,
    transaction: BillingTransactionRecord | None,
    simulated_topup_used: bool,
) -> BillingAccountRead:
    return BillingAccountRead(
        balance=Decimal(account.balance),
        currency=account.currency,
        lifetime_topup=Decimal(account.lifetime_topup),
        lifetime_usage=Decimal(account.lifetime_usage),
        last_transaction=transaction_read(transaction) if transaction else None,
        simulated_topup_used=simulated_topup_used,
    )


@router.get("/account", response_model=BillingAccountRead)
async def get_account(user: ReadUser, session: DbSession) -> BillingAccountRead:
    service = BillingService(session)
    account = await service.account(user.id)
    latest, _ = await service.transactions(user.id, page=1, page_size=1)
    topup_used = await service.has_simulated_topup(account.id)
    return account_read(account, latest[0] if latest else None, topup_used)


@router.get("/transactions", response_model=BillingTransactionPage)
async def list_transactions(
    user: ReadUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> BillingTransactionPage:
    rows, total = await BillingService(session).transactions(user.id, page=page, page_size=page_size)
    return BillingTransactionPage(
        items=[transaction_read(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/settings", response_model=BillingSettingsRead)
async def get_settings(user: ReadUser, session: DbSession) -> BillingSettingsRead:
    settings = await BillingService(session).settings(user.id)
    return BillingSettingsRead.model_validate(settings)


@router.put("/settings", response_model=BillingSettingsRead)
async def update_settings(payload: BillingSettingsUpdate, user: ManageUser, session: DbSession) -> BillingSettingsRead:
    settings = await BillingService(session).update_settings(user.id, payload)
    return BillingSettingsRead.model_validate(settings)


@router.post("/topups", response_model=BillingTopupRead, status_code=201)
async def create_topup(
    payload: BillingTopupCreate, user: ManageUser, session: DbSession, response: Response
) -> BillingTopupRead:
    try:
        transaction, repeated = await BillingService(session).top_up(user.id, payload)
    except BillingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BillingValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.status_code = 200 if repeated else 201
    return BillingTopupRead(
        id=transaction.id,
        type=transaction.transaction_type,
        amount=Decimal(transaction.amount),
        currency=transaction.currency,
        status=transaction.status,
        timestamp=transaction.created_at,
        reference=transaction.reference_id,
        description=transaction.description,
        simulation=True,
    )

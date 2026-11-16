from __future__ import annotations

from decimal import ROUND_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.dtos.billing import SUPPORTED_CURRENCY, BillingSettingsUpdate, BillingTopupCreate, decimal_value
from src.shared.models.billing import BillingAccountRecord, BillingSettingsRecord, BillingTransactionRecord
from src.shared.models.core import Application
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.billing import BillingRepository


class BillingConflictError(Exception):
    pass


class BillingValidationError(Exception):
    pass


class BillingService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = BillingRepository(session)
        self.audit = AuditRepository(session)

    async def account(self, user_id: str) -> BillingAccountRecord:
        account = await self.repository.get_or_create_account(user_id)
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def has_simulated_topup(self, account_id: str) -> bool:
        return await self.repository.has_simulated_topup(account_id)

    async def top_up(self, user_id: str, payload: BillingTopupCreate) -> tuple[BillingTransactionRecord, bool]:
        if payload.amount != Decimal("2"):
            raise BillingValidationError("simulated top-up amount must be exactly 2 USD in this version")
        existing = await self.repository.by_idempotency_key(payload.idempotency_key)
        if existing is not None:
            account = await self.account(user_id)
            if (
                existing.account_id != account.id
                or Decimal(existing.amount) != payload.amount
                or existing.currency != payload.currency
            ):
                raise BillingConflictError("idempotency key was already used with a different payload")
            return existing, True
        account = await self.repository.get_or_create_account(user_id, for_update=True)
        if await self.repository.has_simulated_topup(account.id):
            raise BillingConflictError("simulated credit can only be added once per account in this version")
        transaction = BillingTransactionRecord(
            account_id=account.id,
            transaction_type="simulated_topup",
            amount=payload.amount,
            currency=payload.currency,
            status="completed",
            idempotency_key=payload.idempotency_key,
            reference_type="simulated_topup",
            reference_id=payload.idempotency_key,
            description="Phase-one simulated local ledger top-up",
            metadata_json={"simulation": True},
        )
        account.balance = Decimal(account.balance) + payload.amount
        account.lifetime_topup = Decimal(account.lifetime_topup) + payload.amount
        await self.repository.add_transaction(transaction)
        await self.audit.add_event(
            actor_user_id=user_id,
            action="billing.topup",
            resource_type="billing_transaction",
            resource_id=transaction.id,
            metadata={"simulation": True, "idempotency_key": payload.idempotency_key},
        )
        await self.session.commit()
        await self.session.refresh(transaction)
        return transaction, False

    async def transactions(
        self, user_id: str, *, page: int, page_size: int
    ) -> tuple[list[BillingTransactionRecord], int]:
        account = await self.account(user_id)
        total = int(
            await self.session.scalar(
                select(func.count())
                .select_from(BillingTransactionRecord)
                .where(BillingTransactionRecord.account_id == account.id)
            )
            or 0
        )
        rows = list(
            await self.session.scalars(
                select(BillingTransactionRecord)
                .where(BillingTransactionRecord.account_id == account.id)
                .order_by(BillingTransactionRecord.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return rows, total

    async def settings(self, user_id: str) -> BillingSettingsRecord:
        account = await self.repository.get_or_create_account(user_id)
        settings = await self.session.scalar(
            select(BillingSettingsRecord).where(BillingSettingsRecord.account_id == account.id)
        )
        if settings is None:
            settings = BillingSettingsRecord(account_id=account.id)
            self.session.add(settings)
            await self.session.commit()
            await self.session.refresh(settings)
        return settings

    async def update_settings(self, user_id: str, payload: BillingSettingsUpdate) -> BillingSettingsRecord:
        settings = await self.settings(user_id)
        settings.low_balance_threshold = decimal_value(payload.low_balance_threshold)
        settings.notify_low_balance = payload.notify_low_balance
        settings.auto_topup_enabled = payload.auto_topup_enabled
        await self.session.commit()
        await self.session.refresh(settings)
        return settings

    async def debit_runtime_request(
        self, application_id: str, request_id: str, amount: Decimal | float | str, request_status: str,
        *, bill_measured_usage: bool = False,
    ) -> BillingTransactionRecord | None:
        if request_status != "completed" and not bill_measured_usage:
            return None
        raw_amount = Decimal(str(amount))
        if not raw_amount.is_finite() or raw_amount <= 0:
            return None
        # Runtime pricing may be more precise than the six-decimal ledger.
        # Round positive usage upward so every billable positive cost is recorded.
        debit_amount = raw_amount.quantize(Decimal("0.000001"), rounding=ROUND_UP)
        existing = await self.repository.by_source_request(request_id)
        if existing is not None:
            return existing
        owner_id = await self.session.scalar(select(Application.owner_user_id).where(Application.id == application_id))
        if owner_id is None:
            return None
        account = await self.repository.get_or_create_account(owner_id, for_update=True)
        transaction = BillingTransactionRecord(
            account_id=account.id,
            transaction_type="usage_debit",
            amount=-debit_amount,
            currency=SUPPORTED_CURRENCY,
            status="completed",
            idempotency_key=f"runtime:{request_id}",
            source_request_id=request_id,
            reference_type="gateway_request",
            reference_id=request_id,
            description="Gateway runtime usage debit",
            metadata_json={"simulation": False, "billable": True},
        )
        account.balance = Decimal(account.balance) - debit_amount
        account.lifetime_usage = Decimal(account.lifetime_usage) + debit_amount
        await self.repository.add_transaction(transaction)
        await self.audit.add_event(
            actor_user_id=owner_id,
            action="billing.debit",
            resource_type="billing_transaction",
            resource_id=transaction.id,
            request_id=request_id,
            metadata={"amount": str(debit_amount), "request_status": request_status},
        )
        return transaction

    async def debit_comparison_usage(
        self,
        application_id: str,
        source_id: str,
        amount: Decimal | float | str,
        *,
        usage_kind: str,
    ) -> BillingTransactionRecord | None:
        """Record provider usage created only for a direct comparison or its evaluator."""
        if usage_kind not in {"reference", "evaluation"}:
            raise ValueError("usage_kind must be reference or evaluation")
        raw_amount = Decimal(str(amount))
        if not raw_amount.is_finite() or raw_amount <= 0:
            return None
        existing = await self.repository.by_source_request(source_id)
        if existing is not None:
            return existing
        owner_id = await self.session.scalar(select(Application.owner_user_id).where(Application.id == application_id))
        if owner_id is None:
            return None
        debit_amount = raw_amount.quantize(Decimal("0.000001"), rounding=ROUND_UP)
        account = await self.repository.get_or_create_account(owner_id, for_update=True)
        transaction = BillingTransactionRecord(
            account_id=account.id,
            transaction_type="usage_debit",
            amount=-debit_amount,
            currency=SUPPORTED_CURRENCY,
            status="completed",
            idempotency_key=f"comparison:{usage_kind}:{source_id}",
            source_request_id=source_id,
            reference_type=f"comparison_{usage_kind}",
            reference_id=source_id,
            description=f"Gateway comparison {usage_kind} usage debit",
            metadata_json={"simulation": False, "billable": True, "comparison_usage": usage_kind},
        )
        account.balance = Decimal(account.balance) - debit_amount
        account.lifetime_usage = Decimal(account.lifetime_usage) + debit_amount
        await self.repository.add_transaction(transaction)
        await self.audit.add_event(
            actor_user_id=owner_id,
            action="billing.debit",
            resource_type="billing_transaction",
            resource_id=transaction.id,
            request_id=source_id,
            metadata={"amount": str(debit_amount), "comparison_usage": usage_kind},
        )
        return transaction

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.billing import BillingAccountRecord, BillingTransactionRecord
from src.shared.models.core import utc_now


class BillingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def account_for_user(self, user_id: str, *, for_update: bool = False) -> BillingAccountRecord | None:
        statement = select(BillingAccountRecord).where(BillingAccountRecord.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get_or_create_account(self, user_id: str, *, for_update: bool = False) -> BillingAccountRecord:
        account = await self.account_for_user(user_id, for_update=for_update)
        if account is not None:
            return account
        if for_update:
            values = {
                "user_id": user_id,
                "balance": Decimal("0"),
                "currency": "USD",
                "lifetime_topup": Decimal("0"),
                "lifetime_usage": Decimal("0"),
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            dialect = self.session.bind.dialect.name if self.session.bind is not None else ""
            if dialect == "postgresql":
                await self.session.execute(
                    pg_insert(BillingAccountRecord).values(**values).on_conflict_do_nothing(index_elements=["user_id"])
                )
                account = await self.account_for_user(user_id, for_update=True)
                if account is not None:
                    return account
            elif dialect == "sqlite":
                await self.session.execute(
                    sqlite_insert(BillingAccountRecord).values(**values).on_conflict_do_nothing(index_elements=["user_id"])
                )
                account = await self.account_for_user(user_id, for_update=True)
                if account is not None:
                    return account
        account = BillingAccountRecord(user_id=user_id)
        self.session.add(account)
        await self.session.flush()
        return account

    async def by_idempotency_key(self, idempotency_key: str) -> BillingTransactionRecord | None:
        return await self.session.scalar(
            select(BillingTransactionRecord).where(BillingTransactionRecord.idempotency_key == idempotency_key)
        )

    async def has_simulated_topup(self, account_id: str) -> bool:
        transaction_id = await self.session.scalar(
            select(BillingTransactionRecord.id)
            .where(
                BillingTransactionRecord.account_id == account_id,
                BillingTransactionRecord.transaction_type == "simulated_topup",
            )
            .limit(1)
        )
        return transaction_id is not None

    async def by_source_request(self, request_id: str) -> BillingTransactionRecord | None:
        return await self.session.scalar(
            select(BillingTransactionRecord).where(BillingTransactionRecord.source_request_id == request_id)
        )

    async def add_transaction(self, transaction: BillingTransactionRecord) -> BillingTransactionRecord:
        self.session.add(transaction)
        await self.session.flush()
        return transaction

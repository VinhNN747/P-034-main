from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_CURRENCY = "USD"


def decimal_value(value: Decimal | float | int | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError("amount must be a valid decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be greater than zero")
    if amount.as_tuple().exponent < -6:
        raise ValueError("amount supports at most six decimal places")
    return amount.quantize(Decimal("0.000001"))


class BillingTransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: float})

    id: str
    type: str
    amount: Decimal
    currency: str
    status: str
    timestamp: datetime
    reference: str | None = None
    description: str
    simulation: bool = False


class BillingAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: float})

    balance: Decimal
    currency: str
    lifetime_topup: Decimal
    lifetime_usage: Decimal
    last_transaction: BillingTransactionRead | None = None
    simulated_topup_used: bool


class BillingTransactionPage(BaseModel):
    items: list[BillingTransactionRead]
    page: int
    page_size: int
    total: int


class BillingSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: float})

    low_balance_threshold: Decimal
    notify_low_balance: bool
    auto_topup_enabled: bool


class BillingSettingsUpdate(BaseModel):
    low_balance_threshold: Decimal = Field(gt=0, decimal_places=6, max_digits=18)
    notify_low_balance: bool = True
    auto_topup_enabled: bool = False


class BillingTopupCreate(BaseModel):
    amount: Decimal
    currency: str = SUPPORTED_CURRENCY
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("amount")
    @classmethod
    def valid_amount(cls, value: Decimal) -> Decimal:
        return decimal_value(value)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != SUPPORTED_CURRENCY:
            raise ValueError("only USD is supported")
        return normalized


class BillingTopupRead(BillingTransactionRead):
    simulation: Literal[True] = True

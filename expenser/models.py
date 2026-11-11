"""Shared data contract. Both the extraction side and the automation side use these."""
from __future__ import annotations

import hashlib
import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class Transaction(BaseModel):
    """One expense row, as it appears in the review Excel and as it is pushed to an app."""

    name: str = Field(description="Transaction name / description, e.g. 'Albert Heijn'")
    amount: Decimal = Field(description="Positive amount in the group's currency, 2 decimals")
    participants: list[str] = Field(description="Names the amount is split equally between")
    paid_by: str | None = Field(default=None, description="Who paid; must be a known participant")
    date: dt.date | None = None
    # Currency conversion trail. `amount` is what gets pushed (target currency);
    # `rate` means 1 original_currency = rate target_currency.
    original_amount: Decimal | None = None
    original_currency: str | None = None
    rate: Decimal | None = None

    @field_validator("amount")
    @classmethod
    def _two_decimals(cls, v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.01"))

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("transaction name is empty")
        return v

    def fingerprint(self) -> str:
        """Stable id used to avoid pushing the same transaction twice."""
        key = "|".join(
            [
                self.name.lower(),
                f"{self.amount:.2f}",
                self.date.isoformat() if self.date else "",
                self.paid_by or "",
                ",".join(sorted(p.lower() for p in self.participants)),
            ]
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

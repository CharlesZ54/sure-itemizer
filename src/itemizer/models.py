"""Source-neutral data shapes shared by every merchant source.

Money is always integer cents. Sign convention follows Sure's ledger:
positive = money out (a purchase), negative = money in (a refund).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Line:
    """One thing that was paid for inside a charge."""

    name: str
    # Relative size of this line, used to apportion the charge (tax, shipping
    # and discounts land proportionally). Pre-tax price * quantity for Amazon;
    # the exact amount for sources that know it, such as a single Venmo payment.
    weight_cents: int
    quantity: int = 1
    # The line's exact signed share, for sources that know it (a Venmo cash-out
    # is made of payments in and out). Used as-is instead of apportioning.
    amount_cents: int | None = None


@dataclass
class Purchase:
    """A single payment event as the merchant saw it: what hit the card."""

    source: str
    external_id: str  # stable across runs, unique within the source
    date: date
    amount_cents: int
    lines: list[Line] = field(default_factory=list)
    payment_method: str | None = None
    reference: str | None = None  # e.g. an Amazon order number
    # Set when the source found the charge but could not tell what was in it.
    problem: str | None = None
    # Set by sources that describe a transaction instead of splitting it
    # (one thing paid for, e.g. a Venmo payment): the transaction's new name.
    description: str | None = None


@dataclass(frozen=True)
class SureTxn:
    id: str
    date: date
    amount_cents: int  # signed per the convention above
    name: str
    notes: str | None
    account_id: str
    account_name: str
    category: str | None


@dataclass
class Plan:
    """What the service intends to do with one Sure transaction."""

    txn: SureTxn
    purchase: Purchase | None
    action: str  # "split" | "rename" | "skip"
    reason: str = ""
    # For "split": [(name, signed cents)].
    children: list[tuple[str, int]] = field(default_factory=list)
    # For "rename": the new name, and the notes (keeping the bank's original text).
    name: str | None = None
    notes: str | None = None

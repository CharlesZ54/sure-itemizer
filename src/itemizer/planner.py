from __future__ import annotations

from .models import Plan, Purchase, SureTxn
from .money import allocate

MAX_NAME = 120  # Sure shows names in one line; keep children readable


def child_name(name: str, quantity: int) -> str:
    name = name if len(name) <= MAX_NAME else name[: MAX_NAME - 1].rstrip() + "…"
    return f"{name} (x{quantity})" if quantity > 1 else name


def plan(txn: SureTxn, purchase: Purchase) -> Plan:
    if purchase.problem:
        return Plan(txn, purchase, "skip", purchase.problem)
    if not purchase.lines:
        return Plan(txn, purchase, "skip", "no line items")
    if purchase.amount_cents != txn.amount_cents:
        return Plan(txn, purchase, "skip", "amount changed since matching")

    # A single item is still "split" into one row. By the time we see a
    # charge, Sure's AI rule has usually already categorized and locked it
    # from the bare merchant name; a fresh, uncategorized row named after the
    # item is what lets the categorizer look again with real information.
    amounts = allocate(txn.amount_cents, [line.weight_cents for line in purchase.lines])
    children = [(child_name(line.name, line.quantity), cents) for line, cents in zip(purchase.lines, amounts)]
    if any(cents == 0 for _, cents in children):
        return Plan(txn, purchase, "skip", "an item would get a $0.00 share")
    return Plan(txn, purchase, "split", children=children)

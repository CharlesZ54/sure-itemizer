from __future__ import annotations

from .models import Plan, Purchase, SureTxn
from .money import allocate

MAX_NAME = 120  # Sure shows names in one line; keep children readable


def child_name(name: str, quantity: int) -> str:
    name = name if len(name) <= MAX_NAME else name[: MAX_NAME - 1].rstrip() + "…"
    return f"{name} (x{quantity})" if quantity > 1 else name


ORIGINAL_MARKER = "[itemizer] bank: "
MAX_DESCRIPTION = 160


def plan(txn: SureTxn, purchase: Purchase) -> Plan:
    if purchase.problem:
        return Plan(txn, purchase, "skip", purchase.problem)
    if purchase.amount_cents != txn.amount_cents:
        return Plan(txn, purchase, "skip", "amount changed since matching")
    if purchase.description is not None:
        return rename(txn, purchase)
    if not purchase.lines:
        return Plan(txn, purchase, "skip", "no line items")

    # A single item is still "split" into one row. By the time we see a
    # charge, Sure's AI rule has usually already categorized and locked it
    # from the bare merchant name; a fresh, uncategorized row named after the
    # item is what lets the categorizer look again with real information.
    if all(line.amount_cents is not None for line in purchase.lines):
        amounts = [line.amount_cents for line in purchase.lines]
        if sum(amounts) != txn.amount_cents:
            return Plan(txn, purchase, "skip", "the parts don't add up to the transaction")
    else:
        amounts = allocate(txn.amount_cents, [line.weight_cents for line in purchase.lines])
    children = [(child_name(line.name, line.quantity), cents) for line, cents in zip(purchase.lines, amounts)]
    if any(cents == 0 for _, cents in children):
        return Plan(txn, purchase, "skip", "an item would get a $0.00 share")
    return Plan(txn, purchase, "split", children=children)


def rename(txn: SureTxn, purchase: Purchase) -> Plan:
    """Give the transaction a readable name; the bank's text moves to the notes."""
    name = " ".join(purchase.description.split())
    if len(name) > MAX_DESCRIPTION:
        name = name[: MAX_DESCRIPTION - 1].rstrip() + "…"
    if not name:
        return Plan(txn, purchase, "skip", "nothing to describe")
    if txn.name == name:
        return Plan(txn, purchase, "skip", "already described")
    existing = txn.notes or ""
    notes = existing if ORIGINAL_MARKER in existing else f"{existing}\n{ORIGINAL_MARKER}{txn.name}".strip()
    return Plan(txn, purchase, "rename", name=name, notes=notes)

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def to_cents(value: float | str | Decimal) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def allocate(total_cents: int, weights: list[int]) -> list[int]:
    """Split total_cents across weights so the parts sum exactly to the total.

    Largest-remainder rounding: every part is within one cent of its exact
    proportional share, and no cent is created or lost. Zero/empty weights
    fall back to an even split.
    """
    if not weights:
        raise ValueError("allocate() needs at least one weight")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    if sum(weights) == 0:
        weights = [1] * len(weights)

    sign = -1 if total_cents < 0 else 1
    total = abs(total_cents)
    weight_sum = sum(weights)

    floors = [total * w // weight_sum for w in weights]
    remainders = [total * w % weight_sum for w in weights]
    leftover = total - sum(floors)
    # Hand the leftover cents to the largest remainders; ties go to the
    # earlier line so the result is deterministic.
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    return [sign * part for part in floors]

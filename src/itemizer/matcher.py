from __future__ import annotations

from .models import Purchase, SureTxn


def match(txns: list[SureTxn], purchases: list[Purchase],
          window: tuple[int, int]) -> list[tuple[SureTxn, Purchase]]:
    """Pair bank transactions with merchant charges, one-to-one.

    A pair needs the exact same signed amount and a bank date between
    `window[0]` days before and `window[1]` days after the merchant's date.
    Closest dates win, so two same-priced charges a week apart each find
    their own transaction.
    """
    before, after = window
    candidates = []
    for t in txns:
        for p in purchases:
            if t.amount_cents != p.amount_cents:
                continue
            delta = (t.date - p.date).days
            if -before <= delta <= after:
                candidates.append((abs(delta), t.date, p.date, t.id, p.external_id, t, p))

    candidates.sort(key=lambda c: c[:5])
    used_t: set[str] = set()
    used_p: set[str] = set()
    pairs = []
    for *_, t, p in candidates:
        if t.id in used_t or p.external_id in used_p:
            continue
        used_t.add(t.id)
        used_p.add(p.external_id)
        pairs.append((t, p))
    return pairs

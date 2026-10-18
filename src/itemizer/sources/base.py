"""The contract every merchant source implements.

A source answers two questions:

1. Which Sure transactions might be mine? (`claims`) -- cheap, local, and
   asked first, so a run with nothing new never logs in to the merchant.
2. What did the merchant charge, and for what? (`fetch`) -- returns one
   Purchase per card charge / bank pull, with its line items.

Matching, apportioning, planning and writing to Sure are shared, so a new
source (Venmo, another retailer, ...) only has to turn its merchant's data into
Purchases.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date

from ..models import Purchase, SureTxn


class Source(ABC):
    #: Short id used in config (SOURCES=amazon,venmo), state and logs.
    name: str
    #: Matched against the Sure transaction name to find candidates.
    descriptor: re.Pattern[str]
    #: Passed to Sure's `search` filter to narrow the transaction list.
    sure_search: str
    #: How far the merchant's date may sit before/after the bank's date.
    date_window: tuple[int, int] = (3, 5)

    def __init__(self, account_filter: str | None = None) -> None:
        # Sure account id, or a case-insensitive substring of its name.
        self.account_filter = account_filter

    def claims(self, txn: SureTxn) -> bool:
        if not self.descriptor.search(txn.name):
            return False
        if self.account_filter:
            f = self.account_filter.lower()
            return txn.account_id == self.account_filter or f in txn.account_name.lower()
        return True

    @abstractmethod
    def fetch(self, since: date, amounts: set[int] | None = None) -> list[Purchase]:
        """Return the merchant's charges on or after `since`.

        `amounts` (absolute cents) are the Sure transactions still waiting for
        a match. A source may use it to skip expensive per-order lookups, but
        must not rely on it for correctness.
        """

"""The contract every merchant source implements.

A source answers two questions:

1. Which Sure transactions might be mine? (`claims`) -- cheap, local, and
   asked first, so a run with nothing new never contacts the merchant.
2. What did the merchant charge, and for what? (`fetch`) -- returns one
   Purchase per card charge / bank pull, with its line items.

Matching, apportioning, planning and writing to Sure are shared, so a new
source (Venmo, another retailer, ...) only has to turn its merchant's data into
Purchases.

A source that needs the user to sign in also sets `login_fields` and
implements `login`/`logout`/`connected`. The web UI then offers a Connect
page for it, and scheduled runs never sign in on their own: they raise
`LoginRequired`, which notifies the user once.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..models import Purchase, SureTxn


class LoginRequired(Exception):
    """The merchant needs the user to sign in (again) through the web UI."""


class Source(ABC):
    #: Short id used in config (SOURCES=amazon,venmo), state and logs.
    name: str
    #: Human name for the web UI.
    title: str = ""
    #: Matched against the Sure transaction name to find candidates.
    descriptor: re.Pattern[str]
    #: Passed to Sure's `search` filter to narrow the transaction list.
    sure_search: str
    #: How far the merchant's date may sit before/after the bank's date.
    date_window: tuple[int, int] = (3, 5)
    #: Fields the Connect page asks for: (name, label, html input type).
    #: Empty means the source needs no interactive login.
    login_fields: list[tuple[str, str, str]] = []

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
        must not rely on it for correctness. Raises LoginRequired when the
        user has to sign in.
        """

    # Interactive login (only for sources with login_fields).

    def connected(self) -> bool:
        return True

    def login(self, fields: dict[str, str], io: Any) -> None:
        """Sign in with what the user typed; `io` relays any follow-up prompts."""
        raise NotImplementedError

    def logout(self) -> None:
        raise NotImplementedError

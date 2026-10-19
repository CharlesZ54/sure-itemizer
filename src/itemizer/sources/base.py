"""The contract every merchant source implements.

A source answers two questions:

1. Which Sure transactions might be mine? (`claims`) -- cheap, local, and
   asked first, so a run with nothing new never contacts the merchant.
2. What did the merchant charge, and for what? (`fetch`) -- returns one
   Purchase per card charge / bank pull, with its line items.

Matching, apportioning, planning and writing to Sure are shared, so a new
source (Venmo, another retailer, ...) only has to turn its merchant's data into
Purchases.

A source that needs the user to sign in implements `connected`/`logout` and
either `login_fields`+`login` (the service signs in, relaying questions) or
`live_url`+`live_save` (the user signs in on the merchant's page through a
live browser view). The web UI offers a Connect page for it, and scheduled
runs never sign in on their own: they raise `LoginRequired`, which notifies
the user once.
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
    #: Shown on the Connect page as "Setup guide": a guide to the fields.
    help_url: str | None = None
    #: Fields the Connect page asks for: (name, label, html input type), plus
    #: an optional fourth element True for a field that may be left empty.
    #: Empty means the source needs no interactive login.
    login_fields: list[tuple] = []
    #: Shown under the Connect form instead of the default "used once, then discarded".
    login_note: str | None = None

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
    def fetch(self, since: date, amounts: set[int] | None = None,
              txns: list[SureTxn] | None = None) -> list[Purchase]:
        """Return the merchant's charges on or after `since`.

        `amounts` (absolute cents) are the Sure transactions still waiting for
        a match, so a source can skip expensive lookups. `txns` are all of the
        source's Sure transactions in the period, finished ones included (the
        bank's side of things, e.g. which Venmo payments it paid for). Raises
        LoginRequired when the user has to sign in.
        """

    # Signing in -------------------------------------------------------
    #
    # Two ways, a source may offer either or both:
    # - login_fields + login(): the service signs in for the user and relays
    #   any questions (codes, captchas) to the web UI.
    # - live_url + live_save(): the user signs in themselves on the merchant's
    #   own page, through a live view of a browser in the container.

    #: Where the live browser opens for the user to sign in.
    live_url: str | None = None
    #: User agent for the live browser (None: a normal desktop Chrome).
    live_user_agent: str | None = None
    #: Script run in every page before the merchant's own (e.g. to hide passkeys).
    live_init_script: str | None = None

    def live_signed_in(self, context: Any) -> bool:
        """True once the browser is signed in, so the live view can finish on its own."""
        return False

    def live_save(self, context: Any) -> None:
        """Keep the signed-in browser's session for scheduled runs."""
        raise NotImplementedError

    def connected(self) -> bool:
        return True

    def login(self, fields: dict[str, str], io: Any) -> None:
        """Sign in with what the user typed; `io` relays any follow-up prompts."""
        raise NotImplementedError

    def logout(self) -> None:
        raise NotImplementedError

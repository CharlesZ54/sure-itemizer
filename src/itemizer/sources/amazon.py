"""Amazon: turn card charges into per-item Purchases.

Amazon charges once per *shipment*, not per order, so a three-shipment order
shows up as three card charges. The Transactions page gives us each charge
with its order number; the order page gives the items grouped by shipment.
`purchases_for_order` works out which shipments each charge paid for by
grossing shipment subtotals up by the order's tax/shipping/discount factor
and finding the one assignment that fits every charge.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..config import Config
from ..models import Line, Purchase
from ..money import to_cents
from .base import LoginRequired, Source

log = logging.getLogger(__name__)

MAX_GROUPS = 10
MAX_CHARGES = 6


@dataclass(frozen=True)
class ItemView:
    title: str
    unit_cents: int | None
    quantity: int


@dataclass(frozen=True)
class OrderView:
    number: str
    groups: list[list[ItemView]]  # items, grouped by shipment
    grand_total_cents: int | None


@dataclass(frozen=True)
class ChargeView:
    date: date
    amount_cents: int  # always positive; direction is is_refund
    is_refund: bool
    payment_method: str | None


def _tolerance(cents: int) -> int:
    # Tax is rounded per shipment and promos can land unevenly, so a grossed-up
    # subtotal is close to the charge but rarely exact.
    return max(10, cents * 2 // 100)


def _group_cents(group: list[ItemView]) -> int:
    return sum((i.unit_cents or 0) * i.quantity for i in group)


def _lines(groups: list[list[ItemView]]) -> list[Line]:
    return [
        Line(name=i.title, weight_cents=(i.unit_cents or 0) * i.quantity, quantity=i.quantity)
        for g in groups
        for i in g
    ]


def _assign(order: OrderView, charges: list[ChargeView]) -> list[list[int]] | None | str:
    """Map each charge to the shipment groups it paid for.

    Returns a list (per charge) of group indexes, None if nothing fits, or
    "ambiguous" if more than one different assignment fits.
    """
    groups = order.groups
    subtotal = sum(_group_cents(g) for g in groups)
    if subtotal <= 0:
        return None
    total = order.grand_total_cents or subtotal
    charged = sum(c.amount_cents for c in charges)
    # Groups may belong to charges we can't see (older than the lookback, or
    # on another card) only if the visible charges don't cover the order.
    allow_other = charged < total - _tolerance(total)

    solutions: list[list[list[int]]] = []
    assignment = [-1] * len(groups)

    def expected(idx: list[int]) -> int:
        return round(sum(_group_cents(groups[j]) for j in idx) * total / subtotal)

    def check() -> bool:
        per_charge: list[list[int]] = [[] for _ in charges]
        for j, ci in enumerate(assignment):
            if ci >= 0:
                per_charge[ci].append(j)
        for ci, idx in enumerate(per_charge):
            if not idx or abs(expected(idx) - charges[ci].amount_cents) > _tolerance(charges[ci].amount_cents):
                return False
        solutions.append(per_charge)
        return True

    def walk(j: int) -> bool:
        if j == len(groups):
            check()
            return len(solutions) > 1
        for ci in ([-1] if allow_other else []) + list(range(len(charges))):
            assignment[j] = ci
            if walk(j + 1):
                return True
        assignment[j] = -1
        return False

    walk(0)
    if not solutions:
        return None
    if len(solutions) > 1:
        # Two shipments with identical contents swap without changing what any
        # charge bought; that is not real ambiguity.
        def signature(sol: list[list[int]]) -> list[list[tuple]]:
            return [sorted((i.title, i.unit_cents, i.quantity) for j in idx for i in groups[j]) for idx in sol]

        if signature(solutions[0]) != signature(solutions[1]):
            return "ambiguous"
    return solutions[0]


def purchases_for_order(order: OrderView, charges: list[ChargeView]) -> list[Purchase]:
    out: list[Purchase] = []
    seen: dict[str, int] = defaultdict(int)

    def purchase(c: ChargeView, lines: list[Line], problem: str | None = None) -> Purchase:
        kind = "refund" if c.is_refund else "charge"
        key = f"{order.number}:{c.date.isoformat()}:{c.amount_cents}:{kind}"
        seen[key] += 1
        return Purchase(
            source="amazon",
            external_id=f"{key}:{seen[key]}",
            date=c.date,
            amount_cents=-c.amount_cents if c.is_refund else c.amount_cents,
            lines=lines,
            payment_method=c.payment_method,
            reference=order.number,
            problem=problem,
        )

    items = [i for g in order.groups for i in g]
    distinct = {(i.title, i.unit_cents) for i in items}

    # Refunds don't say which item came back. Only a single-product order is
    # unambiguous.
    for c in (c for c in charges if c.is_refund):
        if len(distinct) == 1:
            out.append(purchase(c, _lines([[items[0]]])))
        else:
            out.append(purchase(c, [], "refund on a multi-item order; Amazon doesn't say which item"))

    paid = [c for c in charges if not c.is_refund]
    if not paid:
        return out
    if not items:
        return out + [purchase(c, [], "order has no items Amazon would show") for c in paid]
    if len(items) > 1 and any(i.unit_cents is None for i in items):
        return out + [purchase(c, [], "some item prices are missing") for c in paid]
    if len(order.groups) > MAX_GROUPS or len(paid) > MAX_CHARGES:
        return out + [purchase(c, [], "order too large to match charges to shipments") for c in paid]

    if len(paid) == 1 and len(order.groups) == 1:
        return out + [purchase(paid[0], _lines(order.groups))]

    result = _assign(order, paid)
    if result is None and len(paid) == 1:
        # One visible charge but it doesn't line up with any set of shipments
        # (gift card or points covered part of it). If every item is in that
        # one charge's order, apportioning by price is still right.
        if order.grand_total_cents is None or paid[0].amount_cents <= order.grand_total_cents:
            return out + [purchase(paid[0], _lines(order.groups))]
    if result is None:
        return out + [purchase(c, [], "couldn't tell which shipment each charge paid for") for c in paid]
    if result == "ambiguous":
        return out + [purchase(c, [], "more than one way the charges fit the shipments") for c in paid]

    for c, idx in zip(paid, result):
        out.append(purchase(c, _lines([order.groups[j] for j in idx])))
    return out


def order_view(order) -> OrderView:
    """Adapt an amazonorders Order (full details) to OrderView."""

    def view(item) -> ItemView:
        return ItemView(
            title=" ".join(item.title.split()),
            unit_cents=to_cents(item.price) if item.price is not None else None,
            quantity=item.quantity or 1,
        )

    groups = [[view(i) for i in s.items] for s in (order.shipments or []) if s.items]
    if not groups:
        groups = [[view(i) for i in order.items]] if order.items else []
    total = to_cents(order.grand_total) if order.grand_total is not None else None
    return OrderView(number=order.order_number, groups=groups, grand_total_cents=total)


class AmazonSource(Source):
    name = "amazon"
    title = "Amazon"
    descriptor = re.compile(r"amazon|amzn", re.I)
    sure_search = "amazon"
    # The Transactions page date is when Amazon charged; the card usually
    # posts the same day or a few days later.
    date_window = (2, 5)
    login_fields = [("email", "Amazon email", "email"), ("password", "Amazon password", "password")]

    def __init__(self, data_dir: Path, card_last4: str | None = None,
                 account_filter: str | None = None, domain: str | None = None) -> None:
        super().__init__(account_filter)
        self.data_dir = Path(data_dir)
        self.card_last4 = card_last4
        self.domain = domain
        self._session = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "AmazonSource":
        return cls(data_dir=cfg.data_dir / "amazon", card_last4=cfg.amazon_card_last4,
                   account_filter=cfg.amazon_sure_account, domain=cfg.amazon_domain)

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "cookies.json"

    def _new_session(self, username: str | None = None, password: str | None = None, io=None):
        from amazonorders.conf import AmazonOrdersConfig
        from amazonorders.session import AmazonSession, IODefault

        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        config = AmazonOrdersConfig(
            config_path=str(self.data_dir / "config.yml"),
            data={
                "cookie_jar_path": str(self.cookie_path),
                "output_dir": str(self.data_dir / "output"),
                # Be a polite, low-volume client.
                "thread_pool_size": 2,
                "connection_pool_size": 4,
                # Amazon sometimes answers a sign-in with a JavaScript check
                # that plain HTTP can't pass; these hand just that step to
                # headless Chromium and carry its cookies back.
                "auth_forms_classes": [
                    "amazonorders.contrib.browser.playwright.PlaywrightAcicForm",
                    "amazonorders.contrib.browser.playwright.PlaywrightJSAuthForm",
                ],
                "browser": "chromium",
                **({"domain": self.domain} if self.domain else {}),
            },
        )
        return AmazonSession(username, password, io=io or IODefault(), config=config)

    # Interactive login --------------------------------------------------

    def connected(self) -> bool:
        return self.cookie_path.exists()

    def login(self, fields: dict[str, str], io) -> None:
        email, password = fields.get("email", "").strip(), fields.get("password", "")
        if not email or not password:
            raise ValueError("email and password are required")
        with self._lock:
            session = self._new_session(email, password, io)
            # Start from a clean jar so an old, half-expired session can't
            # confuse the sign-in flow.
            session.session.cookies.clear()
            session.login()
            if not session.is_authenticated:
                raise RuntimeError("Amazon didn't accept the sign-in")
            # Only the cookies Amazon issued are kept (written by the library
            # as it goes); the password is dropped here.
            session.password = None
            if self.cookie_path.exists():
                os.chmod(self.cookie_path, 0o600)
            self._session = session
        log.info("amazon: signed in; session saved")

    def logout(self) -> None:
        with self._lock:
            self._session = None
            self.cookie_path.unlink(missing_ok=True)

    def _authed_session(self):
        """The saved session, without ever signing in on our own."""
        if self._session is not None and self._session.is_authenticated:
            return self._session
        if not self.cookie_path.exists():
            raise LoginRequired("Amazon isn't connected yet")
        session = self._new_session()
        if not session.auth_cookies_stored():
            raise LoginRequired("the saved Amazon session is incomplete; sign in again")
        # Trust the saved cookies; if Amazon disagrees, the first request
        # redirects to sign-in and we stop there instead of retrying.
        session.is_authenticated = True
        self._session = session
        return session

    # Fetching -----------------------------------------------------------

    def _card_ok(self, payment_method: str | None) -> bool:
        if not self.card_last4:
            return True
        return bool(payment_method) and self.card_last4 in payment_method

    def fetch(self, since: date, amounts: set[int] | None = None) -> list[Purchase]:
        from amazonorders.exception import AmazonOrdersAuthError

        with self._lock:
            try:
                return self._fetch(self._authed_session(), since, amounts)
            except AmazonOrdersAuthError as e:
                self._session = None
                raise LoginRequired(f"Amazon wants you to sign in again ({e.__class__.__name__})") from e

    def _fetch(self, session, since: date, amounts: set[int] | None) -> list[Purchase]:
        from amazonorders.exception import AmazonOrdersAuthError
        from amazonorders.orders import AmazonOrders
        from amazonorders.transactions import AmazonTransactions

        days = (date.today() - since).days + 1
        raw = AmazonTransactions(session).get_transactions(days=days)
        log.info("amazon: %d transactions in the last %d days", len(raw), days)

        by_order: dict[str | None, list[ChargeView]] = defaultdict(list)
        for t in raw:
            if not self._card_ok(t.payment_method):
                continue
            by_order[t.order_number].append(ChargeView(
                date=t.completed_date,
                amount_cents=abs(to_cents(t.grand_total)),
                is_refund=bool(t.is_refund),
                payment_method=t.payment_method,
            ))

        orders = AmazonOrders(session)
        purchases: list[Purchase] = []
        for number, charges in by_order.items():
            # Only open orders that could explain a Sure transaction we still
            # need; every order page is another request to Amazon.
            if amounts is not None and not any(c.amount_cents in amounts for c in charges):
                continue
            if not number:
                for c in charges:
                    purchases.append(Purchase(source=self.name, external_id=f"no-order:{c.date}:{c.amount_cents}",
                                              date=c.date, amount_cents=c.amount_cents,
                                              payment_method=c.payment_method,
                                              problem="Amazon shows no order number for this charge"))
                continue
            try:
                view = order_view(orders.get_order(number))
            except AmazonOrdersAuthError:
                raise
            except Exception as e:  # one unreadable order shouldn't sink the run
                log.warning("amazon: couldn't read order %s: %s", number, e)
                for c in charges:
                    purchases.append(Purchase(source=self.name, external_id=f"{number}:{c.date}:{c.amount_cents}",
                                              date=c.date, amount_cents=-c.amount_cents if c.is_refund else c.amount_cents,
                                              reference=number, problem=f"couldn't read the order page: {e}"))
                continue
            purchases.extend(purchases_for_order(view, charges))
        return purchases

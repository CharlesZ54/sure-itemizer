"""Amazon: turn card charges into per-item Purchases.

Amazon charges once per *shipment*, not per order, so a three-shipment order
shows up as three card charges. The Transactions page gives us each charge
with its order number; the order page gives the items grouped by shipment.
`purchases_for_order` works out which shipments each charge paid for by
grossing shipment subtotals up by the order's tax/shipping/discount factor
and finding the one assignment that fits every charge.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

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


# Amazon sites offered on the Connect page. Only amazon.com is tested; the
# amazon-orders library reads the others in English where the site offers it.
DEFAULT_SITE = "amazon.com"
SITES = [
    ("amazon.com", "United States"), ("amazon.ca", "Canada"), ("amazon.com.mx", "Mexico"),
    ("amazon.com.br", "Brazil"), ("amazon.co.uk", "United Kingdom"), ("amazon.de", "Germany"),
    ("amazon.fr", "France"), ("amazon.it", "Italy"), ("amazon.es", "Spain"), ("amazon.nl", "Netherlands"),
    ("amazon.se", "Sweden"), ("amazon.pl", "Poland"), ("amazon.com.be", "Belgium"), ("amazon.ie", "Ireland"),
    ("amazon.com.tr", "Turkey"), ("amazon.ae", "United Arab Emirates"), ("amazon.sa", "Saudi Arabia"),
    ("amazon.in", "India"), ("amazon.co.jp", "Japan"), ("amazon.sg", "Singapore"),
    ("amazon.com.au", "Australia"),
]


class AmazonSource(Source):
    name = "amazon"
    title = "Amazon"
    descriptor = re.compile(r"amazon|amzn", re.I)
    sure_search = "amazon"
    # The Transactions page date is when Amazon charged; the card usually
    # posts the same day or a few days later.
    date_window = (2, 5)
    help_url = "https://github.com/CharlesZ54/sure-itemizer/blob/main/docs/amazon.md"
    login_fields = [("domain", "Amazon site", [(d, f"{name} ({d})") for d, name in SITES]),
                    ("email", "Amazon email", "email"), ("password", "Amazon password", "password")]

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.data_dir = Path(data_dir)
        self._session = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "AmazonSource":
        return cls(data_dir=cfg.data_dir / "amazon")

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "cookies.json"

    @property
    def site_path(self) -> Path:
        return self.data_dir / "site.json"

    @property
    def domain(self) -> str:
        """The Amazon site chosen on the Connect page (amazon.com until then)."""
        try:
            return json.loads(self.site_path.read_text())["domain"]
        except (OSError, ValueError, KeyError):
            return DEFAULT_SITE

    def _save_domain(self, domain: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.site_path.write_text(json.dumps({"domain": domain}))

    def login_defaults(self) -> dict[str, str]:
        return {"domain": self.domain}

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
                "domain": self.domain,
            },
        )
        return AmazonSession(username, password, io=io or IODefault(), config=config)

    # Interactive login --------------------------------------------------

    def connected(self) -> bool:
        return self.cookie_path.exists()

    def login(self, fields: dict[str, str], io) -> None:
        email, password = fields.get("email", "").strip(), fields.get("password", "")
        domain = (fields.get("domain") or self.domain).strip().lower()
        if not email or not password:
            raise ValueError("email and password are required")
        if domain not in dict(SITES):
            raise ValueError(f"unknown Amazon site: {domain}")
        from .amazon_browser import HeadlessSignIn

        with self._lock:
            # A failed reconnect must not leave a half-written session behind,
            # nor destroy one that still worked.
            previous = self.cookie_path.read_bytes() if self.cookie_path.exists() else None
            previous_domain = self.domain
            self._save_domain(domain)
            try:
                # The password goes to the browser only; the session object
                # never holds it.
                session = self._new_session(email, None, io)
                session.session.cookies.clear()
                HeadlessSignIn(session, io).sign_in(email, password)
                if not session.is_authenticated:
                    raise RuntimeError("Amazon didn't accept the sign-in")
            except BaseException:
                self._save_domain(previous_domain)
                if previous is None:
                    self.cookie_path.unlink(missing_ok=True)
                else:
                    self.cookie_path.write_bytes(previous)
                raise
            if self.cookie_path.exists():
                os.chmod(self.cookie_path, 0o600)
            self._session = session
        log.info("amazon: signed in; session saved")

    # Fallback: the user signs in themselves in the live browser view.

    def _constants(self):
        from amazonorders.conf import AmazonOrdersConfig

        data = {"domain": self.domain}
        return AmazonOrdersConfig(config_path=str(self.data_dir / "config.yml"), data=data).constants

    @property
    def live_url(self) -> str:
        c = self._constants()
        return f"{c.SIGN_IN_URL}?{urlencode(c.SIGN_IN_QUERY_PARAMS)}"

    @property
    def live_user_agent(self) -> str:
        # The session continues outside the browser with this identity.
        return self._constants().BASE_HEADERS.get("User-Agent")

    @property
    def live_init_script(self) -> str:
        from .amazon_browser import NO_PASSKEYS

        return NO_PASSKEYS

    def live_signed_in(self, context) -> bool:
        return any(c["name"] == "x-main" and "amazon." in c["domain"] for c in context.cookies())

    def live_save(self, context) -> None:
        from .amazon_browser import HeadlessSignIn

        with self._lock:
            session = self._new_session()
            handover = HeadlessSignIn(session, None)
            handover._harvest_cookies(context)
            handover._verify_session()  # persists the cookies once Amazon accepts them
            if self.cookie_path.exists():
                os.chmod(self.cookie_path, 0o600)
            self._session = session

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

    def fetch(self, since: date, amounts: set[int] | None = None, txns=None) -> list[Purchase]:
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

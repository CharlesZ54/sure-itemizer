"""eBay: split each order's card charge into the items bought.

Orders come from eBay's official API with the user's own developer keys (see
docs/ebay.md). The bank line carries the order number ("EBAY O*12-34567-89012"),
which the matcher prefers over amount and date alone.

Shipping and tax are spread over the items by price. A refund of the whole
order, or on a single-item order, is split the same way; a partial refund on a
multi-item order doesn't say which item it was for, so it is only renamed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..models import Line, Purchase
from ..money import to_cents
from .base import LoginRequired, Source
from .ebay_api import EbayAuthError, EbayClient, EbayError, code_from, find_all, text

log = logging.getLogger(__name__)

# Refunds can land weeks after the order, so look back further for orders.
REFUND_LEAD_DAYS = 45


@dataclass
class Item:
    title: str
    quantity: int
    unit_cents: int


@dataclass
class Refund:
    id: str
    when: datetime
    cents: int


@dataclass
class Order:
    id: str
    paid: datetime | None
    paid_cents: int
    items: list[Item] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)


def _when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_order(el: ET.Element) -> Order:
    items = []
    for t in find_all(el, "TransactionArray/Transaction"):
        qty = int(text(t, "QuantityPurchased", "1") or 1)
        items.append(Item(" ".join(text(t, "Item/Title").split()) or "eBay item", qty,
                          to_cents(text(t, "TransactionPrice", "0"))))
    refunds = []
    for i, r in enumerate(find_all(el, "MonetaryDetails/Refunds/Refund")):
        status = text(r, "RefundStatus").lower()
        when = _when(text(r, "RefundTime"))
        cents = to_cents(text(r, "RefundAmount", "0"))
        if when and cents and status in ("", "successful", "success", "completed"):
            refunds.append(Refund(text(r, "ReferenceID") or str(i), when, cents))
    paid = text(el, "AmountPaid") or text(el, "Total") or "0"
    return Order(id=text(el, "OrderID"), paid=_when(text(el, "PaidTime")), paid_cents=to_cents(paid),
                 items=items, refunds=refunds)


def _lines(order: Order) -> list[Line]:
    return [Line(i.title, i.unit_cents * i.quantity, i.quantity) for i in order.items]


def purchases_from(orders: list[Order]) -> list[Purchase]:
    """Each paid order's charge, and each refund. Signs follow Sure: positive = money out."""
    out: list[Purchase] = []
    for o in orders:
        if not o.id or not o.items:
            continue
        if o.paid and o.paid_cents > 0:
            out.append(Purchase(source="ebay", external_id=o.id, date=o.paid.date(), amount_cents=o.paid_cents,
                                lines=_lines(o), reference=o.id))
        for r in o.refunds:
            p = Purchase(source="ebay", external_id=f"{o.id}:refund:{r.id}", date=r.when.date(),
                         amount_cents=-r.cents, reference=o.id)
            if len(o.items) == 1 or r.cents == o.paid_cents:
                p.lines = _lines(o)
            else:
                titles = ", ".join(i.title for i in o.items)
                p.description = f"eBay refund: {titles}"
            out.append(p)
    return out


class EbaySource(Source):
    name = "ebay"
    title = "eBay"
    # "EBAY O*12-34567-89012" on a card; PayPal-funded orders show as PayPal instead.
    descriptor = re.compile(r"\bebay\b", re.I)
    sure_search = "ebay"
    help_url = "https://github.com/CharlesZ54/sure-itemizer/blob/main/docs/ebay.md"
    login_fields = [
        ("client_id", "App ID (Client ID)", "text", True),
        ("client_secret", "Cert ID (Client Secret)", "password", True),
        ("runame", "RuName (eBay Redirect URL name)", "text", True),
    ]
    login_note = ("These are kept, with the access eBay grants, in /data/ebay/auth.json (readable only by the "
                  "container), so scheduled runs can read your orders. Leave them blank to reuse the saved ones. "
                  "Disconnect deletes them.")

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "EbaySource":
        return cls(data_dir=cfg.data_dir / "ebay")

    @property
    def auth_path(self) -> Path:
        return self.data_dir / "auth.json"

    # Signing in ---------------------------------------------------------

    def connected(self) -> bool:
        return self.auth_path.exists()

    def _saved(self) -> dict:
        return json.loads(self.auth_path.read_text()) if self.auth_path.exists() else {}

    def _save(self, client: EbayClient) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        tmp = self.auth_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"client_id": client.client_id, "client_secret": client.client_secret,
                                   "runame": client.runame, "refresh_token": client.refresh_token,
                                   "refresh_expires_at": client.refresh_expires_at}))
        os.chmod(tmp, 0o600)
        tmp.replace(self.auth_path)

    def _client(self) -> EbayClient:
        saved = self._saved()
        if not saved.get("refresh_token"):
            raise LoginRequired("eBay isn't connected yet")
        return EbayClient(saved["client_id"], saved["client_secret"], saved["runame"], saved["refresh_token"])

    def login(self, fields: dict[str, str], io) -> None:
        saved = self._saved()
        # Blank fields keep what was saved, so reconnecting only needs the eBay sign-in.
        keys = {k: (fields.get(k) or "").strip() or saved.get(k, "") for k in ("client_id", "client_secret", "runame")}
        missing = [label for (k, label, *_) in self.login_fields if not keys[k]]
        if missing:
            raise ValueError(f"Enter your eBay developer keys first: {', '.join(missing)}")
        with self._lock:
            client = EbayClient(keys["client_id"], keys["client_secret"], keys["runame"])
            state = secrets.token_urlsafe(16)
            answer = io.prompt(
                "Open eBay's sign-in, sign in with the eBay account you buy with, and choose Agree. eBay then "
                "shows a page you can close: copy that page's whole address from the address bar and paste it here.",
                link=client.auth_url(state))
            client.exchange(code_from(answer, state))
            user = client.user_id()
            self._save(client)
        log.info("ebay: connected as %s", user or "your account")

    def logout(self) -> None:
        # Forgets the keys and the token; the user can also revoke the app's access
        # in eBay (Account settings → Permissions → Third-party app access).
        self.auth_path.unlink(missing_ok=True)

    # Fetching -----------------------------------------------------------

    def fetch(self, since: date, amounts: set[int] | None = None, txns=None) -> list[Purchase]:
        client = self._client()
        start = datetime.combine(since - timedelta(days=REFUND_LEAD_DAYS), time(), tzinfo=timezone.utc)
        try:
            orders = [parse_order(el) for el in client.orders(start)]
        except EbayAuthError as e:
            raise LoginRequired(str(e)) from e
        except EbayError as e:
            raise RuntimeError(f"ebay: {e}") from e
        log.info("ebay: %d orders since %s", len(orders), start.date())
        return [p for p in purchases_from(orders) if p.date >= since]

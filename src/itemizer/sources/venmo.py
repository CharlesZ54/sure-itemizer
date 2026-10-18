"""Venmo: give the bank's VENMO lines a real description.

Nothing is split. A payment you made from your bank becomes
"Venmo to Alex Smith: pizza"; a cash-out to your bank becomes
"Venmo cash-out: Alex Smith (dinner), Sam Lee (tickets)", listing the payments
received since the previous cash-out. Payments made from your Venmo balance
never reach the bank, so there's nothing to describe for them.

Data comes from Venmo's (unofficial) mobile API with a token from signing in
on the Connect page (password + texted code) or pasted there. Venmo's own
statement CSVs dropped into /data/venmo/statements/ are read too, and are the
more precise source for cash-outs (they record each transfer).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Config
from ..models import Purchase
from ..money import to_cents
from .base import LoginRequired, Source
from .venmo_api import VenmoAuthError, VenmoClient, VenmoError

log = logging.getLogger(__name__)

MAX_NAMED = 3  # cash-out descriptions name this many payments, then "+N more"


@dataclass(frozen=True)
class Row:
    id: str
    when: datetime
    type: str
    status: str
    note: str
    sender: str
    recipient: str
    cents: int  # signed as Venmo shows it: negative = left your Venmo
    fee_cents: int
    funding: str
    destination: str

    @property
    def counterparty(self) -> str:
        # Payments: the other side is the recipient when you paid, the sender when
        # you were paid. Charges (requests) are the other way round.
        paid_out = self.cents < 0
        if self.type.lower() == "charge":
            return self.sender if paid_out else self.recipient
        return self.recipient if paid_out else self.sender

    @property
    def is_payment(self) -> bool:
        return self.type.lower() in ("payment", "charge")

    @property
    def is_transfer(self) -> bool:
        return "transfer" in self.type.lower()

    @property
    def from_balance(self) -> bool:
        return "venmo balance" in self.funding.lower()


def _amount(text: str) -> int:
    """'- $38.09' / '+ $1,234.56' / '$0.00' / '' -> signed cents."""
    text = (text or "").strip()
    if not text:
        return 0
    negative = text.startswith("-")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return 0
    cents = to_cents(digits)
    return -cents if negative else cents


def parse_statement(text: str) -> list[Row]:
    """Rows from a Venmo statement CSV (preamble and balance lines skipped)."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header: dict[str, int] | None = None
    rows: list[Row] = []
    for raw in reader:
        cells = [c.strip() for c in raw]
        if header is None:
            lowered = [c.lower() for c in cells]
            if "id" in lowered and "datetime" in lowered:
                header = {name: i for i, name in enumerate(lowered)}
            continue

        def col(name: str) -> str:
            i = header.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        rid, when = col("id"), col("datetime")
        if not rid or not when:
            continue  # balance and summary lines
        try:
            stamp = datetime.fromisoformat(when.replace("Z", ""))
        except ValueError:
            continue
        rows.append(Row(
            id=rid, when=stamp, type=col("type"), status=col("status"),
            note=" ".join(col("note").split()), sender=col("from"), recipient=col("to"),
            cents=_amount(col("amount (total)")), fee_cents=abs(_amount(col("amount (fee)"))),
            funding=col("funding source"), destination=col("destination"),
        ))
    if header is None:
        raise ValueError("this doesn't look like a Venmo statement (no ID/Datetime header)")
    return rows


def _label(row: Row) -> str:
    who = row.counterparty or "someone"
    return f"{who} ({row.note})" if row.note else who


def purchases_from(rows: list[Row]) -> list[Purchase]:
    """What the bank saw, described. Signs follow Sure: positive = money left the bank."""
    rows = sorted((r for r in rows if r.status.lower() in ("complete", "completed", "issued", "")),
                  key=lambda r: r.when)
    out: list[Purchase] = []
    received: list[Row] = []  # payments into the balance since the last cash-out
    for r in rows:
        if r.is_payment and r.cents > 0:
            received.append(r)
            continue
        if r.is_payment and r.cents < 0 and not r.from_balance:
            who = r.counterparty or "someone"
            desc = f"Venmo to {who}: {r.note}" if r.note else f"Venmo to {who}"
            out.append(Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=-r.cents,
                                reference=r.id, description=desc))
            continue
        if r.is_transfer and r.cents < 0:
            # Cash-out to the bank; an instant transfer arrives minus its fee.
            net = abs(r.cents) - r.fee_cents
            named = [_label(p) for p in received]
            desc = "Venmo cash-out"
            if named:
                extra = f" +{len(named) - MAX_NAMED} more" if len(named) > MAX_NAMED else ""
                desc += ": " + ", ".join(named[:MAX_NAMED]) + extra
            out.append(Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=-net,
                                reference=r.id, description=desc))
            received = []
            continue
        if r.is_transfer and r.cents > 0:
            out.append(Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=r.cents,
                                reference=r.id, description="Venmo: added to balance"))
    return out


# Bank lines that are money coming back from Venmo.
CASH_OUT = re.compile(r"cash ?out|transfer from venmo", re.I)


def _name(user: dict | None) -> str:
    user = user or {}
    return (user.get("display_name") or " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            or user.get("username") or "someone")


def payments_from_stories(stories: list[dict], my_id: str) -> list[Row]:
    """Venmo API stories as statement-like rows (cents negative = left your Venmo)."""
    rows = []
    for story in stories:
        p = story.get("payment") or {}
        if not p or (p.get("status") or "").lower() not in ("settled", "complete", "completed"):
            continue
        actor = p.get("actor") or {}
        target = (p.get("target") or {}).get("user") or {}
        action = (p.get("action") or "").lower()
        i_acted = str(actor.get("id")) == str(my_id)
        # pay: the actor paid the target. charge: the actor requested, the target paid.
        paid_out = i_acted if action == "pay" else not i_acted
        other = target if i_acted else actor
        cents = to_cents(p.get("amount") or 0)
        when = (p.get("date_completed") or story.get("date_created") or "").replace("Z", "")
        try:
            stamp = datetime.fromisoformat(when)
        except ValueError:
            continue
        rows.append(Row(id=str(p.get("id") or story.get("id")), when=stamp, type="Payment", status="Complete",
                        note=" ".join((p.get("note") or "").split()), sender="" if paid_out else _name(other),
                        recipient=_name(other) if paid_out else "", cents=-cents if paid_out else cents,
                        fee_cents=0, funding="", destination=""))
    return rows


def describe_cash_outs(txns: list, received: list[Row], since: date) -> list[Purchase]:
    """Describe the bank's Venmo cash-outs with the payments received before each one.

    The API doesn't list transfers, so each cash-out is described from what
    came in after the previous cash-out (or the lookback start) up to its date.
    """
    outs = sorted((t for t in txns if t.amount_cents < 0 and CASH_OUT.search(t.name)), key=lambda t: t.date)
    received = sorted(received, key=lambda r: r.when)
    purchases, start = [], since
    for t in outs:
        window = [r for r in received if start <= r.when.date() <= t.date]
        named = [_label(r) for r in window]
        desc = "Venmo cash-out"
        if named:
            extra = f" +{len(named) - MAX_NAMED} more" if len(named) > MAX_NAMED else ""
            desc += ": " + ", ".join(named[:MAX_NAMED]) + extra
        purchases.append(Purchase(source="venmo", external_id=f"cash-out:{t.id}", date=t.date,
                                  amount_cents=t.amount_cents, description=desc))
        start = t.date + timedelta(days=1)
    return purchases


class VenmoSource(Source):
    name = "venmo"
    title = "Venmo"
    descriptor = re.compile(r"venmo", re.I)
    sure_search = "venmo"
    # Bank pulls for Venmo payments post the same day or a few after.
    date_window = (1, 5)
    login_fields = [
        ("username", "Venmo email or phone", "text", True),
        ("password", "Venmo password", "password", True),
        ("token", "…or paste a Venmo access token instead", "password", True),
    ]

    def __init__(self, data_dir: Path, account_filter: str | None = None) -> None:
        super().__init__(account_filter)
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "VenmoSource":
        return cls(data_dir=cfg.data_dir / "venmo", account_filter=cfg.source_setting("VENMO", "SURE_ACCOUNT"))

    @property
    def auth_path(self) -> Path:
        return self.data_dir / "auth.json"

    @property
    def statements_dir(self) -> Path:
        return self.data_dir / "statements"

    # Signing in ---------------------------------------------------------

    def connected(self) -> bool:
        return self.auth_path.exists()

    def _save(self, client: VenmoClient) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        tmp = self.auth_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": client.token, "device_id": client.device_id}))
        os.chmod(tmp, 0o600)
        tmp.replace(self.auth_path)

    def _client(self) -> VenmoClient:
        if not self.auth_path.exists():
            raise LoginRequired("Venmo isn't connected yet")
        saved = json.loads(self.auth_path.read_text())
        return VenmoClient(token=saved.get("token"), device_id=saved.get("device_id"))

    def login(self, fields: dict[str, str], io) -> None:
        token = (fields.get("token") or "").strip()
        username, password = (fields.get("username") or "").strip(), fields.get("password") or ""
        with self._lock:
            # Reuse this install's device id, so a trusted device stays trusted.
            device = None
            if self.auth_path.exists():
                device = json.loads(self.auth_path.read_text()).get("device_id")
            client = VenmoClient(device_id=device)
            if token:
                client.set_token(token)
            elif username and password:
                secret = client.start_login(username, password)
                if secret:
                    client.send_code(secret)
                    code = io.prompt("Enter the code Venmo texted you")
                    client.finish_login(re.sub(r"\s", "", code), secret)
                    client.trust_device()
            else:
                raise ValueError("enter your Venmo email/phone and password, or paste an access token")
            me = client.me()  # proves the token works before anything is saved
            self._save(client)
        log.info("venmo: connected as %s", me.get("username") or "your account")

    def logout(self) -> None:
        # Only forgets the token here; revoking it is up to the user (Venmo's
        # security settings), so a token shared with another tool keeps working.
        self.auth_path.unlink(missing_ok=True)

    # Fetching -----------------------------------------------------------

    def _saved_statements(self) -> list[Row]:
        rows: list[Row] = []
        if self.statements_dir.exists():
            for f in sorted(self.statements_dir.glob("*.csv")):
                try:
                    rows += parse_statement(f.read_text(encoding="utf-8-sig"))
                except ValueError as e:
                    log.warning("venmo: skipping %s: %s", f.name, e)
        return rows

    def _api_rows(self, since: date) -> list[Row]:
        client = self._client()
        try:
            me = client.me()
            stories, before = [], None
            for _ in range(20):  # 50 a page; far more than a lookback needs
                page = client.stories(me["id"], before_id=before)
                if not page:
                    break
                stories += page
                before = page[-1].get("id")
                oldest = (page[-1].get("date_created") or "")[:10]
                if oldest and oldest < since.isoformat():
                    break
        except VenmoAuthError as e:
            raise LoginRequired(str(e)) from e
        except VenmoError as e:
            raise RuntimeError(f"venmo: {e}") from e
        return payments_from_stories(stories, me["id"])

    def fetch(self, since: date, amounts: set[int] | None = None, txns=None) -> list[Purchase]:
        statement = self._saved_statements()
        api = self._api_rows(since) if self.connected() or not statement else []
        purchases = {p.external_id: p for p in purchases_from(statement)}
        # Statement CSVs record cash-outs exactly; without them, describe the
        # bank's cash-outs from the payments received before each.
        if not any(r.is_transfer for r in statement):
            received = [r for r in api if r.cents > 0]
            for p in describe_cash_outs(txns or [], received, since):
                purchases[p.external_id] = p
        for p in purchases_from([r for r in api if r.cents < 0]):
            purchases.setdefault(p.external_id, p)
        return [p for p in purchases.values() if p.date >= since]

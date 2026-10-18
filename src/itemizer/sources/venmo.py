"""Venmo: give the bank's VENMO lines a real description.

A payment you made from your bank is renamed "Venmo to Alex Smith: pizza".
A cash-out to your bank is split into what it's made of: the payments received
since the previous cash-out ("Venmo from Sam Lee: dinner"), less the payments
made from the Venmo balance in that time ("Venmo to Alex Smith: pizza"). Any
difference (money left in, or taken from, the balance) gets a line of its own,
so the parts always add up to the cash-out.

Data comes from Venmo's (unofficial) mobile API with a token from signing in
on the Connect page (password + texted code) or pasted there. Venmo's own
statement CSVs dropped into /data/venmo/statements/ are read too.
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
from ..models import Line, Purchase
from ..money import to_cents
from .base import LoginRequired, Source
from .venmo_api import VenmoAuthError, VenmoClient, VenmoError

log = logging.getLogger(__name__)

LEAD_DAYS = 31  # look back this much further for payments behind the first cash-out


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
    """'- $12.34' / '+ $1,234.56' / '$0.00' / '' -> signed cents."""
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


def _line(row: Row) -> Line:
    """A cash-out's part: money in is negative, as in Sure."""
    who = row.counterparty or "someone"
    name = f"Venmo {'to' if row.cents < 0 else 'from'} {who}" + (f": {row.note}" if row.note else "")
    return Line(name, abs(row.cents), amount_cents=-row.cents)


def purchases_from(rows: list[Row], bank_paid=None) -> list[Purchase]:
    """What the bank saw. Signs follow Sure: positive = money left the bank.

    `bank_paid(row)` tells whether an outgoing payment was pulled from the
    bank, for rows (the API's) that don't say how they were funded.
    """
    rows = sorted((r for r in rows if r.status.lower() in ("complete", "completed", "issued", "")),
                  key=lambda r: r.when)
    out: list[Purchase] = []
    parts: list[Row] = []  # payments in, and out of the balance, since the last cash-out
    for r in rows:
        if r.is_payment and r.cents > 0:
            parts.append(r)
            continue
        if r.is_payment and r.cents < 0:
            from_balance = r.from_balance if r.funding or not bank_paid else not bank_paid(r)
            if from_balance:
                parts.append(r)
                continue
            who = r.counterparty or "someone"
            desc = f"Venmo to {who}: {r.note}" if r.note else f"Venmo to {who}"
            out.append(Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=-r.cents,
                                reference=r.id, description=desc))
            continue
        if r.is_transfer and r.cents < 0:
            # Cash-out to the bank; an instant transfer arrives minus its fee.
            net = abs(r.cents) - r.fee_cents
            p = Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=-net, reference=r.id)
            if parts:
                p.lines = [_line(x) for x in parts]
                if r.fee_cents:
                    p.lines.append(Line("Venmo instant transfer fee", r.fee_cents, amount_cents=r.fee_cents))
                rest = -net - sum(line.amount_cents for line in p.lines)
                if rest:
                    name = "Venmo balance from before" if rest < 0 else "Left in the Venmo balance"
                    p.lines.append(Line(name, abs(rest), amount_cents=rest))
            else:
                p.description = "Venmo cash-out"
            out.append(p)
            parts = []
            continue
        if r.is_transfer and r.cents > 0:
            out.append(Purchase(source="venmo", external_id=r.id, date=r.when.date(), amount_cents=r.cents,
                                reference=r.id, description="Venmo: added to balance"))
    return out


def _name(user: dict | None) -> str:
    user = user or {}
    return (user.get("display_name") or " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            or user.get("username") or "someone")


def _stamp(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", ""))
    except ValueError:
        return None


def rows_from_stories(stories: list[dict], my_id: str) -> list[Row]:
    """Venmo API stories (payments and transfers) as statement-like rows.

    Cents are negative when money left your Venmo balance, as in a statement.
    """
    rows = []
    for story in stories:
        t = story.get("transfer") or {}
        if t:
            # Cash-outs to a bank or card; money added to Venmo isn't a transfer story.
            stamp = _stamp(t.get("date_requested") or story.get("date_created"))
            if stamp and (t.get("status") or "").lower() in ("issued", "complete", "completed"):
                cents = t.get("amount_cents") or to_cents(t.get("amount") or 0)
                rows.append(Row(id=str(t.get("id") or story.get("id")), when=stamp, type="Standard Transfer",
                                status="Issued", note="", sender="", recipient="", cents=-cents,
                                fee_cents=t.get("amount_fee_cents") or 0, funding="",
                                destination=(t.get("destination") or {}).get("name") or ""))
            continue
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
        stamp = _stamp(p.get("date_completed") or story.get("date_created"))
        if not stamp:
            continue
        rows.append(Row(id=str(p.get("id") or story.get("id")), when=stamp, type="Payment", status="Complete",
                        note=" ".join((p.get("note") or "").split()), sender="" if paid_out else _name(other),
                        recipient=_name(other) if paid_out else "", cents=-cents if paid_out else cents,
                        fee_cents=0, funding="", destination=""))
    return rows


class VenmoSource(Source):
    name = "venmo"
    title = "Venmo"
    descriptor = re.compile(r"venmo", re.I)
    sure_search = "venmo"
    # Bank pulls for Venmo payments post the same day or a few after.
    date_window = (1, 5)
    help_url = "https://github.com/CharlesZ54/sure-itemizer/blob/main/docs/venmo.md"
    login_fields = [
        ("username", "Venmo email or phone", "text", True),
        ("password", "Venmo password", "password", True),
        # Venmo refuses (and counts against you) sign-ins from devices it doesn't
        # know: github.com/mmohades/Venmo/issues/86. A browser's v_id cookie is one.
        ("device_id", "Trusted device ID: the v_id cookie from a browser signed in to Venmo", "text", True),
        ("token", "…or instead, paste a Venmo access token", "password", True),
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
        trusted = (fields.get("device_id") or "").strip()
        with self._lock:
            device = trusted or None
            if not device and self.auth_path.exists():
                device = json.loads(self.auth_path.read_text()).get("device_id")
            client = VenmoClient(device_id=device)
            if token:
                client.set_token(token)
            elif username and password:
                if not device:
                    # A made-up device id gets "Unable to complete your request",
                    # and repeated tries can lock the account. Don't try.
                    raise ValueError("Venmo only accepts sign-ins from a device it already trusts. Enter the "
                                     "v_id cookie of a browser you've signed in to Venmo with (or paste a token).")
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
        return rows_from_stories(stories, me["id"])

    def _bank_paid(self, txns: list):
        """Whether the bank shows a pull for an outgoing payment (else the balance paid it)."""
        before, after = self.date_window
        pulls = [t for t in txns if t.amount_cents > 0]

        def paid(row: Row) -> bool:
            day = row.when.date()
            return any(t.amount_cents == -row.cents and day - timedelta(days=before) <= t.date
                       <= day + timedelta(days=after) for t in pulls)
        return paid

    def fetch(self, since: date, amounts: set[int] | None = None, txns=None) -> list[Purchase]:
        # Read further back than asked, so the first cash-out in range still
        # knows the payments that came in before it.
        earlier = since - timedelta(days=LEAD_DAYS)
        statement = self._saved_statements()
        api = self._api_rows(earlier) if self.connected() or not statement else []
        if any(r.is_transfer for r in statement):
            # The statement has the cash-outs already, under its own ids.
            api = [r for r in api if not r.is_transfer]
        purchases = {p.external_id: p for p in purchases_from(statement)}
        for p in purchases_from(api, bank_paid=self._bank_paid(txns or [])):
            purchases.setdefault(p.external_id, p)
        return [p for p in purchases.values() if p.date >= since]

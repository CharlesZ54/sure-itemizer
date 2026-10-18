"""Venmo: give the bank's VENMO lines a real description.

Nothing is split. A payment you made from your bank becomes
"Venmo to Alex Smith: pizza"; a cash-out to your bank becomes
"Venmo cash-out: Alex Smith (dinner), Sam Lee (tickets)", listing the payments
it carried. Payments made from your Venmo balance never reach the bank, so
there's nothing to describe for them.

The data is Venmo's own statement CSV. Signing in happens in the live browser
view only (no scripted sign-in: failed automated attempts are what get a
payments account restricted). Statements can also be dropped into
/data/venmo/statements/ by hand.
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
from datetime import date, datetime
from pathlib import Path

from ..config import Config
from ..models import Purchase
from ..money import to_cents
from .base import LoginRequired, Source

log = logging.getLogger(__name__)

HOME = "https://account.venmo.com/"
STATEMENT_PAGE = "https://account.venmo.com/statement"
# Pages that mean "not signed in yet", whatever the host.
NOT_SIGNED_IN = re.compile(r"sign|login|verify|challenge|mfa|auth|two-factor|otp", re.I)
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


class VenmoSource(Source):
    name = "venmo"
    title = "Venmo"
    descriptor = re.compile(r"venmo", re.I)
    sure_search = "venmo"
    # Bank pulls for Venmo payments post the same day or a few after; cash-outs
    # take 1-3 business days.
    date_window = (1, 5)
    live_url = HOME

    def __init__(self, data_dir: Path, account_filter: str | None = None) -> None:
        super().__init__(account_filter)
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "VenmoSource":
        return cls(data_dir=cfg.data_dir / "venmo", account_filter=cfg.source_setting("VENMO", "SURE_ACCOUNT"))

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def statements_dir(self) -> Path:
        return self.data_dir / "statements"

    # Signing in (live view only) ----------------------------------------

    def connected(self) -> bool:
        return self.state_path.exists()

    def live_signed_in(self, context) -> bool:
        for page in context.pages:
            if page.is_closed():
                continue
            url = page.url
            if url.startswith(HOME) and not NOT_SIGNED_IN.search(url.split("?")[0]):
                return True
        return False

    def live_save(self, context) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        context.storage_state(path=str(self.state_path))
        os.chmod(self.state_path, 0o600)

    def logout(self) -> None:
        self.state_path.unlink(missing_ok=True)

    # Fetching -----------------------------------------------------------

    def _browser(self, pw):
        from ..livebrowser import CHROME_UA, VIEWPORT

        browser = pw.chromium.launch(headless=True, channel="chromium",
                                     args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(self.state_path), locale="en-US", viewport=VIEWPORT,
                                      user_agent=CHROME_UA.format(major=browser.version.split(".")[0]))
        return browser, context

    def probe(self) -> Path:
        """Record what Venmo's statements page requests, to find its statement download."""
        from playwright.sync_api import sync_playwright

        if not self.connected():
            raise LoginRequired("Venmo isn't connected yet")
        out = self.data_dir / "probe"
        out.mkdir(parents=True, exist_ok=True)
        os.chmod(out, 0o700)
        seen: list[dict] = []

        def keep(response) -> None:
            url = response.url
            if "venmo.com" not in url:
                return
            ctype = response.headers.get("content-type") or ""
            entry = {"status": response.status, "method": response.request.method, "url": url, "type": ctype}
            if "json" in ctype or "csv" in ctype:
                try:
                    body = response.text()
                    path = out / f"{len(seen):03d}.txt"
                    path.write_text(body)
                    os.chmod(path, 0o600)
                    entry["saved"] = path.name
                except Exception:
                    pass
            seen.append(entry)

        with self._lock, sync_playwright() as pw:
            browser, context = self._browser(pw)
            try:
                page = context.new_page()
                page.on("response", keep)
                page.goto(STATEMENT_PAGE, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                if NOT_SIGNED_IN.search(page.url.split("?")[0]):
                    raise LoginRequired("Venmo wants you to sign in again")
                button = page.get_by_role("button", name=re.compile("download csv", re.I))
                if button.count():
                    try:
                        with page.expect_download(timeout=20000) as dl:
                            button.first.click()
                        dl.value.save_as(str(out / "download.csv"))
                        os.chmod(out / "download.csv", 0o600)
                    except Exception as e:
                        seen.append({"download_failed": str(e)[:200]})
                    page.wait_for_timeout(3000)
                context.storage_state(path=str(self.state_path))  # keep refreshed cookies
            finally:
                browser.close()
        index = out / "index.json"
        index.write_text(json.dumps(seen, indent=1))
        os.chmod(index, 0o600)
        return out

    def _saved_statements(self) -> list[Row]:
        rows: list[Row] = []
        if self.statements_dir.exists():
            for f in sorted(self.statements_dir.glob("*.csv")):
                try:
                    rows += parse_statement(f.read_text(encoding="utf-8-sig"))
                except ValueError as e:
                    log.warning("venmo: skipping %s: %s", f.name, e)
        return rows

    def fetch(self, since: date, amounts: set[int] | None = None) -> list[Purchase]:
        rows = self._saved_statements()
        if not self.connected() and not rows:
            raise LoginRequired("Venmo isn't connected yet")
        # Downloading the statement from venmo.com comes once the probe shows
        # how the statements page fetches it.
        unique = {r.id: r for r in rows}.values()
        return [p for p in purchases_from(list(unique)) if p.date >= since]

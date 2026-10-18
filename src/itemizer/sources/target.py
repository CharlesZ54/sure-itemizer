"""Target: purchases made with a Target Circle Card (debit or credit), which
Target links to the account, in-store receipts included.

Signing in happens in the live browser view (Target's sign-in varies between
password and one-time codes, and sits behind bot protection). The browser's
session is saved as Playwright storage state, and later runs open Target's
own order pages headlessly with it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date
from pathlib import Path

from ..config import Config
from ..models import Purchase
from .base import LoginRequired, Source

log = logging.getLogger(__name__)

ORDERS_URL = "https://www.target.com/orders"
# Target's own API calls that aren't about orders (analytics, ads, location).
NOISE = re.compile(r"firefly_events|telemetry|privacy_rule|sapphire|location_fulfillment|ads|recommend", re.I)


class TargetSource(Source):
    name = "target"
    title = "Target"
    descriptor = re.compile(r"\btarget\b", re.I)
    sure_search = "target"
    # Target Circle debit purchases reach the bank as an ACH pull a day or
    # few after the purchase.
    date_window = (1, 6)
    live_url = "https://www.target.com/account"

    def __init__(self, data_dir: Path, account_filter: str | None = None) -> None:
        super().__init__(account_filter)
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "TargetSource":
        return cls(data_dir=cfg.data_dir / "target", account_filter=cfg.source_setting("TARGET", "SURE_ACCOUNT"))

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    # Signing in ---------------------------------------------------------

    def connected(self) -> bool:
        return self.state_path.exists()

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

        if not self.state_path.exists():
            raise LoginRequired("Target isn't connected yet")
        browser = pw.chromium.launch(headless=True, channel="chromium",
                                     args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(self.state_path), locale="en-US", viewport=VIEWPORT,
                                      user_agent=CHROME_UA.format(major=browser.version.split(".")[0]))
        return browser, context

    def probe(self) -> Path:
        """Record Target's own order API responses, to learn their shape."""
        from playwright.sync_api import sync_playwright

        out = self.data_dir / "probe"
        out.mkdir(parents=True, exist_ok=True)
        os.chmod(out, 0o700)
        seen: list[dict] = []

        def keep(response) -> None:
            url = response.url
            if "target.com" not in url or NOISE.search(url):
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            try:
                body = response.json()
            except Exception:
                return
            n = len(seen)
            (out / f"{n:03d}.json").write_text(json.dumps({"url": url, "status": response.status, "body": body}))
            seen.append({"n": n, "status": response.status, "url": url.split("?")[0]})

        with self._lock, sync_playwright() as pw:
            browser, context = self._browser(pw)
            try:
                page = context.new_page()
                page.on("response", keep)
                page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                if "login" in page.url or "signin" in page.url:
                    raise LoginRequired("Target wants you to sign in again")
                links = page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href*="/orders/"]')]
                                          .map(a => a.href))]""")
                tabs = page.locator("a, button").filter(has_text=re.compile(r"in.?store", re.I))
                if tabs.count():
                    tabs.first.click()
                    page.wait_for_timeout(5000)
                    links += page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href*="/orders/"]')]
                                               .map(a => a.href))]""")
                for link in [l for l in dict.fromkeys(links) if l.rstrip("/") != ORDERS_URL][:4]:
                    page.goto(link, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(5000)
                context.storage_state(path=str(self.state_path))  # keep refreshed tokens
            finally:
                browser.close()
        for f in out.iterdir():
            os.chmod(f, 0o600)
        (out / "index.json").write_text(json.dumps(seen, indent=1))
        return out

    def fetch(self, since: date, amounts: set[int] | None = None) -> list[Purchase]:
        if not self.connected():
            raise LoginRequired("Target isn't connected yet")
        # Receipt parsing comes once the probe shows Target's data shape.
        log.info("target: receipt reading isn't built yet; nothing to match")
        return []

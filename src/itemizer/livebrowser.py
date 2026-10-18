"""A live view of a headless browser, so the user can sign in to a merchant
on the merchant's own page from the web UI.

The browser runs in the container. The web UI shows its screen as a stream of
screenshots and forwards clicks, scrolling and typing to it. Nothing typed is
stored; when the user is signed in, the source saves the browser's session
(cookies) and the browser is closed. Works for any merchant, whatever its
sign-in page looks like.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1100, "height": 820}
IDLE_TIMEOUT = 600  # seconds without input before the browser is closed
FRAME_EVERY = 0.35  # seconds between screenshots while someone is watching
CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
SPECIAL_KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp",
                "ArrowDown", "Home", "End", "PageUp", "PageDown"}


class LiveBrowser:
    """One live browser per source, driven from a single thread (Playwright's sync API requires it)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"  # idle | starting | running | saving | done | failed
        self.error: str | None = None
        self.url = ""
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self._watched_at = 0.0
        self._cmds: queue.Queue = queue.Queue()

    # Called from web handlers -------------------------------------------

    def start(self, url: str, signed_in: Callable[[Any], bool], save: Callable[[Any], None],
              user_agent: str | None = None, init_script: str | None = None,
              on_success: Callable[[], None] | None = None) -> None:
        with self.lock:
            if self.state in ("starting", "running", "saving"):
                return  # already open; the page just reconnects to it
            self.state, self.error, self.frame, self.url = "starting", None, None, ""
            self._cmds = queue.Queue()
        threading.Thread(target=self._run, args=(url, signed_in, save, user_agent, init_script, on_success),
                         daemon=True).start()

    def send(self, cmd: dict) -> None:
        with self.lock:
            if self.state != "running":
                raise RuntimeError("the browser isn't open")
        self._cmds.put(cmd)

    def snapshot(self) -> dict:
        with self.lock:
            self._watched_at = time.monotonic()
            return {"state": self.state, "error": self.error, "url": self.url, "frame_at": self.frame_at}

    def latest_frame(self) -> bytes | None:
        with self.lock:
            self._watched_at = time.monotonic()
            return self.frame

    # Browser thread ------------------------------------------------------

    def _set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _run(self, url, signed_in, save, user_agent, init_script, on_success) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, channel="chromium",
                                             args=["--disable-blink-features=AutomationControlled"])
                try:
                    ua = user_agent or CHROME_UA.format(major=browser.version.split(".")[0])
                    context = browser.new_context(user_agent=ua, locale="en-US", viewport=VIEWPORT)
                    if init_script:
                        context.add_init_script(init_script)
                    pages = [context.new_page()]
                    # Sign-in flows sometimes open a popup; follow the newest page.
                    context.on("page", lambda p: pages.append(p))
                    pages[-1].goto(url, wait_until="domcontentloaded", timeout=45000)
                    self._set(state="running")
                    signed = self._loop(context, pages, signed_in)
                    if signed:
                        self._set(state="saving")
                        save(context)
                finally:
                    browser.close()
            if not signed:
                self._set(state="failed", error=self.error or "closed")
                return
            if on_success:
                on_success()
            # Only now: whoever watches for "done" can rely on everything above.
            self._set(state="done")
            log.info("live sign-in finished; session saved")
        except Exception as e:
            log.warning("live browser failed: %s", e)
            self._set(state="failed", error=str(e).splitlines()[0][:300])

    def _loop(self, context, pages, signed_in) -> bool:
        last_input = time.monotonic()
        while True:
            page = next((p for p in reversed(pages) if not p.is_closed()), None)
            if page is None:
                self._set(error="the page was closed")
                return False
            try:
                cmd = self._cmds.get(timeout=0.1)
            except queue.Empty:
                cmd = None
            if cmd:
                last_input = time.monotonic()
                kind = cmd.get("type")
                if kind == "cancel":
                    self._set(error="cancelled")
                    return False
                if kind == "done":
                    return True
                try:
                    self._apply(page, cmd)
                except Exception as e:  # a stray click shouldn't end the session
                    log.debug("input failed: %s", e)
            if signed_in(context):
                return True
            if time.monotonic() - last_input > IDLE_TIMEOUT:
                self._set(error="closed after 10 minutes without input")
                return False
            watched = time.monotonic() - self._watched_at < 5
            if watched and (cmd or time.monotonic() - self.frame_at > FRAME_EVERY):
                try:
                    shot = page.screenshot(type="jpeg", quality=60, timeout=5000)
                    self._set(frame=shot, frame_at=time.monotonic(), url=page.url)
                except Exception:
                    pass  # mid-navigation; next round

    @staticmethod
    def _apply(page, cmd: dict) -> None:
        kind = cmd.get("type")
        if kind == "click":
            x = float(cmd["x"]) * VIEWPORT["width"]
            y = float(cmd["y"]) * VIEWPORT["height"]
            page.mouse.click(x, y)
        elif kind == "scroll":
            page.mouse.wheel(0, max(-2000, min(2000, float(cmd.get("dy", 0)))))
        elif kind == "text":
            page.keyboard.type(str(cmd.get("text", ""))[:500], delay=20)
        elif kind == "key" and cmd.get("key") in SPECIAL_KEYS:
            page.keyboard.press(cmd["key"])
        elif kind == "back":
            page.go_back(wait_until="domcontentloaded")

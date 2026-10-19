"""Interactive merchant logins, driven from the web UI.

A merchant's login code runs in a background thread and talks to the user
through `WebIO`: whenever it needs input (a 2FA code, a choice of where to
send it, a captcha) it parks the question here and waits for the browser to
answer. Credentials pass through memory only; nothing typed here is stored.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

PROMPT_TIMEOUT = 300  # seconds to wait for the user before abandoning a login


class LoginCancelled(Exception):
    pass


class WebIO:
    """Stands in for a console: echo() collects messages, prompt() asks the browser."""

    def __init__(self, flow: "LoginFlow") -> None:
        self.flow = flow

    def echo(self, msg: str, **kwargs: Any) -> None:
        if msg and msg.strip():
            self.flow._message(msg.strip())

    def prompt(self, msg: str, type: Any = None, **kwargs: Any) -> Any:
        answer = self.flow._ask(msg, kwargs.get("choices") or [], kwargs.get("img_url"), kwargs.get("link"))
        return type(answer) if type else answer

    def handoff(self, context: Any, pages: list, reason: str, until: Callable[[Any], bool]) -> None:
        """Let the user take over the browser (live view) until `until` holds or they press Continue."""
        self.flow._handoff(context, pages, reason, until)


class LoginFlow:
    """At most one login in progress per source."""

    def __init__(self, live: Any = None) -> None:
        self.lock = threading.Lock()
        # The source's LiveBrowser, for handing the browser to the user mid-login.
        self.live = live
        self.state = "idle"  # idle | running | need_input | live | done | failed
        self.prompt: dict | None = None
        self.messages: list[str] = []
        self.error: str | None = None
        self._answers: queue.Queue = queue.Queue()

    def start(self, login: Callable[[WebIO], None], on_success: Callable[[], None] | None = None) -> None:
        with self.lock:
            if self.state in ("running", "need_input", "live"):
                raise RuntimeError("a login is already in progress")
            self.state, self.prompt, self.messages, self.error = "running", None, [], None
            self._answers = queue.Queue()
        threading.Thread(target=self._run, args=(login, on_success), daemon=True).start()

    def _run(self, login, on_success) -> None:
        try:
            login(WebIO(self))
            with self.lock:
                self.state, self.prompt = "done", None
            if on_success:
                on_success()
        except Exception as e:
            log.warning("login failed: %s", e)
            with self.lock:
                self.state, self.prompt, self.error = "failed", None, str(e) or e.__class__.__name__

    def _message(self, msg: str) -> None:
        with self.lock:
            self.messages.append(msg)

    def _ask(self, msg: str, choices: list[str], img_url: str | None, link: str | None = None) -> str:
        with self.lock:
            self.state = "need_input"
            self.prompt = {"message": msg, "choices": list(choices), "image": img_url, "link": link}
        try:
            answer = self._answers.get(timeout=PROMPT_TIMEOUT)
        except queue.Empty:
            raise LoginCancelled("no answer within 5 minutes; start the login again") from None
        if answer is None:
            raise LoginCancelled("login cancelled")
        with self.lock:
            self.state, self.prompt = "running", None
        return answer

    def _handoff(self, context: Any, pages: list, reason: str, until: Callable[[Any], bool]) -> None:
        if self.live is None:
            raise LoginCancelled(f"{reason} (this needs the web UI's live view)")
        self.live.claim(reason)  # before the page is told to open the live view
        with self.lock:
            self.state, self.prompt = "live", {"message": reason, "choices": [], "image": None}
        try:
            if not self.live.attach(context, pages, until, reason):
                raise LoginCancelled(self.live.snapshot()["error"] or "login cancelled")
        finally:
            with self.lock:
                if self.state == "live":
                    self.state, self.prompt = "running", None

    def answer(self, value: str) -> None:
        with self.lock:
            if self.state != "need_input":
                raise RuntimeError("the login isn't waiting for input")
        self._answers.put(value.strip())

    def cancel(self) -> None:
        self._answers.put(None)

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "prompt": self.prompt, "messages": list(self.messages),
                    "error": self.error}

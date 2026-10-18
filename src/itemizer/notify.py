"""Notifications through Apprise: Discord, ntfy, Pushover, Telegram, Home
Assistant, email, plain JSON webhooks and ~100 more, each set by a URL.

APPRISE_URL takes one or more Apprise URLs, separated by commas or spaces.
See https://github.com/caronc/apprise/wiki for the formats.
"""
from __future__ import annotations

import logging

from .state import State

log = logging.getLogger(__name__)

TITLE = "Sure Itemizer"


class Notifier:
    def __init__(self, urls: str | None, public_url: str | None, state: State) -> None:
        self.urls = (urls or "").strip() or None
        self.public_url = public_url
        self.state = state
        self._apprise = None
        if self.urls:
            import apprise

            self._apprise = apprise.Apprise()
            if not self._apprise.add(self.urls):
                log.error("APPRISE_URL has a URL Apprise doesn't understand (see Apprise's message above); "
                          "notifications are off")
                self._apprise = None

    @property
    def configured(self) -> bool:
        return self._apprise is not None

    def send(self, event: str, message: str, source: str | None = None) -> bool:
        if not self._apprise:
            log.info("notification (Apprise not configured): %s", message)
            return False
        import apprise

        kind = apprise.NotifyType.INFO if event == "test" else apprise.NotifyType.WARNING
        body = message + (f"\n{self.public_url}" if self.public_url else "")
        try:
            ok = bool(self._apprise.notify(body=body, title=TITLE, notify_type=kind))
        except Exception as e:  # a broken notifier must never break a run
            log.warning("notification failed: %s", e.__class__.__name__)
            return False
        if not ok:
            log.warning("notification failed (Apprise couldn't deliver to every URL)")
        return ok

    def once(self, key: str, event: str, message: str, source: str | None = None) -> None:
        """Send only the first time `key` is raised, until `clear(key)`."""
        if self.state.flag(key):
            return
        if self.send(event, message, source) or not self.configured:
            self.state.set_flag(key)

    def clear(self, key: str) -> None:
        self.state.clear_flag(key)

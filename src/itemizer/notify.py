"""Webhook notifications (Discord, Slack, ntfy, Home Assistant, n8n, ...)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .state import State

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, url: str | None, public_url: str | None, state: State) -> None:
        self.url = url
        self.public_url = public_url
        self.state = state

    def send(self, event: str, message: str, source: str | None = None) -> bool:
        if not self.url:
            log.info("notification (no webhook configured): %s", message)
            return False
        text = message + (f"\n{self.public_url}" if self.public_url else "")
        payload = {
            "event": event,
            "source": source,
            "message": message,
            "url": self.public_url,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Discord reads "content", Slack/Mattermost/Google Chat read "text".
            "content": text,
            "text": text,
        }
        try:
            r = requests.post(self.url, json=payload, timeout=15)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.warning("webhook failed: %s", e)
            return False

    def once(self, key: str, event: str, message: str, source: str | None = None) -> None:
        """Send only the first time `key` is raised, until `clear(key)`."""
        if self.state.flag(key):
            return
        if self.send(event, message, source) or not self.url:
            self.state.set_flag(key)

    def clear(self, key: str) -> None:
        self.state.clear_flag(key)

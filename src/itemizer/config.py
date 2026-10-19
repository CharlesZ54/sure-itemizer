from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .cron import Cron


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


DEFAULT_SCHEDULE = "30 7 * * *"  # daily at 07:30


@dataclass(frozen=True)
class Config:
    sure_url: str
    sure_api_key: str
    dry_run: bool
    lookback_days: int
    run_on_start: bool
    web_port: int
    data_dir: Path
    sources: tuple[str, ...]
    apprise_url: str | None = None
    # Where the user reaches the status page; put in notifications as a link.
    public_url: str | None = None
    # When to run, as a cron expression in the container's time zone (TZ).
    # Merchant credentials and choices (such as the Amazon site) are
    # deliberately not configuration: they're entered on the Connect pages.
    schedule: str = DEFAULT_SCHEDULE

    @classmethod
    def from_env(cls) -> "Config":
        sure_url = _env("SURE_URL")
        api_key = _env("SURE_API_KEY")
        if not sure_url or not api_key:
            raise SystemExit("SURE_URL and SURE_API_KEY are required")
        schedule = " ".join(_env("SCHEDULE", DEFAULT_SCHEDULE).split())
        try:
            Cron(schedule)
        except ValueError as e:
            raise SystemExit(f"SCHEDULE isn't a valid cron expression: {e}") from None
        return cls(
            sure_url=sure_url.rstrip("/"),
            sure_api_key=api_key,
            dry_run=_bool(_env("DRY_RUN"), True),
            lookback_days=int(_env("LOOKBACK_DAYS", "30")),
            run_on_start=_bool(_env("RUN_ON_START"), True),
            web_port=int(_env("WEB_PORT", "8686")),
            data_dir=Path(_env("DATA_DIR", "/data")),
            sources=tuple(s.strip().lower() for s in _env("SOURCES", "amazon").split(",") if s.strip()),
            apprise_url=_env("APPRISE_URL"),
            public_url=(_env("PUBLIC_URL") or "").rstrip("/") or None,
            schedule=schedule,
        )

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Config:
    sure_url: str
    sure_api_key: str
    dry_run: bool
    lookback_days: int
    run_times: tuple[str, ...]
    run_on_start: bool
    web_port: int
    data_dir: Path
    sources: tuple[str, ...]
    notify_webhook_url: str | None = None
    # Where the user reaches the status page; put in notifications as a link.
    public_url: str | None = None

    # Merchant credentials are deliberately not configuration: they are typed
    # into the web UI at login time and never stored.
    amazon_card_last4: str | None = None
    amazon_sure_account: str | None = None
    amazon_domain: str | None = None

    def source_setting(self, prefix: str, key: str, default: str | None = None) -> str | None:
        """A merchant's own setting, read from {PREFIX}_{KEY} (e.g. TARGET_SURE_ACCOUNT)."""
        return _env(f"{prefix}_{key}", default)

    @classmethod
    def from_env(cls) -> "Config":
        sure_url = _env("SURE_URL")
        api_key = _env("SURE_API_KEY")
        if not sure_url or not api_key:
            raise SystemExit("SURE_URL and SURE_API_KEY are required")
        return cls(
            sure_url=sure_url.rstrip("/"),
            sure_api_key=api_key,
            dry_run=_bool(_env("DRY_RUN"), True),
            lookback_days=int(_env("LOOKBACK_DAYS", "30")),
            run_times=tuple(t.strip() for t in _env("RUN_TIMES", "07:30").split(",") if t.strip()),
            run_on_start=_bool(_env("RUN_ON_START"), True),
            web_port=int(_env("WEB_PORT", "8686")),
            data_dir=Path(_env("DATA_DIR", "/data")),
            sources=tuple(s.strip().lower() for s in _env("SOURCES", "amazon").split(",") if s.strip()),
            notify_webhook_url=_env("NOTIFY_WEBHOOK_URL"),
            public_url=(_env("PUBLIC_URL") or "").rstrip("/") or None,
            amazon_card_last4=_env("AMZN_CARD_LAST4"),
            amazon_sure_account=_env("AMZN_SURE_ACCOUNT"),
            amazon_domain=_env("AMZN_DOMAIN"),
        )

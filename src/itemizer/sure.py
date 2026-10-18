from __future__ import annotations

import logging
from datetime import date

import requests

from .models import SureTxn
from .money import cents_to_str

log = logging.getLogger(__name__)


class SureError(RuntimeError):
    pass


class SplitsUnsupported(SureError):
    """This Sure has no split API (it arrived in v0.7.6)."""


class SureClient:
    def __init__(self, base_url: str, api_key: str, session: requests.Session | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = session or requests.Session()
        self.http.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        r = self.http.request(method, f"{self.base}/api/v1{path}", timeout=30, **kw)
        if r.status_code in (401, 403):
            raise SureError(f"Sure rejected the API key ({r.status_code}): {r.text[:200]}")
        return r

    @staticmethod
    def _txn(d: dict) -> SureTxn:
        # amount_cents is unsigned; Sure stores purchases positive, refunds negative.
        cents = int(d["amount_cents"])
        return SureTxn(
            id=d["id"],
            date=date.fromisoformat(d["date"]),
            amount_cents=-cents if d.get("classification") == "income" else cents,
            name=d.get("name") or "",
            notes=d.get("notes"),
            account_id=d["account"]["id"],
            account_name=d["account"]["name"],
            category=(d.get("category") or {}).get("name"),
        )

    def transactions(self, start_date: date, search: str | None = None) -> list[SureTxn]:
        out, page = [], 1
        while True:
            params = {"start_date": start_date.isoformat(), "per_page": 100, "page": page}
            if search:
                params["search"] = search
            r = self._call("GET", "/transactions", params=params)
            if r.status_code != 200:
                raise SureError(f"listing transactions failed ({r.status_code}): {r.text[:200]}")
            body = r.json()
            out += [self._txn(d) for d in body["transactions"]]
            if page >= body["pagination"]["total_pages"]:
                return out
            page += 1

    def split_state(self, txn_id: str) -> bool:
        """True if the transaction (or its parent) is already split."""
        r = self._call("GET", f"/transactions/{txn_id}/split")
        if r.status_code == 200:
            return True
        if r.status_code == 404 and _json(r).get("message") == "Transaction is not split":
            return False
        if _no_route(r):
            raise SplitsUnsupported("Sure has no /split endpoint; it needs v0.7.6 or later")
        raise SureError(f"checking split state failed ({r.status_code}): {r.text[:200]}")

    def split(self, txn_id: str, children: list[tuple[str, int]]) -> list[str]:
        body = {"split": {"splits": [{"name": n, "amount": cents_to_str(c)} for n, c in children]}}
        r = self._call("POST", f"/transactions/{txn_id}/split", json=body)
        if _no_route(r):
            raise SplitsUnsupported("Sure has no /split endpoint; it needs v0.7.6 or later")
        if r.status_code != 201:
            raise SureError(f"split failed ({r.status_code}): {_json(r).get('errors') or r.text[:200]}")
        return [c["id"] for c in r.json().get("children", [])]


def _no_route(r: requests.Response) -> bool:
    # An unknown route is Rails' generic 404 -- HTML, or {"status":404,"error":"Not Found"}
    # when JSON is requested -- never Sure's {"error":"not_found","message":...}.
    return r.status_code == 404 and "message" not in _json(r)


def _json(r: requests.Response) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}

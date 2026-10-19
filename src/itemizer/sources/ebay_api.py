"""A small client for eBay's official APIs, as a buyer.

Signing in is eBay's OAuth authorization-code grant, with the user's own
developer keyset (client ID, client secret and RuName). The resulting refresh
token lasts about 18 months; access tokens (2 hours) are minted from it as
needed. Orders come from the Trading API's GetOrders with OrderRole=Buyer,
which reaches back 90 days. Only reading is implemented.
"""
from __future__ import annotations

import base64
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRADING_URL = "https://api.ebay.com/ws/api.dll"
# The basic scope every keyset has; the Trading API doesn't check scopes.
SCOPE = "https://api.ebay.com/oauth/api_scope"
COMPATIBILITY_LEVEL = "1349"
NS = "{urn:ebay:apis:eBLBaseComponents}"
MAX_DAYS_BACK = 89  # GetOrders refuses CreateTimeFrom more than 90 days ago
# Trading API error codes for a missing, expired or revoked token.
TOKEN_ERRORS = {"931", "932", "16110", "21916013", "21917053"}


class EbayError(RuntimeError):
    pass


class EbayAuthError(EbayError):
    """The keys or the user's consent are missing, wrong, expired or revoked."""


def code_from(answer: str, state: str | None = None) -> str:
    """The authorization code from the address eBay sent the browser to (or the bare code)."""
    answer = answer.strip()
    if "code=" not in answer:
        if not answer or " " in answer:
            raise EbayAuthError("that doesn't look like the address eBay sent you to")
        return answer
    query = parse_qs(urlparse(answer).query or answer.split("?", 1)[-1])
    if query.get("isAuthSuccessful", ["true"])[0].lower() == "false":
        raise EbayAuthError("eBay says the sign-in was declined")
    if state and query.get("state", [state])[0] != state:
        raise EbayAuthError("that address belongs to a different sign-in; start again")
    return query["code"][0]


def _ns(path: str) -> str:
    """'OrderArray/Order' -> the same path in eBay's XML namespace."""
    return "/".join(f"{NS}{part}" for part in path.split("/"))


def text(el: ET.Element | None, path: str, default: str = "") -> str:
    found = el.find(_ns(path)) if el is not None else None
    return found.text.strip() if found is not None and found.text else default


def find_all(el: ET.Element, path: str) -> list[ET.Element]:
    return el.findall(_ns(path))


class EbayClient:
    def __init__(self, client_id: str, client_secret: str, runame: str, refresh_token: str | None = None,
                 session: requests.Session | None = None) -> None:
        self.client_id, self.client_secret, self.runame = client_id, client_secret, runame
        self.refresh_token = refresh_token
        self.refresh_expires_at: float | None = None
        self.http = session or requests.Session()
        self._access: tuple[str, float] | None = None

    # Signing in ---------------------------------------------------------

    def auth_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({"client_id": self.client_id, "redirect_uri": self.runame,
                                           "response_type": "code", "scope": SCOPE, "state": state,
                                           "prompt": "login"})

    def _token(self, data: dict) -> dict:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            r = self.http.post(TOKEN_URL, data=data, timeout=30,
                               headers={"Authorization": f"Basic {basic}",
                                        "Content-Type": "application/x-www-form-urlencoded"})
        except requests.RequestException as e:
            raise EbayError(f"couldn't reach eBay: {e.__class__.__name__}") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not r.ok:
            reason = body.get("error_description") or body.get("error") or r.reason
            raise EbayAuthError(f"eBay says: {reason}")
        return body

    def exchange(self, code: str) -> None:
        body = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.runame})
        self.refresh_token = body["refresh_token"]
        self.refresh_expires_at = time.time() + int(body.get("refresh_token_expires_in") or 0) or None
        self._access = (body["access_token"], time.time() + int(body.get("expires_in") or 7200) - 300)

    def access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        if not self.refresh_token:
            raise EbayAuthError("eBay isn't connected yet")
        body = self._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token, "scope": SCOPE})
        self._access = (body["access_token"], time.time() + int(body.get("expires_in") or 7200) - 300)
        return self._access[0]

    # Reading ------------------------------------------------------------

    def trading(self, call: str, inner: str = "") -> ET.Element:
        xml = (f'<?xml version="1.0" encoding="utf-8"?>'
               f'<{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">{inner}</{call}Request>')
        headers = {"X-EBAY-API-CALL-NAME": call, "X-EBAY-API-SITEID": "0",
                   "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
                   "X-EBAY-API-IAF-TOKEN": self.access_token(), "Content-Type": "text/xml"}
        try:
            r = self.http.post(TRADING_URL, data=xml.encode(), headers=headers, timeout=60)
        except requests.RequestException as e:
            raise EbayError(f"couldn't reach eBay: {e.__class__.__name__}") from None
        if r.status_code in (401, 403):
            raise EbayAuthError(f"eBay rejected the token ({r.status_code})")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise EbayError(f"eBay error {r.status_code}: unreadable response") from None
        if text(root, "Ack") == "Failure":
            errors = find_all(root, "Errors")
            codes = {text(e, "ErrorCode") for e in errors}
            message = "; ".join(text(e, "LongMessage") or text(e, "ShortMessage") for e in errors) or "unknown"
            raise (EbayAuthError if codes & TOKEN_ERRORS else EbayError)(f"eBay says: {message}")
        return root

    def user_id(self) -> str:
        return text(self.trading("GetUser"), "User/UserID")

    def orders(self, since: datetime) -> list[ET.Element]:
        """The user's purchases created since `since` (at most 90 days back)."""
        now = datetime.now(timezone.utc)
        since = max(since, now - timedelta(days=MAX_DAYS_BACK))
        stamp = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        out: list[ET.Element] = []
        for page in range(1, 51):
            root = self.trading("GetOrders", (
                f"<CreateTimeFrom>{stamp(since)}</CreateTimeFrom><CreateTimeTo>{stamp(now)}</CreateTimeTo>"
                f"<OrderRole>Buyer</OrderRole><OrderStatus>All</OrderStatus><DetailLevel>ReturnAll</DetailLevel>"
                f"<Pagination><EntriesPerPage>100</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"))
            out += find_all(root, "OrderArray/Order")
            if text(root, "HasMoreOrders").lower() != "true":
                break
        return out

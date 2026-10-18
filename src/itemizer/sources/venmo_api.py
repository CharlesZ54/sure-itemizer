"""A small client for Venmo's (unofficial) mobile API.

The sign-in mirrors the venmo-api library (github.com/mmohades/Venmo): password,
then a code texted to the account's phone, then this device is trusted so later
sign-ins skip the code. The resulting token doesn't expire until it's revoked.
Only reading is implemented; nothing here can move money.
"""
from __future__ import annotations

import uuid

import requests

BASE = "https://api.venmo.com/v1"
USER_AGENT = "Venmo/10.50.0 (iPhone; iOS 17.0; Scale/3.0)"
TWO_FACTOR_ERROR = 81109


class VenmoError(RuntimeError):
    pass


class VenmoAuthError(VenmoError):
    """The token is missing, revoked or rejected."""


def new_device_id() -> str:
    # Same shape as the app's: 8-4-4-4-12 hex, upper-case.
    return str(uuid.uuid4()).upper()


class VenmoClient:
    def __init__(self, token: str | None = None, device_id: str | None = None,
                 session: requests.Session | None = None) -> None:
        self.device_id = device_id or new_device_id()
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "device-id": self.device_id})
        self.token = None
        if token:
            self.set_token(token)

    def set_token(self, token: str) -> None:
        self.token = token.strip().removeprefix("Bearer ").strip()
        self.http.headers["Authorization"] = f"Bearer {self.token}"

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        try:
            return self.http.request(method, BASE + path, timeout=30, **kw)
        except requests.RequestException as e:
            raise VenmoError(f"couldn't reach Venmo: {e.__class__.__name__}") from None

    @staticmethod
    def _error(r: requests.Response) -> tuple[int | None, str]:
        try:
            err = (r.json() or {}).get("error") or {}
            return err.get("code"), err.get("message") or r.reason
        except ValueError:
            return None, r.reason

    # Signing in ---------------------------------------------------------

    def start_login(self, username: str, password: str) -> str | None:
        """Returns None when signed in already, or the otp-secret when Venmo wants a texted code."""
        r = self._call("POST", "/oauth/access_token",
                       json={"phone_email_or_username": username, "client_id": "1", "password": password})
        if r.ok:
            self.set_token(r.json()["access_token"])
            return None
        code, message = self._error(r)
        secret = r.headers.get("venmo-otp-secret")
        if code == TWO_FACTOR_ERROR and secret:
            return secret
        raise VenmoAuthError(f"Venmo says: {message}")

    def send_code(self, otp_secret: str) -> None:
        r = self._call("POST", "/account/two-factor/token", json={"via": "sms"},
                       headers={"venmo-otp-secret": otp_secret})
        if not r.ok:
            raise VenmoAuthError(f"Venmo couldn't text a code: {self._error(r)[1]}")

    def finish_login(self, code: str, otp_secret: str) -> None:
        r = self._call("POST", "/oauth/access_token", params={"client_id": 1},
                       headers={"venmo-otp": code, "venmo-otp-secret": otp_secret})
        if not r.ok:
            raise VenmoAuthError(f"Venmo says: {self._error(r)[1]}")
        self.set_token(r.json()["access_token"])

    def trust_device(self) -> None:
        self._call("POST", "/users/devices")  # best effort: only saves future codes

    # Reading ------------------------------------------------------------

    def _get(self, path: str, **params) -> dict:
        r = self._call("GET", path, params=params)
        if r.status_code in (401, 403):
            raise VenmoAuthError(f"Venmo rejected the token: {self._error(r)[1]}")
        if not r.ok:
            raise VenmoError(f"Venmo error {r.status_code}: {self._error(r)[1]}")
        return r.json()

    def me(self) -> dict:
        return self._get("/account")["data"]["user"]

    def stories(self, user_id: str, before_id: str | None = None, limit: int = 50) -> list[dict]:
        params = {"limit": limit, **({"before_id": before_id} if before_id else {})}
        return self._get(f"/stories/target-or-actor/{user_id}", **params).get("data") or []

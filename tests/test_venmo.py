from datetime import date

import pytest

from itemizer.models import SureTxn
from itemizer.planner import ORIGINAL_MARKER, plan
from itemizer.sources.venmo import parse_statement, purchases_from

# Shaped like Venmo's statement CSV; people and amounts are made up.
STATEMENT = """Account Statement - (@Pat-Example) ,,,,,,,,,,,,,,,,,,,,,
Account Activity,,,,,,,,,,,,,,,,,,,,,
,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,Destination,Beginning Balance,Ending Balance,Statement Period Venmo Fees,Terminal Location,Year to Date Venmo Fees,Disclaimer
,,,,,,,,,,,,,,,,$10.00,,,,,
,1001,2026-10-01T12:00:00,Payment,Complete,pizza \U0001F355,Pat Example,Alex Doe,- $38.09,,0,,0,,Bank Account *0000,,,,,Venmo,,
,1002,2026-10-02T12:00:00,Payment,Complete,dinner,Sam Roe,Pat Example,+ $40.00,,0,,0,,,Venmo balance,,,,Venmo,,
,1003,2026-10-03T09:00:00,Charge,Complete,tickets,Pat Example,Jo Poe,+ $25.19,,0,,0,,,Venmo balance,,,,Venmo,,
,1004,2026-10-03T12:00:00,Payment,Complete,coffee,Pat Example,Kim Loe,- $5.00,,0,,0,,Venmo balance,,,,,Venmo,,
,1005,2026-10-04T08:00:00,Standard Transfer,Issued,,,,- $65.19,,0,,0,,,Bank Account *0000,,,,Venmo,,
,1006,2026-10-06T08:00:00,Instant Transfer,Issued,,,,- $100.00,,0,- $1.75,0,,,Bank Account *0000,,,,Venmo,,
,1007,2026-10-07T08:00:00,Charge,Complete,"utilities, Oct",Lee Moe,Pat Example,"- $1,076.20",,0,,0,,Bank Account *0000,,,,,Venmo,,
,1008,2026-10-08T08:00:00,Payment,Cancelled,oops,Pat Example,Kim Loe,- $9.00,,0,,0,,Bank Account *0000,,,,,Venmo,,
,,,,,,,,,,,,,,,,,$0.00,,,,
"""


def by_id(purchases):
    return {p.external_id: p for p in purchases}


def test_parses_rows_and_skips_balance_lines():
    rows = parse_statement(STATEMENT)
    assert [r.id for r in rows] == ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008"]
    assert rows[0].cents == -3809 and rows[6].cents == -107620 and rows[5].fee_cents == 175
    assert rows[6].note == "utilities, Oct"


def test_rejects_files_that_arent_statements():
    with pytest.raises(ValueError):
        parse_statement("date,amount\n2026-01-01,5\n")


def test_payments_from_the_bank_are_described_with_who_and_why():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1001"].amount_cents == 3809 and p["1001"].description == "Venmo to Alex Doe: pizza \U0001F355"
    # A request someone else sent you, which you paid: the other side is its sender.
    assert p["1007"].amount_cents == 107620 and p["1007"].description == "Venmo to Lee Moe: utilities, Oct"


def test_cash_outs_list_the_payments_they_carried():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1005"].amount_cents == -6519
    assert p["1005"].description == "Venmo cash-out: Sam Roe (dinner), Jo Poe (tickets)"
    # Instant transfer: the bank gets the amount minus the fee.
    assert p["1006"].amount_cents == -9825 and p["1006"].description == "Venmo cash-out"


def test_balance_payments_and_cancelled_ones_never_reach_the_bank():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert "1004" not in p and "1008" not in p and "1002" not in p


def test_many_payments_are_summarised():
    rows = parse_statement(STATEMENT)
    extra = "\n".join(f",20{i},2026-10-05T0{i}:00:00,Payment,Complete,n{i},P{i},Pat Example,+ $1.00,,0,,0,,,"
                      f"Venmo balance,,,,Venmo,," for i in range(1, 6))
    more = parse_statement(STATEMENT.replace(",1006,", extra + "\n,1006,", 1))
    cash_out = by_id(purchases_from(more))["1006"]
    assert cash_out.description == "Venmo cash-out: P1 (n1), P2 (n2), P3 (n3) +2 more"
    assert len(rows) == 8


def test_rename_plan_keeps_the_bank_text_in_the_notes():
    txn = SureTxn("t", date(2026, 10, 2), 3809, "Transfer to Venmo - VENMO PAYMENT", "mine", "a", "Checking", None)
    purchase = by_id(purchases_from(parse_statement(STATEMENT)))["1001"]
    result = plan(txn, purchase)
    assert result.action == "rename" and result.name == "Venmo to Alex Doe: pizza \U0001F355"
    assert result.notes == f"mine\n{ORIGINAL_MARKER}Transfer to Venmo - VENMO PAYMENT"
    # Renaming again doesn't stack the original text.
    again = plan(SureTxn("t", txn.date, 3809, "something", result.notes, "a", "Checking", None), purchase)
    assert again.notes == result.notes


# --- the mobile API -------------------------------------------------------

import json as _json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from itemizer.models import Purchase  # noqa: E402,F401
from itemizer.sources import venmo_api  # noqa: E402
from itemizer.sources.base import LoginRequired  # noqa: E402
from itemizer.sources.venmo import VenmoSource, describe_cash_outs, payments_from_stories  # noqa: E402

ME = {"id": "100", "username": "pat-example", "display_name": "Pat Example"}
ALEX = {"id": "200", "display_name": "Alex Doe"}
SAM = {"id": "300", "first_name": "Sam", "last_name": "Roe"}


def story(pid, action, actor, target, amount, note, when, status="settled"):
    return {"id": f"s{pid}", "date_created": when, "type": "payment",
            "payment": {"id": pid, "action": action, "actor": actor, "target": {"type": "user", "user": target},
                        "amount": amount, "note": note, "status": status, "date_completed": when}}


STORIES = [
    story("1", "pay", ME, ALEX, 38.09, "pizza", "2026-10-01T12:00:00"),        # I paid Alex
    story("2", "pay", SAM, ME, 40.0, "dinner", "2026-10-02T12:00:00"),         # Sam paid me
    story("3", "charge", ME, ALEX, 25.19, "tickets", "2026-10-03T12:00:00"),   # I requested, Alex paid
    story("4", "charge", SAM, ME, 76.20, "utilities", "2026-10-07T12:00:00"),  # Sam requested, I paid
    story("5", "pay", ME, SAM, 9.0, "oops", "2026-10-08T12:00:00", status="cancelled"),
]


def test_stories_become_rows_with_the_right_direction():
    rows = {r.id: r for r in payments_from_stories(STORIES, "100")}
    assert rows["1"].cents == -3809 and rows["1"].counterparty == "Alex Doe"
    assert rows["2"].cents == 4000 and rows["2"].counterparty == "Sam Roe"
    assert rows["3"].cents == 2519 and rows["3"].counterparty == "Alex Doe"
    assert rows["4"].cents == -7620 and rows["4"].counterparty == "Sam Roe"
    assert "5" not in rows


def test_cash_outs_are_described_from_what_came_in_before_them():
    received = [r for r in payments_from_stories(STORIES, "100") if r.cents > 0]
    txn = lambda tid, d, cents: SureTxn(tid, d, cents, "Transfer from Venmo - VENMO CASHOUT", None, "a", "Chk", None)
    outs = describe_cash_outs([txn("c1", date(2026, 10, 2), -4000), txn("c2", date(2026, 10, 5), -2519),
                               txn("not-a-cashout", date(2026, 10, 5), 500)], received, since=date(2026, 9, 20))
    assert [(p.external_id, p.amount_cents, p.description) for p in outs] == [
        ("cash-out:c1", -4000, "Venmo cash-out: Sam Roe (dinner)"),
        ("cash-out:c2", -2519, "Venmo cash-out: Alex Doe (tickets)"),
    ]


class FakeVenmo:
    """api.venmo.com's sign-in and reading endpoints."""

    def __init__(self, want_code=True):
        self.calls, self.want_code, self.token = [], want_code, "tok-123"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body, headers=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(_json.dumps(body).encode())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = _json.loads(self.rfile.read(n) or b"{}")
                fake.calls.append(("POST", self.path, body, dict(self.headers)))
                if self.path.startswith("/v1/oauth/access_token"):
                    if self.headers.get("venmo-otp"):
                        if self.headers["venmo-otp"] == "123456" and self.headers.get("venmo-otp-secret") == "sec":
                            return self._json(200, {"access_token": fake.token})
                        return self._json(400, {"error": {"message": "Wrong code"}})
                    if body.get("password") != "pw":
                        return self._json(401, {"error": {"code": 264, "message": "Your password is incorrect"}})
                    if fake.want_code:
                        return self._json(401, {"error": {"code": 81109, "message": "2FA"}}, {"venmo-otp-secret": "sec"})
                    return self._json(200, {"access_token": fake.token})
                return self._json(200, {})

            def do_GET(self):
                fake.calls.append(("GET", self.path, None, dict(self.headers)))
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._json(401, {"error": {"message": "Invalid token"}})
                if self.path.startswith("/v1/account"):
                    return self._json(200, {"data": {"user": ME}})
                if self.path.startswith("/v1/stories/target-or-actor/100"):
                    return self._json(200, {"data": [] if "before_id" in self.path else STORIES})
                self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"


class CodeIO:
    def __init__(self, code):
        self.code, self.prompts = code, []

    def prompt(self, msg, type=None, **kw):
        self.prompts.append(msg)
        return self.code


@pytest.fixture
def venmo(monkeypatch, tmp_path):
    fake = FakeVenmo()
    monkeypatch.setattr(venmo_api, "BASE", fake.base)
    return fake, VenmoSource(tmp_path / "venmo")


def test_sign_in_with_texted_code_saves_only_the_token(venmo):
    fake, source = venmo
    io = CodeIO("123 456")
    source.login({"username": "me@example.com", "password": "pw", "token": ""}, io)
    assert io.prompts == ["Enter the code Venmo texted you"]
    paths = [c[1].split("?")[0] for c in fake.calls]
    assert paths == ["/v1/oauth/access_token", "/v1/account/two-factor/token", "/v1/oauth/access_token",
                     "/v1/users/devices", "/v1/account"]
    saved = source.auth_path.read_text()
    assert _json.loads(saved)["token"] == "tok-123" and "pw" not in saved
    assert oct(os.stat(source.auth_path).st_mode & 0o777) == "0o600"
    # The same device id is used throughout, so Venmo can trust it.
    devices = {c[3].get("device-id") for c in fake.calls}
    assert len(devices) == 1 and _json.loads(saved)["device_id"] in devices


def test_wrong_password_is_reported(venmo):
    _, source = venmo
    with pytest.raises(venmo_api.VenmoAuthError, match="password is incorrect"):
        source.login({"username": "me@example.com", "password": "nope", "token": ""}, CodeIO("1"))
    assert not source.connected()


def test_pasting_a_token_works_and_bad_ones_are_refused(venmo):
    fake, source = venmo
    with pytest.raises(venmo_api.VenmoAuthError):
        source.login({"token": "wrong"}, None)
    assert not source.connected()
    source.login({"token": "Bearer tok-123"}, None)
    assert source.connected()


def test_fetch_describes_payments_and_cash_outs(venmo):
    _, source = venmo
    source.login({"token": "tok-123"}, None)
    cash_out = SureTxn("c1", date(2026, 10, 4), -6519, "Transfer from Venmo - VENMO CASHOUT", None, "a", "Chk", None)
    got = {p.external_id: p for p in source.fetch(date(2026, 9, 20), txns=[cash_out])}
    assert got["1"].amount_cents == 3809 and got["1"].description == "Venmo to Alex Doe: pizza"
    assert got["4"].amount_cents == 7620 and got["4"].description == "Venmo to Sam Roe: utilities"
    assert got["cash-out:c1"].description == "Venmo cash-out: Sam Roe (dinner), Alex Doe (tickets)"
    assert "2" not in got and "3" not in got  # money in only shows up via a cash-out


def test_a_rejected_token_means_sign_in_again(venmo):
    fake, source = venmo
    source.login({"token": "tok-123"}, None)
    fake.token = "revoked-now"
    with pytest.raises(LoginRequired, match="rejected the token"):
        source.fetch(date(2026, 9, 20), txns=[])

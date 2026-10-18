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
,1001,2026-10-01T12:00:00,Payment,Complete,pizza \U0001F355,Pat Example,Alex Doe,- $21.50,,0,,0,,Bank Account *0000,,,,,Venmo,,
,1002,2026-10-02T12:00:00,Payment,Complete,dinner,Sam Roe,Pat Example,+ $40.00,,0,,0,,,Venmo balance,,,,Venmo,,
,1003,2026-10-03T09:00:00,Charge,Complete,tickets,Pat Example,Jo Poe,+ $25.00,,0,,0,,,Venmo balance,,,,Venmo,,
,1004,2026-10-03T12:00:00,Payment,Complete,coffee,Pat Example,Kim Loe,- $5.00,,0,,0,,Venmo balance,,,,,Venmo,,
,1005,2026-10-04T08:00:00,Standard Transfer,Issued,,,,- $65.00,,0,,0,,,Bank Account *0000,,,,Venmo,,
,1006,2026-10-06T08:00:00,Instant Transfer,Issued,,,,- $100.00,,0,- $1.75,0,,,Bank Account *0000,,,,Venmo,,
,1007,2026-10-07T08:00:00,Charge,Complete,"utilities, Oct",Lee Moe,Pat Example,"- $1,088.00",,0,,0,,Bank Account *0000,,,,,Venmo,,
,1008,2026-10-08T08:00:00,Payment,Cancelled,oops,Pat Example,Kim Loe,- $9.00,,0,,0,,Bank Account *0000,,,,,Venmo,,
,,,,,,,,,,,,,,,,,$0.00,,,,
"""


def by_id(purchases):
    return {p.external_id: p for p in purchases}


def test_parses_rows_and_skips_balance_lines():
    rows = parse_statement(STATEMENT)
    assert [r.id for r in rows] == ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008"]
    assert rows[0].cents == -2150 and rows[6].cents == -108800 and rows[5].fee_cents == 175
    assert rows[6].note == "utilities, Oct"


def test_rejects_files_that_arent_statements():
    with pytest.raises(ValueError):
        parse_statement("date,amount\n2026-01-01,5\n")


def test_payments_from_the_bank_are_described_with_who_and_why():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1001"].amount_cents == 2150 and p["1001"].description == "Venmo to Alex Doe: pizza \U0001F355"
    # A request someone else sent you, which you paid: the other side is its sender.
    assert p["1007"].amount_cents == 108800 and p["1007"].description == "Venmo to Lee Moe: utilities, Oct"


def test_cash_outs_split_into_the_payments_they_carried():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1005"].amount_cents == -6500
    # In: dinner and tickets; out of the balance: coffee; the rest was there before.
    assert [(line.name, line.amount_cents) for line in p["1005"].lines] == [
        ("Venmo from Sam Roe: dinner", -4000), ("Venmo from Jo Poe: tickets", -2500),
        ("Venmo to Kim Loe: coffee", 500), ("Venmo balance from before", -500)]
    # Instant transfer: the bank gets the amount minus the fee.
    assert p["1006"].amount_cents == -9825 and p["1006"].description == "Venmo cash-out"


def test_balance_payments_and_cancelled_ones_never_reach_the_bank():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert "1004" not in p and "1008" not in p and "1002" not in p


def test_rename_plan_keeps_the_bank_text_in_the_notes():
    txn = SureTxn("t", date(2026, 10, 2), 2150, "Transfer to Venmo - VENMO PAYMENT", "mine", "a", "Checking", None)
    purchase = by_id(purchases_from(parse_statement(STATEMENT)))["1001"]
    result = plan(txn, purchase)
    assert result.action == "rename" and result.name == "Venmo to Alex Doe: pizza \U0001F355"
    assert result.notes == f"mine\n{ORIGINAL_MARKER}Transfer to Venmo - VENMO PAYMENT"
    # Renaming again doesn't stack the original text.
    again = plan(SureTxn("t", txn.date, 2150, "something", result.notes, "a", "Checking", None), purchase)
    assert again.notes == result.notes


# --- the mobile API -------------------------------------------------------

import json as _json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from itemizer.models import Purchase  # noqa: E402,F401
from itemizer.sources import venmo_api  # noqa: E402
from itemizer.sources.base import LoginRequired  # noqa: E402
from itemizer.sources.venmo import VenmoSource, purchases_from, rows_from_stories  # noqa: E402

ME = {"id": "100", "username": "pat-example", "display_name": "Pat Example"}
ALEX = {"id": "200", "display_name": "Alex Doe"}
SAM = {"id": "300", "first_name": "Sam", "last_name": "Roe"}


def story(pid, action, actor, target, amount, note, when, status="settled"):
    return {"id": f"s{pid}", "date_created": when, "type": "payment",
            "payment": {"id": pid, "action": action, "actor": actor, "target": {"type": "user", "user": target},
                        "amount": amount, "note": note, "status": status, "date_completed": when}}


STORIES = [
    story("1", "pay", ME, ALEX, 21.50, "pizza", "2026-10-01T12:00:00"),        # I paid Alex
    story("2", "pay", SAM, ME, 40.0, "dinner", "2026-10-02T12:00:00"),         # Sam paid me
    story("3", "charge", ME, ALEX, 25.00, "tickets", "2026-10-03T12:00:00"),   # I requested, Alex paid
    story("4", "charge", SAM, ME, 88.00, "utilities", "2026-10-07T12:00:00"),  # Sam requested, I paid
    story("5", "pay", ME, SAM, 9.0, "oops", "2026-10-08T12:00:00", status="cancelled"),
]


def test_stories_become_rows_with_the_right_direction():
    rows = {r.id: r for r in rows_from_stories(STORIES, "100")}
    assert rows["1"].cents == -2150 and rows["1"].counterparty == "Alex Doe"
    assert rows["2"].cents == 4000 and rows["2"].counterparty == "Sam Roe"
    assert rows["3"].cents == 2500 and rows["3"].counterparty == "Alex Doe"
    assert rows["4"].cents == -8800 and rows["4"].counterparty == "Sam Roe"
    assert "5" not in rows


def transfer(tid, cents, when, fee=0):
    return {"id": f"t{tid}", "date_created": when, "type": "transfer", "payment": None,
            "transfer": {"id": int(tid), "amount_cents": cents, "amount_fee_cents": fee, "date_requested": when,
                         "status": "issued", "destination": {"type": "bank", "name": "Example Bank"}}}


CASH_OUTS = [transfer("901", 4000, "2026-10-02T18:00:00"), transfer("902", 2500, "2026-10-05T18:00:00")]


def parts(p):
    return [(line.name, line.amount_cents) for line in p.lines]


def test_cash_outs_split_into_what_came_in_since_the_previous_one():
    rows = rows_from_stories(STORIES + CASH_OUTS, "100")
    outs = {p.external_id: p for p in purchases_from(rows) if p.amount_cents < 0}
    assert outs["901"].amount_cents == -4000 and parts(outs["901"]) == [("Venmo from Sam Roe: dinner", -4000)]
    # Not Sam again: he was in the first.
    assert outs["902"].amount_cents == -2500 and parts(outs["902"]) == [("Venmo from Alex Doe: tickets", -2500)]
    assert outs["901"].description is None


def test_balance_payments_and_leftovers_are_parts_of_the_cash_out():
    stories = [
        story("1", "pay", SAM, ME, 100.0, "dinner", "2026-10-01T12:00:00"),
        story("2", "pay", ME, ALEX, 30.0, "from the balance", "2026-10-02T12:00:00"),
        story("3", "pay", ME, ALEX, 12.0, "from the bank", "2026-10-02T13:00:00"),
        transfer("904", 6000, "2026-10-03T12:00:00"),
    ]
    by_bank = lambda r: r.id == "3"  # noqa: E731
    out = {p.external_id: p for p in purchases_from(rows_from_stories(stories, "100"), bank_paid=by_bank)}
    assert out["3"].description == "Venmo to Alex Doe: from the bank"
    assert parts(out["904"]) == [("Venmo from Sam Roe: dinner", -10000), ("Venmo to Alex Doe: from the balance", 3000),
                                 ("Left in the Venmo balance", 1000)]


def test_an_instant_cash_out_arrives_minus_its_fee():
    stories = [story("1", "pay", SAM, ME, 100.0, "dinner", "2026-10-01T12:00:00"),
               transfer("903", 10000, "2026-10-09T09:00:00", fee=175)]
    (p,) = purchases_from(rows_from_stories(stories, "100"))
    assert p.amount_cents == -9825
    assert parts(p) == [("Venmo from Sam Roe: dinner", -10000), ("Venmo instant transfer fee", 175)]


def test_a_cash_out_with_nothing_behind_it_is_just_named():
    (p,) = purchases_from(rows_from_stories([transfer("905", 500, "2026-10-09T09:00:00")], "100"))
    assert p.description == "Venmo cash-out" and not p.lines


def test_the_cash_out_split_is_planned_with_exact_amounts():
    from itemizer.planner import plan
    stories = [story("1", "pay", SAM, ME, 100.0, "dinner", "2026-10-01T12:00:00"),
               story("2", "pay", ME, ALEX, 35.0, "lunch", "2026-10-02T12:00:00"),
               transfer("906", 6500, "2026-10-03T12:00:00")]
    (p,) = purchases_from(rows_from_stories(stories, "100"), bank_paid=lambda r: False)
    t = SureTxn("c1", date(2026, 10, 4), -6500, "Transfer from Venmo - VENMO CASHOUT", None, "a", "Chk", None)
    result = plan(t, p)
    assert result.action == "split"
    assert result.children == [("Venmo from Sam Roe: dinner", -10000), ("Venmo to Alex Doe: lunch", 3500)]


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
                    return self._json(200, {"data": [] if "before_id" in self.path else STORIES + CASH_OUTS})
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
    source.login({"username": "me@example.com", "password": "pw", "device_id": "TRUSTED-V-ID", "token": ""}, io)
    assert io.prompts == ["Enter the code Venmo texted you"]
    paths = [c[1].split("?")[0] for c in fake.calls]
    assert paths == ["/v1/oauth/access_token", "/v1/account/two-factor/token", "/v1/oauth/access_token",
                     "/v1/users/devices", "/v1/account"]
    saved = source.auth_path.read_text()
    assert _json.loads(saved)["token"] == "tok-123" and "pw" not in saved
    assert oct(os.stat(source.auth_path).st_mode & 0o777) == "0o600"
    # The trusted device id is used throughout, and kept for later.
    assert {c[3].get("device-id") for c in fake.calls} == {"TRUSTED-V-ID"}
    assert _json.loads(saved)["device_id"] == "TRUSTED-V-ID"


def test_wrong_password_is_reported(venmo):
    _, source = venmo
    with pytest.raises(venmo_api.VenmoAuthError, match="password is incorrect"):
        source.login({"username": "me@example.com", "password": "nope", "device_id": "V", "token": ""}, CodeIO("1"))
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
    pull = lambda tid, d, cents: SureTxn(tid, d, cents, "VENMO PAYMENT", None, "a", "Chk", None)  # noqa: E731
    bank = [pull("b1", date(2026, 10, 2), 2150), pull("b4", date(2026, 10, 8), 8800)]
    got = {p.external_id: p for p in source.fetch(date(2026, 9, 20), txns=bank)}
    assert got["1"].amount_cents == 2150 and got["1"].description == "Venmo to Alex Doe: pizza"
    assert got["4"].amount_cents == 8800 and got["4"].description == "Venmo to Sam Roe: utilities"
    assert got["901"].amount_cents == -4000 and parts(got["901"]) == [("Venmo from Sam Roe: dinner", -4000)]
    assert parts(got["902"]) == [("Venmo from Alex Doe: tickets", -2500)]
    assert "2" not in got and "3" not in got  # money in only shows up via a cash-out


def test_a_rejected_token_means_sign_in_again(venmo):
    fake, source = venmo
    source.login({"token": "tok-123"}, None)
    fake.token = "revoked-now"
    with pytest.raises(LoginRequired, match="rejected the token"):
        source.fetch(date(2026, 9, 20))


def test_no_password_sign_in_without_a_trusted_device(venmo):
    fake, source = venmo
    with pytest.raises(ValueError, match="device it already trusts"):
        source.login({"username": "me@example.com", "password": "pw", "device_id": "", "token": ""}, CodeIO("1"))
    assert fake.calls == []  # never even tried: failed attempts can lock the account


def test_connect_page_links_the_venmo_guide(tmp_path):
    import urllib.request

    from itemizer.config import Config
    from itemizer.notify import Notifier
    from itemizer.runner import Runner
    from itemizer.state import State
    from itemizer.web import serve

    source = VenmoSource(tmp_path / "venmo")
    state = State(tmp_path / "s.db")
    cfg = Config(sure_url="http://sure", sure_api_key="k", dry_run=True, lookback_days=30, run_times=("07:30",),
                 run_on_start=False, web_port=0, data_dir=tmp_path, sources=("venmo",))
    server = serve(0, state, Runner(cfg, None, [source], state), Notifier(None, None, state), [source], True)
    page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/connect/venmo").read().decode()
    assert 'href="https://github.com/CharlesZ54/sure-itemizer/blob/main/docs/venmo.md"' in page
    assert 'name="device_id"' in page and 'name="token"' in page
    server.shutdown()


def test_a_cash_out_just_inside_the_lookback_still_names_earlier_payments(venmo):
    _, source = venmo
    source.login({"token": "tok-123"}, None)
    got = {p.external_id: p for p in source.fetch(date(2026, 10, 4))}
    assert {"902"} <= set(got) and "901" not in got
    assert parts(got["902"]) == [("Venmo from Alex Doe: tickets", -2500)]  # paid 10-03, before the lookback


def test_transaction_list_shows_the_source_and_filters_every_column(tmp_path):
    import urllib.request

    from itemizer.config import Config
    from itemizer.models import Plan
    from itemizer.notify import Notifier
    from itemizer.runner import Runner
    from itemizer.state import State
    from itemizer.web import serve

    source = VenmoSource(tmp_path / "venmo")
    state = State(tmp_path / "s.db")
    t = SureTxn("t1", date(2026, 10, 2), 1234, "VENMO PAYMENT", None, "a", "Checking", None)
    state.record(t, "venmo", "planned", Plan(t, None, "rename", name="Venmo to Alex Doe: pizza"), reason="dry run")
    cfg = Config(sure_url="http://sure", sure_api_key="k", dry_run=True, lookback_days=30, run_times=("07:30",),
                 run_on_start=False, web_port=0, data_dir=tmp_path, sources=("venmo",))
    server = serve(0, state, Runner(cfg, None, [source], state), Notifier(None, None, state), [source], True)
    page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/").read().decode()
    server.shutdown()
    assert "<th>Source</th>" in page and "<td data-v='venmo'>Venmo</td>" in page
    for name in ("date", "source", "account", "name", "amount", "status", "what"):
        assert f'name="{name}"' in page
    assert '<option value="venmo">Venmo</option>' in page and '<option value="checking">Checking</option>' in page

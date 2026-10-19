import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from itemizer.matcher import match
from itemizer.models import SureTxn
from itemizer.planner import plan
from itemizer.sources import ebay_api
from itemizer.sources.base import LoginRequired
from itemizer.sources.ebay import EbaySource, parse_order, purchases_from
from itemizer.sources.ebay_api import EbayAuthError, code_from

# Shaped like GetOrders output; items, people and numbers are made up.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def order_xml(oid, paid, items, amount, refunds=()):
    txns = "".join(f"<Transaction><Item><Title>{t}</Title></Item><QuantityPurchased>{q}</QuantityPurchased>"
                   f"<TransactionPrice currencyID='USD'>{p}</TransactionPrice></Transaction>" for t, q, p in items)
    refs = "".join(f"<Refund><RefundStatus>Successful</RefundStatus><RefundTime>{iso(w)}</RefundTime>"
                   f"<RefundAmount currencyID='USD'>{a}</RefundAmount><ReferenceID>R{i}</ReferenceID></Refund>"
                   for i, (w, a) in enumerate(refunds))
    return (f"<Order><OrderID>{oid}</OrderID><AmountPaid currencyID='USD'>{amount}</AmountPaid>"
            f"<PaidTime>{iso(paid)}</PaidTime><TransactionArray>{txns}</TransactionArray>"
            f"<MonetaryDetails><Refunds>{refs}</Refunds></MonetaryDetails></Order>")


def parse(xml):
    return parse_order(ET.fromstring(xml.replace("<Order>", '<Order xmlns="urn:ebay:apis:eBLBaseComponents">', 1)))


PAID = NOW - timedelta(days=10)
ORDERS = [
    # Two items, with shipping and tax on top: 20.00 + 2 x 5.00 = 30.00 of items, 34.50 charged.
    order_xml("11-11111-11111", PAID, [("USB cable", 1, "20.00"), ("Phone case", 2, "5.00")], "34.50",
              refunds=[(PAID + timedelta(days=4), "11.50")]),
    order_xml("22-22222-22222", PAID + timedelta(days=1), [("Lens cap", 1, "12.00")], "12.99",
              refunds=[(PAID + timedelta(days=6), "12.99")]),
]


def test_orders_become_a_charge_split_by_item_and_refunds():
    got = {p.external_id: p for p in purchases_from([parse(x) for x in ORDERS])}
    charge = got["11-11111-11111"]
    assert charge.amount_cents == 3450 and charge.reference == "11-11111-11111"
    assert [(line.name, line.weight_cents, line.quantity) for line in charge.lines] == [
        ("USB cable", 2000, 1), ("Phone case", 1000, 2)]
    # Partial refund on a two-item order: eBay doesn't say which item, so only a name.
    partial = got["11-11111-11111:refund:R0"]
    assert partial.amount_cents == -1150 and partial.description == "eBay refund: USB cable, Phone case"
    # A single-item order's refund splits like the charge.
    full = got["22-22222-22222:refund:R0"]
    assert full.amount_cents == -1299 and [line.name for line in full.lines] == ["Lens cap"]


def test_the_split_spreads_shipping_and_tax_by_price():
    p = {p.external_id: p for p in purchases_from([parse(x) for x in ORDERS])}["11-11111-11111"]
    t = SureTxn("t1", p.date, 3450, "eBay - Ebay O*11-11111-11111", None, "a", "Card", None)
    assert plan(t, p).children == [("USB cable", 2300), ("Phone case (x2)", 1150)]


def test_the_order_number_in_the_bank_line_wins_over_a_closer_date():
    a = parse(order_xml("33-33333-33333", PAID, [("A", 1, "10.00")], "10.00"))
    b = parse(order_xml("44-44444-44444", PAID + timedelta(days=2), [("B", 1, "10.00")], "10.00"))
    purchases = purchases_from([a, b])
    t = SureTxn("t1", (PAID + timedelta(days=2)).date(), 1000, "eBay - Ebay O*33-33333-33333", None, "a", "Card", None)
    assert [p.external_id for _, p in match([t], purchases, (3, 5))] == ["33-33333-33333"]


def test_code_is_read_from_the_address_eBay_sends_you_to():
    url = ("https://signin.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true"
           "&state=s1&code=v%5E1.1%23i%5E1&expires_in=299")
    assert code_from(url, "s1") == "v^1.1#i^1"
    assert code_from("v^1.1#i^1") == "v^1.1#i^1"
    with pytest.raises(EbayAuthError, match="different sign-in"):
        code_from(url, "other")
    with pytest.raises(EbayAuthError, match="declined"):
        code_from("https://signin.ebay.com/x?isAuthSuccessful=false&code=1")


class FakeEbay:
    """eBay's token endpoint and Trading API."""

    def __init__(self):
        self.calls, self.refresh = [], "refresh-1"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fake.calls.append((self.path, dict(self.headers), body))
                if self.path == "/token":
                    form = {k: v[0] for k, v in parse_qs(body).items()}
                    if self.headers.get("Authorization") != "Basic Y2lkOnNlY3JldA==":  # cid:secret
                        return self._send(401, '{"error":"invalid_client","error_description":"client '
                                               'authentication failed"}', "application/json")
                    ok = (form.get("grant_type") == "authorization_code" and form.get("code") == "good-code"
                          and form.get("redirect_uri") == "My-App-PRD-runame") or \
                         (form.get("grant_type") == "refresh_token" and form.get("refresh_token") == fake.refresh)
                    if not ok:
                        return self._send(400, '{"error":"invalid_grant","error_description":"the provided '
                                               'authorization grant is invalid"}', "application/json")
                    return self._send(200, '{"access_token":"acc","expires_in":7200,"refresh_token":"refresh-1",'
                                           '"refresh_token_expires_in":47304000}', "application/json")
                call = self.headers.get("X-EBAY-API-CALL-NAME")
                ns = 'xmlns="urn:ebay:apis:eBLBaseComponents"'
                if self.headers.get("X-EBAY-API-IAF-TOKEN") != "acc":
                    return self._send(200, f"<{call}Response {ns}><Ack>Failure</Ack><Errors><ErrorCode>21917053"
                                           f"</ErrorCode><LongMessage>Invalid token</LongMessage></Errors>"
                                           f"</{call}Response>", "text/xml")
                if call == "GetUser":
                    return self._send(200, f"<GetUserResponse {ns}><Ack>Success</Ack><User><UserID>pat_example"
                                           f"</UserID></User></GetUserResponse>", "text/xml")
                assert "<OrderRole>Buyer</OrderRole>" in body
                return self._send(200, f"<GetOrdersResponse {ns}><Ack>Success</Ack><OrderArray>{''.join(ORDERS)}"
                                       f"</OrderArray><HasMoreOrders>false</HasMoreOrders></GetOrdersResponse>",
                                  "text/xml")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"


class PasteIO:
    def __init__(self, answer):
        self.answer, self.links = answer, []

    def prompt(self, msg, type=None, **kw):
        self.links.append(kw.get("link"))
        state = parse_qs(urlparse(kw["link"]).query)["state"][0]
        return self.answer.replace("STATE", state)


@pytest.fixture
def ebay(monkeypatch, tmp_path):
    fake = FakeEbay()
    monkeypatch.setattr(ebay_api, "TOKEN_URL", fake.base + "/token")
    monkeypatch.setattr(ebay_api, "TRADING_URL", fake.base + "/ws/api.dll")
    return fake, EbaySource(tmp_path / "ebay")


KEYS = {"client_id": "cid", "client_secret": "secret", "runame": "My-App-PRD-runame"}
LANDED = "https://signin.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true" \
         "&state=STATE&code=good-code&expires_in=299"


def test_connect_saves_the_keys_and_refresh_token_only(ebay):
    fake, source = ebay
    io = PasteIO(LANDED)
    source.login(KEYS, io)
    link = urlparse(io.links[0])
    assert link.netloc == "auth.ebay.com"
    assert parse_qs(link.query)["redirect_uri"] == ["My-App-PRD-runame"]
    saved = source._saved()
    assert saved["refresh_token"] == "refresh-1" and saved["client_secret"] == "secret"
    assert "acc" not in source.auth_path.read_text()  # short-lived access tokens aren't stored
    assert oct(source.auth_path.stat().st_mode & 0o777) == "0o600"


def test_reconnecting_can_reuse_the_saved_keys(ebay):
    _, source = ebay
    source.login(KEYS, PasteIO(LANDED))
    source.login({"client_id": "", "client_secret": "", "runame": ""}, PasteIO(LANDED))
    assert source.connected()


def test_missing_keys_are_asked_for_before_going_to_eBay(ebay):
    fake, source = ebay
    with pytest.raises(ValueError, match="Cert ID"):
        source.login({"client_id": "cid", "client_secret": "", "runame": "r"}, PasteIO(LANDED))
    assert fake.calls == []


def test_wrong_secret_is_reported(ebay):
    _, source = ebay
    with pytest.raises(EbayAuthError, match="client authentication failed"):
        source.login({**KEYS, "client_secret": "nope"}, PasteIO(LANDED))
    assert not source.connected()


def test_fetch_reads_buyer_orders(ebay):
    fake, source = ebay
    source.login(KEYS, PasteIO(LANDED))
    got = {p.external_id for p in source.fetch((NOW - timedelta(days=30)).date())}
    assert {"11-11111-11111", "22-22222-22222", "22-22222-22222:refund:R0"} <= got


def test_an_expired_refresh_token_means_sign_in_again(ebay):
    fake, source = ebay
    source.login(KEYS, PasteIO(LANDED))
    fake.refresh = "rotated"
    with pytest.raises(LoginRequired, match="invalid"):
        source.fetch(date.today() - timedelta(days=30))

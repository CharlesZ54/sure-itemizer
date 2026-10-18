"""End-to-end through Runner with a fake Sure API and a fake source."""
import json
import re
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from itemizer.config import Config
from itemizer.models import Line, Purchase
from itemizer.runner import Runner
from itemizer.sources.base import Source
from itemizer.state import State
from itemizer.sure import SureClient

TODAY = date.today()


class FakeSure:
    """Just enough of Sure's /api/v1 to exercise the client."""

    def __init__(self, supports_splits=True, missing_route_body=b'{"status":404,"error":"Not Found"}'):
        self.supports_splits = supports_splits
        # What Rails returns for a route that doesn't exist (JSON when JSON is asked for).
        self.missing_route_body = missing_route_body
        self.txns = {}
        self.split = {}
        self.calls = []

    def add(self, id, days_ago, cents, name, classification="expense", notes=None):
        self.txns[id] = {
            "id": id, "date": (TODAY - timedelta(days=days_ago)).isoformat(), "amount_cents": abs(cents),
            "classification": classification, "name": name, "notes": notes,
            "account": {"id": "acc", "name": "Example Rewards Card (0000)"}, "category": None,
        }

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                assert self.headers["X-Api-Key"] == "k"
                if self.path.startswith("/api/v1/transactions?"):
                    return self._json(200, {"transactions": list(fake.txns.values()),
                                            "pagination": {"page": 1, "per_page": 100, "total_count": 1,
                                                           "total_pages": 1}})
                m = re.match(r"/api/v1/transactions/([^/]+)/split$", self.path)
                if m and not fake.supports_splits:
                    self.send_response(404)
                    self.end_headers()
                    return self.wfile.write(fake.missing_route_body)
                if m:
                    if m[1] in fake.split:
                        return self._json(200, {"children": []})
                    return self._json(404, {"error": "not_found", "message": "Transaction is not split"})
                self._json(404, {"error": "not_found"})

            def do_POST(self):
                body = self._body()
                fake.calls.append(("POST", self.path, body))
                m = re.match(r"/api/v1/transactions/([^/]+)/split$", self.path)
                fake.split[m[1]] = body["split"]["splits"]
                kids = [{"id": f"{m[1]}-c{i}"} for i in range(len(body["split"]["splits"]))]
                self._json(201, {"children": kids})

        return H


class FakeSource(Source):
    name = "amazon"
    descriptor = re.compile("amazon", re.I)
    sure_search = "amazon"

    def __init__(self, purchases):
        super().__init__()
        self.purchases = purchases
        self.fetches = 0

    def fetch(self, since, amounts=None):
        self.fetches += 1
        return self.purchases


@pytest.fixture
def env(tmp_path):
    def build(fake, purchases, dry_run=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cfg = Config(sure_url=f"http://127.0.0.1:{server.server_port}", sure_api_key="k", dry_run=dry_run,
                     lookback_days=30, run_times=("07:30",), run_on_start=False, web_port=0,
                     data_dir=tmp_path, sources=("amazon",))
        source = FakeSource(purchases)
        state = State(tmp_path / "t.db")
        return Runner(cfg, SureClient(cfg.sure_url, "k"), [source], state), source, state

    return build


def p(id, days_ago, cents, *lines):
    return Purchase(source="amazon", external_id=id, date=TODAY - timedelta(days=days_ago), amount_cents=cents,
                    lines=[Line(n, w) for n, w in lines], reference="112-1")


def test_live_run_splits_and_is_idempotent(env):
    fake = FakeSure()
    fake.add("multi", 2, 3075, "Amazon - AMAZON MKTPL*EXAMPLE01")
    fake.add("single", 3, 4500, "Amazon - AMAZON.COM*EXAMPLE02")
    fake.add("other", 3, 999, "Grocery store")
    fake.add("lonely", 4, 1234, "Amazon - AMAZON MKTPL*EXAMPLE03")
    runner, source, state = env(fake, [p("a", 2, 3075, ("Cable", 2000), ("Soap", 900)),
                                       p("b", 3, 4500, ("Headphones", 4500))])

    summary = runner.run()
    assert summary["applied"] == 2 and summary["unmatched"] == 1
    assert fake.split["multi"] == [{"name": "Cable", "amount": "21.21"}, {"name": "Soap", "amount": "9.54"}]
    assert fake.split["single"] == [{"name": "Headphones", "amount": "45.00"}]
    assert "other" not in {c[1].split("/")[4] for c in fake.calls if c[0] != "GET"}

    # Second run: finished transactions and the children we created are left alone.
    fake.add("multi-c0", 2, 2121, "Amazon basics cable")  # a child that happens to match the descriptor
    writes_before = len([c for c in fake.calls if c[0] != "GET"])
    runner.run()
    assert len([c for c in fake.calls if c[0] != "GET"]) == writes_before
    statuses = {r["txn_id"]: r["status"] for r in state.plans()}
    assert statuses["multi"] == "applied" and statuses["lonely"] == "unmatched"


def test_dry_run_writes_nothing(env):
    fake = FakeSure()
    fake.add("multi", 2, 3075, "Amazon - AMAZON MKTPL*EXAMPLE01")
    fake.add("single", 3, 4500, "Amazon - AMAZON.COM*EXAMPLE02")
    runner, _, state = env(fake, [p("a", 2, 3075, ("Cable", 2000), ("Soap", 900)),
                                  p("b", 3, 4500, ("Headphones", 4500))], dry_run=True)
    assert runner.run()["planned"] == 2
    assert [c for c in fake.calls if c[0] != "GET"] == []


@pytest.mark.parametrize("body", [b'{"status":404,"error":"Not Found"}', b"<html>Not Found</html>"])
def test_sure_without_split_api_defers(env, body):
    fake = FakeSure(supports_splits=False, missing_route_body=body)
    fake.add("multi", 2, 3075, "Amazon - AMAZON MKTPL*EXAMPLE01")
    fake.add("single", 3, 4500, "Amazon - AMAZON.COM*EXAMPLE02")
    runner, _, state = env(fake, [p("a", 2, 3075, ("Cable", 2000), ("Soap", 900)),
                                  p("b", 3, 4500, ("Headphones", 4500))])
    summary = runner.run()
    assert summary["deferred"] == 2
    assert [c for c in fake.calls if c[0] != "GET"] == []


def test_already_split_in_sure_is_respected(env):
    fake = FakeSure()
    fake.add("multi", 2, 3075, "Amazon - AMAZON MKTPL*EXAMPLE01")
    fake.split["multi"] = [{"name": "done by hand"}]
    runner, _, _ = env(fake, [p("a", 2, 3075, ("Cable", 2000), ("Soap", 900))])
    assert runner.run()["already_split"] == 1
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_no_candidates_means_no_merchant_login(env):
    fake = FakeSure()
    fake.add("other", 3, 999, "Grocery store")
    runner, source, _ = env(fake, [])
    runner.run()
    assert source.fetches == 0

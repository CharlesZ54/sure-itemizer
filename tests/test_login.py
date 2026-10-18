"""Interactive login, login-required handling and webhook notifications."""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from itemizer.auth import LoginFlow
from itemizer.config import Config
from itemizer.notify import Notifier
from itemizer.runner import Runner, login_flag
from itemizer.sources.amazon import AmazonSource
from itemizer.sources.base import LoginRequired, Source
from itemizer.state import State
from itemizer.web import serve


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


class Webhook:
    """Collects POSTed JSON."""

    def __init__(self):
        self.calls = []
        hook = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                hook.calls.append(json.loads(self.rfile.read(n)))
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"


class OtpSource(Source):
    """Pretends to be a merchant that asks for a 6-digit code after the password."""

    name = "shop"
    title = "Shop"
    descriptor = re.compile("shop", re.I)
    sure_search = "shop"
    login_fields = [("email", "Email", "email"), ("password", "Password", "password")]

    def __init__(self):
        super().__init__()
        self.session = None
        self.expired = False

    def connected(self):
        return self.session is not None

    def login(self, fields, io):
        if fields["password"] != "hunter2":
            raise RuntimeError("wrong password")
        device = io.prompt("Choose where you would like your one-time passcode sent", type=int,
                           choices=["1: Authenticator app", "2: SMS"])
        code = io.prompt("Enter the one-time passcode")
        if (device, code) != (1, "123456"):
            raise RuntimeError("bad code")
        self.session = "cookie"

    def logout(self):
        self.session = None

    def fetch(self, since, amounts=None):
        if self.session is None or self.expired:
            raise LoginRequired("session expired")
        return []


class FakeSure:
    def transactions(self, since, search=None):
        from datetime import date

        from itemizer.models import SureTxn
        return [SureTxn("t1", date.today(), 1000, "SHOP #1", None, "acc", "Card", None)]


def make_runner(tmp_path, source, webhook_url=None):
    cfg = Config(sure_url="http://sure", sure_api_key="k", dry_run=True, lookback_days=30,
                 run_times=("07:30",), run_on_start=False, web_port=0, data_dir=tmp_path, sources=("shop",))
    state = State(tmp_path / "t.db")
    notifier = Notifier(webhook_url, "http://nas:8686", state)
    return Runner(cfg, FakeSure(), [source], state, notifier), state, notifier


class TestLoginFlow:
    def test_prompts_are_relayed_and_answered(self):
        flow, source = LoginFlow(), OtpSource()
        flow.start(lambda io: source.login({"email": "a@b.c", "password": "hunter2"}, io))
        snap = wait_for(lambda: (s := flow.snapshot())["state"] == "need_input" and s)
        assert snap["prompt"]["choices"] == ["1: Authenticator app", "2: SMS"]
        flow.answer("1")
        wait_for(lambda: flow.snapshot()["prompt"] and "passcode" in flow.snapshot()["prompt"]["message"]
                 and flow.snapshot()["prompt"]["choices"] == [])
        flow.answer(" 123456 ")
        wait_for(lambda: flow.snapshot()["state"] == "done")
        assert source.connected()

    def test_failure_is_reported(self):
        flow = LoginFlow()
        flow.start(lambda io: OtpSource().login({"email": "a", "password": "nope"}, io))
        snap = wait_for(lambda: (s := flow.snapshot())["state"] == "failed" and s)
        assert snap["error"] == "wrong password"

    def test_cancel(self):
        flow = LoginFlow()
        flow.start(lambda io: OtpSource().login({"email": "a", "password": "hunter2"}, io))
        wait_for(lambda: flow.snapshot()["state"] == "need_input")
        flow.cancel()
        assert wait_for(lambda: (s := flow.snapshot())["state"] == "failed" and s)["error"] == "login cancelled"

    def test_only_one_login_at_a_time(self):
        flow = LoginFlow()
        flow.start(lambda io: OtpSource().login({"email": "a", "password": "hunter2"}, io))
        wait_for(lambda: flow.snapshot()["state"] == "need_input")
        with pytest.raises(RuntimeError):
            flow.start(lambda io: None)
        flow.cancel()


class TestLoginRequired:
    def test_notifies_once_per_expiry_and_again_after_reconnect(self, tmp_path):
        hook, source = Webhook(), OtpSource()
        runner, state, notifier = make_runner(tmp_path, source, hook.url)

        assert runner.run()["shop_login_required"] == 1
        runner.run()
        assert len(hook.calls) == 1
        call = hook.calls[0]
        assert call["event"] == "login_required" and call["source"] == "shop"
        assert "http://nas:8686" in call["content"] and call["content"] == call["text"]

        # Reconnecting clears the flag; the next expiry notifies again.
        source.session = "cookie"
        notifier.clear(login_flag(source))
        runner.run()
        source.expired = True
        runner.run()
        assert len(hook.calls) == 2

    def test_without_webhook_it_just_records(self, tmp_path):
        runner, state, _ = make_runner(tmp_path, OtpSource())
        runner.run()
        assert state.flag("login_required:shop")

    def test_failed_webhook_is_retried_next_run(self, tmp_path):
        runner, state, _ = make_runner(tmp_path, OtpSource(), "http://127.0.0.1:9/nothing-listens")
        runner.run()
        assert not state.flag("login_required:shop")


class TestConnectPage:
    def post(self, url, data):
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_browser_login_end_to_end(self, tmp_path):
        source = OtpSource()
        source.session = "expired cookie"  # connected once, now expired
        runner, state, notifier = make_runner(tmp_path, source)
        state.set_flag(login_flag(source))
        server = serve(0, state, runner, notifier, [source], dry_run=True)
        base = f"http://127.0.0.1:{server.server_port}"

        home = urllib.request.urlopen(base + "/").read().decode()
        assert "Sign-in needed" in home and 'href="connect/shop"' in home
        form = urllib.request.urlopen(base + "/connect/shop").read().decode()
        assert 'type="password"' in form

        self.post(base + "/connect/shop", {"email": "me@example.com", "password": "hunter2"})
        state_url = base + "/connect/shop/state"
        get = lambda: json.loads(urllib.request.urlopen(state_url).read())
        wait_for(lambda: get()["state"] == "need_input")
        assert self.post(base + "/connect/shop/answer", {"value": "1"}) == 204
        wait_for(lambda: get()["prompt"] and get()["prompt"]["choices"] == [])
        self.post(base + "/connect/shop/answer", {"value": "123456"})
        wait_for(lambda: get()["state"] == "done")

        assert source.connected() and not state.flag(login_flag(source))
        assert "hunter2" not in (tmp_path / "t.db").read_bytes().decode(errors="ignore")
        server.shutdown()

    def test_cross_origin_posts_are_refused(self, tmp_path):
        source = OtpSource()
        runner, state, notifier = make_runner(tmp_path, source)
        server = serve(0, state, runner, notifier, [source], dry_run=True)
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/disconnect/shop", data=b"",
                                     method="POST", headers={"Origin": "http://evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        server.shutdown()


class TestAmazonSession:
    def test_never_signs_in_unattended(self, tmp_path):
        source = AmazonSource(tmp_path / "amazon")
        assert not source.connected()
        with pytest.raises(LoginRequired, match="isn't connected"):
            source.fetch(__import__("datetime").date.today())

    def test_logout_removes_saved_session(self, tmp_path):
        source = AmazonSource(tmp_path / "amazon")
        source.data_dir.mkdir(parents=True)
        source.cookie_path.write_text("{}")
        assert source.connected()
        source.logout()
        assert not source.cookie_path.exists()

    def test_requires_email_and_password(self, tmp_path):
        with pytest.raises(ValueError):
            AmazonSource(tmp_path / "amazon").login({"email": "", "password": ""}, None)




class LiveSource(OtpSource):
    name, title = "live", "Live Shop"
    login_fields = []
    live_url = "http://127.0.0.1:9/never-opened"


class TestLiveViewRoutes:
    def test_card_links_to_live_view_and_page_renders(self, tmp_path):
        source = LiveSource()
        runner, state, notifier = make_runner(tmp_path, source)
        server = serve(0, state, runner, notifier, [source], dry_run=True)
        base = f"http://127.0.0.1:{server.server_port}"
        home = urllib.request.urlopen(base + "/").read().decode()
        assert 'href="connect/live/live"' in home
        live_page = urllib.request.urlopen(base + "/connect/live/live").read().decode()
        assert "I&#x27;m signed in" in live_page or "I'm signed in" in live_page
        snap = json.loads(urllib.request.urlopen(base + "/connect/live/live/state").read())
        assert snap["state"] == "idle"
        assert urllib.request.urlopen(base + "/connect/live/live/frame.jpg").status == 204
        server.shutdown()

    def test_password_sources_link_to_the_live_fallback(self, tmp_path):
        source = OtpSource()
        source.live_url = "http://127.0.0.1:9/never-opened"
        runner, state, notifier = make_runner(tmp_path, source)
        server = serve(0, state, runner, notifier, [source], dry_run=True)
        page_ = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/connect/shop").read().decode()
        assert 'href="shop/live"' in page_
        server.shutdown()


class TestTargetSource:
    def test_claims_target_debit_pulls_only(self, tmp_path):
        from datetime import date

        from itemizer.models import SureTxn
        from itemizer.sources.target import TargetSource

        t = TargetSource(tmp_path / "target")
        txn = lambda name: SureTxn("x", date.today(), 100, name, None, "acc", "Checking", None)
        assert t.claims(txn("Target - ACH: TARGET DEBIT CRD"))
        assert not t.claims(txn("Targeted Ads LLC"))

    def test_not_connected_until_a_session_is_saved(self, tmp_path):
        from datetime import date

        from itemizer.sources.target import TargetSource

        t = TargetSource(tmp_path / "target")
        assert not t.connected()
        with pytest.raises(LoginRequired):
            t.fetch(date.today())


def test_trailing_slash_addresses_redirect_to_the_canonical_page(tmp_path):
    source = OtpSource()
    runner, state, notifier = make_runner(tmp_path, source)
    server = serve(0, state, runner, notifier, [source], dry_run=True)

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoFollow)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(f"http://127.0.0.1:{server.server_port}/connect/shop/?x=1")
    assert e.value.code == 303 and e.value.headers["Location"] == "/connect/shop?x=1"
    server.shutdown()

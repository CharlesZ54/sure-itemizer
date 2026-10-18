"""TargetSignIn against a fake Target in real headless Chromium.

Skipped where Chromium isn't installed (it runs in CI and in the image).
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from itemizer.auth import LoginFlow  # noqa: E402
from itemizer.livebrowser import VIEWPORT, LiveBrowser  # noqa: E402
from itemizer.sources.target_browser import SignInError, TargetSignIn  # noqa: E402

try:
    with sync_playwright() as _pw:
        _pw.chromium.launch(headless=True, channel="chromium").close()
except Exception:  # pragma: no cover
    pytest.skip("Chromium isn't installed here", allow_module_level=True)


def header(signed_in=False):
    label = "Account, Pat" if signed_in else "Account, sign in"
    return (f'<a id="account-sign-in" aria-label="{label}" href="#" '
            f'onclick="document.getElementById(\'panel\').style.display=\'block\';return false">Account</a>'
            '<div id="panel" style="display:none"><button onclick="location.href=\'/login\'">'
            'Sign in or create account</button></div>')


# The "Press & hold" check clears after the button is held for 0.8 s.
HUMAN_CHECK = """<div id="px-captcha-modal" style="position:fixed;left:0;top:0;width:100%;height:100%;background:#eee">
  <button id="hold" style="position:absolute;left:400px;top:400px;width:200px;height:50px">Press &amp; hold</button>
</div><script>
  let t; const b = document.getElementById('hold');
  b.addEventListener('mousedown', () => { t = setTimeout(() => document.getElementById('px-captcha-modal').remove(), 800); });
  b.addEventListener('mouseup', () => clearTimeout(t));
</script>"""


class FakeTarget:
    def __init__(self, human_check=False, password_offered=True):
        self.posts, self.human_check, self.password_offered = [], human_check, password_offered
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _page(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<html><body>{body}</body></html>".encode())

            def do_GET(self):
                if self.path == "/login":
                    return self._page('<form method="post" action="/email"><input id="username" name="username">'
                                      '<button type="submit">Continue</button></form>')
                if self.path == "/password":
                    return self._page('<form method="post" action="/check"><input type="password" name="password">'
                                      '<input type="checkbox" id="keepMeSignedIn" name="keepMeSignedIn">'
                                      '<button type="submit">Sign in</button></form>')
                body = header() + (HUMAN_CHECK if fake.human_check else "")
                self._page(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                fake.posts.append((self.path, form))
                if self.path == "/email":
                    pw = '<a href="/password">Enter your password</a>' if fake.password_offered else ""
                    return self._page('<p>How do you want to get your code?</p>'
                                      '<form method="post" action="/send"><button name="via" value="sms">Text me a code'
                                      '</button><button name="via" value="email">Email me a code</button></form>' + pw)
                if self.path == "/send":
                    return self._page('<form method="post" action="/check"><label>Enter your code '
                                      '<input autocomplete="one-time-code" name="code"></label>'
                                      '<button type="submit">Sign in</button></form>')
                if self.path == "/check":
                    if form.get("password") == "pw ok" or form.get("code") == "424242":
                        return self._page(header(signed_in=True))
                    return self._page('<div role="alert">That password is incorrect.</div>'
                                      '<form method="post" action="/check"><input type="password" name="password">'
                                      '<button type="submit">Sign in</button></form>')
                self._page("?")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.home = f"http://127.0.0.1:{self.server.server_port}/"


class ScriptedIO:
    def __init__(self, answers=()):
        self.answers, self.prompts = list(answers), []

    def echo(self, msg, **kw):
        pass

    def prompt(self, msg, type=None, **kw):
        self.prompts.append((msg, kw.get("choices")))
        a = self.answers.pop(0)
        return type(a) if type else a

    def handoff(self, *a, **kw):
        raise AssertionError("unexpected hand-off")


def sign_in(fake, io, password="pw ok"):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chromium")
        try:
            context = browser.new_context(viewport=VIEWPORT)
            page = context.new_page()
            TargetSignIn(io).run(context, page, "me@example.com", password, timeout=60, home=fake.home)
        finally:
            browser.close()


def test_prefers_the_password_when_offered():
    fake = FakeTarget()
    sign_in(fake, ScriptedIO())
    assert [p for p, _ in fake.posts] == ["/email", "/check"]
    assert fake.posts[0][1] == {"username": "me@example.com"}
    assert fake.posts[1][1] == {"password": "pw ok", "keepMeSignedIn": "on"}


def test_code_only_asks_where_to_send_then_for_the_code():
    fake, io = FakeTarget(password_offered=False), ScriptedIO(["1", "424 242"])
    sign_in(fake, io)
    assert io.prompts[0] == ("Where should Target send your sign-in code?", ["1: Text me a code", "2: Email me a code"])
    assert fake.posts[1] == ("/send", {"via": "sms"}) and fake.posts[2] == ("/check", {"code": "424242"})


def test_wrong_password_reports_targets_message():
    with pytest.raises(SignInError, match="That password is incorrect"):
        sign_in(FakeTarget(), ScriptedIO(), password="nope")


def test_human_check_is_handed_to_the_user_then_the_script_continues():
    fake = FakeTarget(human_check=True)
    live = LiveBrowser()
    flow = LoginFlow(live=live)

    def login(io):
        sign_in(fake, io)

    flow.start(login)
    deadline = time.time() + 30
    while flow.snapshot()["state"] != "live" and time.time() < deadline:
        time.sleep(0.1)
    assert flow.snapshot()["state"] == "live"
    assert "press and hold" in flow.snapshot()["prompt"]["message"]

    # The user presses and holds Target's button in the live view.
    while live.snapshot()["state"] != "running":
        time.sleep(0.05)
    x, y = 500 / VIEWPORT["width"], 425 / VIEWPORT["height"]
    live.send({"type": "down", "x": x, "y": y})
    time.sleep(1.5)
    live.send({"type": "up"})

    while flow.snapshot()["state"] not in ("done", "failed") and time.time() < deadline:
        time.sleep(0.1)
    assert flow.snapshot()["state"] == "done", flow.snapshot()
    assert live.snapshot()["state"] == "handed_back"
    assert [p for p, _ in fake.posts] == ["/email", "/check"]


# A check that blinks on and off by itself while it loads, then goes away.
FLICKERING_CHECK = """<div id="px-captcha-modal" style="position:fixed;left:0;top:0;width:100%;height:100%;background:#eee">
  Press &amp; hold</div><script>
  // Mostly visible, with brief blinks, for ~5 s; then gone for good.
  // (Random gaps, so the blinks don't line up with anyone's polling.)
  const m = document.getElementById('px-captcha-modal'); const end = Date.now() + 5000;
  (function blink() {
    if (Date.now() > end) { m.remove(); return; }
    m.style.display = 'none';
    setTimeout(() => { m.style.display = 'block'; setTimeout(blink, 150 + Math.random() * 250); }, 120);
  })();
</script>"""


def test_a_flickering_check_is_one_hand_off_not_a_ping_pong():
    fake = FakeTarget()
    # Serve the flickering check on the home page.
    global HUMAN_CHECK
    original, HUMAN_CHECK = HUMAN_CHECK, FLICKERING_CHECK
    fake.human_check = True
    attaches = []

    class CountingLive(LiveBrowser):
        def attach(self, *a, **kw):
            attaches.append(time.time())
            return super().attach(*a, **kw)

    try:
        flow = LoginFlow(live=CountingLive())
        flow.start(lambda io: sign_in(fake, io))
        deadline = time.time() + 45
        while flow.snapshot()["state"] not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.1)
        assert flow.snapshot()["state"] == "done", flow.snapshot()
        assert len(attaches) == 1, f"handed over {len(attaches)} times"
    finally:
        HUMAN_CHECK = original

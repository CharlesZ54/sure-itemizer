"""Drive HeadlessSignIn through fake Amazon pages in a real headless Chromium.

Skipped where Chromium isn't installed (CI); run inside the image:
  docker run --rm --entrypoint python <image> -m pytest /app/tests
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from itemizer.sources.amazon_browser import HeadlessSignIn, SignInError  # noqa: E402

try:
    with sync_playwright() as _pw:
        _pw.chromium.launch(headless=True, channel="chromium").close()
except Exception:  # pragma: no cover
    pytest.skip("Chromium isn't installed here", allow_module_level=True)

PAGES = {
    "/signin": """<form name="signIn" method="post" action="/password">
        <input type="email" name="email"><input type="submit" id="continue" value="Continue"></form>""",
    "/mfa": """<form id="auth-select-device-form" method="post" action="/otp">
        <label><input type="radio" name="otpDeviceContext" value="app"> <span>Authenticator app</span></label>
        <label><input type="radio" name="otpDeviceContext" value="sms"> <span>Text a code to ***-0000</span></label>
        <input type="submit" id="auth-send-code" value="Send code"></form>""",
}


def password_page(error=""):
    box = f'<div id="auth-error-message-box">{error}</div>' if error else ""
    return f"""{box}<form name="signIn" method="post" action="/check">
        <input type="password" name="password"><input type="checkbox" name="rememberMe">
        <input type="submit" id="signInSubmit" value="Sign in"></form>"""


OTP_PAGE = """<form id="auth-mfa-form" method="post" action="/done">
    <input name="otpCode"><input type="checkbox" id="auth-mfa-remember-device" name="rememberDevice">
    <input type="submit" id="auth-signin-button" value="Sign in"></form>"""


class FakeAmazon:
    def __init__(self):
        self.posts = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _page(self, body, cookie=None):
                data = f"<html><body>{body}</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._page(PAGES.get(self.path.split("?")[0], "home"))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                fake.posts.append((self.path, form))
                if self.path == "/password":
                    return self._page(password_page())
                if self.path == "/check":
                    if form.get("password") != "right horse battery":
                        return self._page(password_page("Your password is incorrect"))
                    return self._page(PAGES["/mfa"])
                if self.path == "/otp":
                    return self._page(OTP_PAGE)
                if self.path == "/done" and form.get("otpCode") == "123456":
                    return self._page("Hello, signed in", cookie="x-main=ok; Path=/")
                self._page("nope")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"


class ScriptedIO:
    def __init__(self, answers):
        self.answers, self.prompts, self.messages = list(answers), [], []

    def echo(self, msg, **kw):
        self.messages.append(msg)

    def prompt(self, msg, type=None, **kw):
        self.prompts.append((msg, kw.get("choices")))
        answer = self.answers.pop(0)
        return type(answer) if type else answer


class LocalSignIn(HeadlessSignIn):
    """HeadlessSignIn's driver against the fake site (no real Amazon, no session)."""

    def __init__(self, io):
        self.io = io

    def _is_signed_in(self, cookies):
        return any(c["name"] == "x-main" for c in cookies)


def drive(fake, io, password):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chromium")
        context = browser.new_context()
        page = context.new_page()
        page.goto(fake.base + "/signin")
        try:
            LocalSignIn(io)._drive(context, page, "me@example.com", password, timeout=60)
        finally:
            browser.close()


def test_full_sign_in_with_device_choice_and_code():
    fake, io = FakeAmazon(), ScriptedIO(["1", "123 456"])
    drive(fake, io, "right horse battery")
    paths = [p for p, _ in fake.posts]
    assert paths == ["/password", "/check", "/otp", "/done"]
    assert fake.posts[0][1]["email"] == "me@example.com"
    assert fake.posts[1][1] == {"password": "right horse battery", "rememberMe": "on"}
    assert fake.posts[2][1]["otpDeviceContext"] == "app"
    assert fake.posts[3][1]["otpCode"] == "123456" and fake.posts[3][1]["rememberDevice"] == "on"
    assert io.prompts[0][1] == ["1: Authenticator app", "2: Text a code to ***-0000"]


def test_wrong_password_reports_amazons_message():
    fake = FakeAmazon()
    with pytest.raises(SignInError, match="Your password is incorrect"):
        drive(fake, ScriptedIO([]), "wrong")

"""The live browser view, against a fake sign-in page in real headless Chromium.

Skipped where Chromium isn't installed.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from itemizer.livebrowser import VIEWPORT, LiveBrowser  # noqa: E402

try:
    with sync_playwright() as _pw:
        _pw.chromium.launch(headless=True, channel="chromium").close()
except Exception:  # pragma: no cover
    pytest.skip("Chromium isn't installed here", allow_module_level=True)

SIGN_IN = """<html><body style="margin:0">
<form method="post" action="/signin">
  <input name="user" style="position:absolute;left:50px;top:50px;width:300px;height:40px">
</form></body></html>"""


def fake_site():
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(SIGN_IN.encode() if self.path == "/" else b"<html>hi</html>")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.update({k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()})
            self.send_response(200)
            if got.get("user") == "me@example.com":
                self.send_header("Set-Cookie", "session=ok; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>signed in</html>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/", got


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_user_signs_in_through_the_live_view():
    url, got = fake_site()
    saved, succeeded = [], []
    live = LiveBrowser()
    live.start(url, signed_in=lambda ctx: any(c["name"] == "session" for c in ctx.cookies()),
               save=lambda ctx: saved.append([c["name"] for c in ctx.cookies()]),
               on_success=lambda: succeeded.append(True))
    wait_for(lambda: live.snapshot()["state"] == "running" and live.latest_frame())
    assert live.latest_frame()[:2] == b"\xff\xd8"  # a JPEG of the page

    # Click the field (coordinates are fractions of the viewport), type, press Enter.
    live.send({"type": "click", "x": 200 / VIEWPORT["width"], "y": 70 / VIEWPORT["height"]})
    live.send({"type": "text", "text": "me@example.com"})
    live.send({"type": "key", "key": "Enter"})

    wait_for(lambda: live.snapshot()["state"] == "done")
    assert got["user"] == "me@example.com"
    assert saved == [["session"]] and succeeded == [True]


def test_cancel_closes_without_saving():
    url, _ = fake_site()
    saved = []
    live = LiveBrowser()
    live.start(url, signed_in=lambda ctx: False, save=lambda ctx: saved.append(1))
    wait_for(lambda: live.snapshot()["state"] == "running")
    live.send({"type": "cancel"})
    wait_for(lambda: live.snapshot()["state"] == "failed")
    assert live.snapshot()["error"] == "cancelled" and not saved


def test_done_button_saves_even_without_auto_detection():
    url, _ = fake_site()
    saved = []
    live = LiveBrowser()
    live.start(url, signed_in=lambda ctx: False, save=lambda ctx: saved.append(1))
    wait_for(lambda: live.snapshot()["state"] == "running")
    live.send({"type": "done"})
    wait_for(lambda: live.snapshot()["state"] == "done")
    assert saved == [1]

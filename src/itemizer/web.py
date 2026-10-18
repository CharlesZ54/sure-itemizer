"""A small read-only status page, plus a "run now" button."""
from __future__ import annotations

import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .runner import Runner
from .state import State

log = logging.getLogger(__name__)

STATUS_ORDER = ["planned", "deferred", "applied", "already_split", "skipped", "unmatched", "error"]

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sure Itemizer</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--chip:#f2f2f7;--accent:#2f6fde}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#ececf1;--muted:#9a9aa3;--line:#2c2c31;--chip:#232328;--accent:#7aa7ff}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}}
main{{max-width:1100px;margin:auto}} h1{{font-size:20px;margin:0 0 4px}} .muted{{color:var(--muted)}}
nav a{{display:inline-block;margin:8px 6px 0 0;padding:3px 10px;border-radius:12px;background:var(--chip);color:var(--fg);text-decoration:none}}
nav a.on{{background:var(--accent);color:#fff}}
table{{width:100%;border-collapse:collapse;margin-top:14px}} th,td{{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-weight:600;color:var(--muted);font-size:12px}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
ul{{margin:0;padding-left:16px}} .wrap{{overflow-x:auto}} button{{font:inherit;padding:5px 12px;border-radius:8px;border:1px solid var(--line);background:var(--chip);color:var(--fg);cursor:pointer}}
</style></head><body><main>
<h1>Sure Itemizer</h1>
<div class="muted">{mode} · last run: {last_run}</div>
<form method="post" action="run" style="margin-top:8px"><button>Run now</button></form>
<nav>{tabs}</nav>
<div class="wrap"><table><tr><th>Date</th><th>Transaction</th><th class="n">Amount</th><th>Status</th><th>What</th></tr>{rows}</table></div>
</main></body></html>"""


def _detail(row) -> str:
    if not row["detail"]:
        return ""
    rows = json.loads(row["detail"])
    return "<ul>" + "".join(f"<li>{html.escape(c['name'])} — ${html.escape(c['amount'])}</li>" for c in rows) + "</ul>"


def make_handler(state: State, runner: Runner, dry_run: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # route through logging, quietly
            log.debug(fmt, *args)

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if url.path != "/":
                return self._send(404, "not found", "text/plain")
            status = parse_qs(url.query).get("status", [None])[0]
            counts = state.counts()
            tabs = [f'<a href="./" class="{"on" if not status else ""}">all {sum(counts.values())}</a>']
            for s in STATUS_ORDER:
                if counts.get(s):
                    tabs.append(f'<a href="?status={s}" class="{"on" if s == status else ""}">'
                                f'{s.replace("_", " ")} {counts[s]}</a>')
            rows = []
            for r in state.plans(status):
                what = _detail(r) or html.escape(r["reason"] or "")
                if r["detail"] and r["reason"]:
                    what = f'<div class="muted">{html.escape(r["reason"])}</div>{what}'
                rows.append(
                    f"<tr><td>{r['txn_date']}</td><td>{html.escape(r['txn_name'] or '')}"
                    f"<div class='muted'>{html.escape(r['reference'] or '')}</div></td>"
                    f"<td class='n'>${r['txn_amount']}</td><td>{r['status'].replace('_', ' ')}</td><td>{what}</td></tr>")
            runs = state.last_runs(1)
            last = "never"
            if runs:
                r = runs[0]
                last = f"{r['started_at']}" + (f" — failed: {html.escape(r['error'])}" if r["error"] else "")
            mode = "Dry run: nothing is written to Sure" if dry_run else "Live: writing to Sure"
            self._send(200, PAGE.format(mode=mode, last_run=last, tabs="".join(tabs),
                                        rows="".join(rows) or "<tr><td colspan=5 class='muted'>Nothing yet.</td></tr>"))

        def do_POST(self):
            if urlparse(self.path).path != "/run":
                return self._send(404, "not found", "text/plain")
            threading.Thread(target=_safe_run, args=(runner,), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "./")
            self.end_headers()

    return Handler


def _safe_run(runner: Runner) -> None:
    try:
        runner.run()
    except Exception:
        pass  # already logged and recorded by Runner.run


def serve(port: int, state: State, runner: Runner, dry_run: bool) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state, runner, dry_run))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("status page on :%d", port)
    return server

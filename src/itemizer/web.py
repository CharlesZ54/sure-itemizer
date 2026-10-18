"""Status page, merchant Connect pages, and a "run now" button."""
from __future__ import annotations

import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .auth import LoginFlow
from .livebrowser import LiveBrowser
from .notify import Notifier
from .runner import Runner, login_flag
from .sources import Source
from .state import State

log = logging.getLogger(__name__)

STATUS_ORDER = ["planned", "deferred", "applied", "already_split", "skipped", "unmatched", "error"]

STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--chip:#f2f2f7;--accent:#2f6fde;--ok:#1e8e3e;--warn:#b26a00;--bad:#c5221f}
@media (prefers-color-scheme:dark){:root{--bg:#151517;--fg:#ececf1;--muted:#9a9aa3;--line:#2c2c31;--chip:#232328;--accent:#7aa7ff;--ok:#5bd17a;--warn:#f0b450;--bad:#ff7b72}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}
main{max-width:1100px;margin:auto} h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 8px}
a{color:var(--accent)} .muted{color:var(--muted)} .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
nav a{display:inline-block;margin:8px 6px 0 0;padding:3px 10px;border-radius:12px;background:var(--chip);color:var(--fg);text-decoration:none}
nav a.on{background:var(--accent);color:#fff}
table{width:100%;border-collapse:collapse;margin-top:10px} th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px} td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
ul{margin:0;padding-left:16px} .wrap{overflow-x:auto}
button,input,select{font:inherit;color:var(--fg)}
button{padding:5px 12px;border-radius:8px;border:1px solid var(--line);background:var(--chip);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
input{padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:var(--bg);width:100%;max-width:340px}
label{display:block;margin:10px 0 4px;font-weight:600}
.card{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.card .grow{flex:1;min-width:200px} form.inline{display:inline}
.panel{border:1px solid var(--line);border-radius:12px;padding:16px;max-width:520px}
.bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:10px 0}
tr.f th{padding:2px 4px 8px} tr.f input,tr.f select{width:100%;max-width:none;min-width:70px;padding:4px 6px;font-size:12px;border-radius:6px;border:1px solid var(--line);background:var(--bg)}
#screen{max-width:1100px;border:1px solid var(--line);border-radius:8px;overflow:hidden;outline:none;background:#fff}
#screen:focus{border-color:var(--accent)} #frame{display:block;width:100%;height:auto;cursor:pointer;min-height:200px;touch-action:none;user-select:none;-webkit-user-select:none}
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{STYLE}</style></head><body><main>{body}</main></body></html>")


def _detail(row) -> str:
    if not row["detail"]:
        return ""
    detail = json.loads(row["detail"])
    if isinstance(detail, dict):  # a rename
        return f"→ <b>{html.escape(detail.get('rename', ''))}</b>"
    return "<ul>" + "".join(f"<li>{html.escape(c['name'])} — ${html.escape(c['amount'])}</li>" for c in detail) + "</ul>"


# Column filters for the transactions table: a text box matches anywhere in
# the cell, a dropdown matches the whole cell. Kept in the address bar so a
# filtered view can be reloaded or bookmarked.
FILTER_JS = """
<script>
const table = document.getElementById("txns");
const boxes = [...table.querySelectorAll("tr.f [data-col]")];
const rows = [...table.querySelectorAll("tr.r")];
const shown = document.getElementById("shown");
function apply(){
  const want = boxes.map(b => [+b.dataset.col, b.value.trim().toLowerCase(), b.tagName === "SELECT"]);
  let n = 0;
  for (const r of rows) {
    const ok = want.every(([c, v, exact]) => {
      if (!v) return true;
      const t = r.cells[c].dataset.v ?? r.cells[c].textContent.toLowerCase();
      return exact ? t === v : t.includes(v);
    });
    r.hidden = !ok; n += ok;
  }
  shown.textContent = n === rows.length ? "" : `showing ${n} of ${rows.length}`;
  const q = new URLSearchParams();
  for (const b of boxes) if (b.value) q.set(b.name, b.value);
  history.replaceState(null, "", q.toString() ? "?" + q : location.pathname);
  for (const a of document.querySelectorAll("nav a[data-status]"))
    a.classList.toggle("on", a.dataset.status === (table.querySelector("[name=status]").value));
}
const q = new URLSearchParams(location.search);
for (const b of boxes) { if (q.has(b.name)) b.value = q.get(b.name); b.addEventListener("input", apply); }
for (const a of document.querySelectorAll("nav a[data-status]")) a.addEventListener("click", e => {
  e.preventDefault(); table.querySelector("[name=status]").value = a.dataset.status; apply();
});
apply();
</script>
"""


CONNECT_JS = """
<script>
const base = location.pathname.replace(/\\/$/, "");
const box = document.getElementById("flow");
function esc(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}
async function send(path, data){
  await fetch(base + path, {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
                            body:new URLSearchParams(data)});
  poll();
}
let timer;
async function poll(){
  clearTimeout(timer);
  const s = await (await fetch(base + "/state")).json();
  let h = s.messages.map(m => `<div class="muted">${esc(m)}</div>`).join("");
  if (s.state === "running") { h += `<p>Talking to the merchant…</p>`; timer = setTimeout(poll, 1500); }
  else if (s.state === "need_input") {
    const p = s.prompt;
    h += `<form id="ans"><label>${esc(p.message)}</label>`;
    if (p.image) h += `<p><img src="${esc(p.image)}" alt="captcha" style="max-width:100%"></p>`;
    if (p.choices.length) {
      h += p.choices.map((c,i) => `<div><label style="font-weight:400"><input type="radio" name="value" value="${i+1}" ${i==0?"checked":""} style="width:auto"> ${esc(c.replace(/^\\d+:\\s*/,""))}</label></div>`).join("");
    } else {
      const otp = /passcode|code/i.test(p.message);
      h += `<input name="value" autocomplete="${otp?"one-time-code":"off"}" ${otp?'inputmode="numeric"':''} autofocus required>`;
    }
    h += `<p><button class="primary">Continue</button> <button type="button" id="cancel">Cancel</button></p></form>`;
  }
  else if (s.state === "live") {
    h += `<p>${esc(s.prompt.message)}</p><p>Opening the live view… <a href="${base}/live">open it</a></p>`;
    location.href = base + "/live";
  }
  else if (s.state === "done") { h += `<p class="ok">Connected. <a href="../">Back to status</a></p>`; }
  else if (s.state === "failed") { h += `<p class="bad">Sign-in failed: ${esc(s.error)}</p><p><a href="">Try again</a></p>`; }
  box.innerHTML = h;
  const f = document.getElementById("ans");
  if (f) {
    f.onsubmit = e => { e.preventDefault(); send("/answer", {value: new FormData(f).get("value")}); };
    document.getElementById("cancel").onclick = () => send("/cancel", {});
  }
}
poll();
</script>
"""


LIVE_JS = """
<script>
const base = location.pathname.replace(/\\/$/, "");
const img = document.getElementById("frame"), screenEl = document.getElementById("screen");
const statusEl = document.getElementById("status"), typed = document.getElementById("typed");
let shownAt = 0, finished = false;
const post = (path, body) => fetch(base + path, {method: "POST", headers: {"Content-Type": "application/json"},
                                                  body: JSON.stringify(body || {})});
const input = cmd => post("/input", cmd);
// Pointer down/up rather than click, so press-and-hold checks work (mouse and touch).
const at = e => ({x: e.offsetX / img.clientWidth, y: e.offsetY / img.clientHeight});
img.addEventListener("pointerdown", e => { e.preventDefault(); screenEl.focus(); img.setPointerCapture(e.pointerId); input({type: "down", ...at(e)}); });
img.addEventListener("pointerup", e => { e.preventDefault(); input({type: "up"}); });
img.addEventListener("pointercancel", () => input({type: "up"}));
img.addEventListener("contextmenu", e => e.preventDefault());
screenEl.addEventListener("wheel", e => { e.preventDefault(); input({type: "scroll", dy: e.deltaY}); }, {passive: false});
const SPECIAL = new Set(["Enter","Tab","Backspace","Delete","Escape","ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home","End","PageUp","PageDown"]);
screenEl.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key.length === 1) { input({type: "text", text: e.key}); e.preventDefault(); }
  else if (SPECIAL.has(e.key)) { input({type: "key", key: e.key}); e.preventDefault(); }
});
screenEl.addEventListener("paste", e => { input({type: "text", text: e.clipboardData.getData("text")}); e.preventDefault(); });
document.getElementById("send").onclick = () => { if (typed.value) input({type: "text", text: typed.value}); typed.value = ""; };
document.getElementById("enter").onclick = () => { if (typed.value) input({type: "text", text: typed.value}); typed.value = ""; input({type: "key", key: "Enter"}); };
document.getElementById("bksp").onclick = () => input({type: "key", key: "Backspace"});
document.getElementById("back").onclick = () => input({type: "back"});
document.getElementById("done").onclick = () => input({type: "done"});
document.getElementById("cancel").onclick = () => input({type: "cancel"});
async function tick() {
  if (finished) return;
  try {
    const s = await (await fetch(base + "/state")).json();
    if (s.state === "running" || s.state === "starting") {
      statusEl.textContent = s.state === "starting" ? "Starting the browser…" : (s.reason || "Click into the page to type.");
      document.getElementById("done").textContent = s.reason ? "Continue" : "I'm signed in";
      if (s.frame_at && s.frame_at !== shownAt) { shownAt = s.frame_at; img.src = base + "/frame.jpg?t=" + s.frame_at; }
    } else if (s.state === "saving") { statusEl.textContent = "Saving the session…"; }
    else if (s.state === "handed_back") { finished = true; location.href = base.replace(/\\/live$/, ""); }
    else if (s.state === "done") { finished = true; statusEl.innerHTML = '<span class="ok">Connected.</span> <a href="../../">Back to status</a>'; }
    else if (s.state === "failed") { finished = true; statusEl.innerHTML = '<span class="bad"></span> <a href="">Start again</a>'; statusEl.firstChild.textContent = s.error || "Closed"; }
  } catch (e) {}
  setTimeout(tick, 400);
}
post("/start").then(tick);
</script>
"""


def make_handler(state: State, runner: Runner, notifier: Notifier, sources: list[Source],
                 flows: dict[str, LoginFlow], dry_run: bool, setup_error: str | None,
                 lives: dict[str, LiveBrowser] | None = None):
    lives = lives or {}
    by_name = {s.name: s for s in sources}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # never log bodies; paths only, quietly
            log.debug(fmt, *args)

        def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8") -> None:
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, to: str) -> None:
            self.send_response(303)
            self.send_header("Location", to)
            self.end_headers()

        def _form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _same_origin(self) -> bool:
            # Keep other websites open in the browser from posting here.
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        # GET ------------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/" and url.path.endswith("/"):
                # One address per page: relative links and redirects assume no trailing slash.
                return self._redirect(url.path.rstrip("/") + (f"?{url.query}" if url.query else ""))
            path = url.path or "/"
            parts = path.strip("/").split("/")
            if path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if path == "/":
                return self._send(200, self._status_page())
            if len(parts) >= 2 and parts[0] == "connect" and parts[1] in by_name:
                source = by_name[parts[1]]
                if len(parts) >= 3 and parts[2] == "live" and source.name in lives:
                    live = lives[source.name]
                    if len(parts) == 3:
                        return self._send(200, self._live_page(source))
                    if parts[3] == "state":
                        return self._send(200, json.dumps(live.snapshot()), "application/json")
                    if parts[3] == "frame.jpg":
                        frame = live.latest_frame()
                        return self._send(200, frame, "image/jpeg") if frame else self._send(204, "")
                flow = flows.get(source.name)
                if flow and len(parts) == 3 and parts[2] == "state":
                    return self._send(200, json.dumps(flow.snapshot()), "application/json")
                if flow and len(parts) == 2:
                    return self._send(200, self._connect_page(source, flow))
            self._send(404, "not found", "text/plain")

        def _status_page(self) -> str:
            counts = state.counts()
            tabs = [f'<a href="./" data-status="">all {sum(counts.values())}</a>']
            for s in STATUS_ORDER:
                if counts.get(s):
                    tabs.append(f'<a href="?status={s}" data-status="{s}">{s.replace("_", " ")} {counts[s]}</a>')
            plans = state.plans(limit=5000)
            titles = {s.name: s.title or s.name for s in sources}
            rows = []
            for r in plans:
                what = _detail(r) or html.escape(r["reason"] or "")
                if r["detail"] and r["reason"]:
                    what = f'<div class="muted">{html.escape(r["reason"])}</div>{what}'
                rows.append(
                    f"<tr class='r'><td>{r['txn_date']}</td>"
                    f"<td data-v='{html.escape(r['source'])}'>{html.escape(titles.get(r['source'], r['source']))}</td>"
                    f"<td data-v='{html.escape((r['account'] or '').lower())}'>{html.escape(r['account'] or '')}</td>"
                    f"<td>{html.escape(r['txn_name'] or '')}"
                    f"<div class='muted'>{html.escape(r['reference'] or '')}</div></td>"
                    f"<td class='n'>${r['txn_amount']}</td><td data-v='{r['status']}'>{r['status'].replace('_', ' ')}</td>"
                    f"<td>{what}</td></tr>")

            def choose(name: str, col: int, values: dict[str, str]) -> str:
                options = "".join(f'<option value="{html.escape(v)}">{html.escape(label)}</option>'
                                  for v, label in sorted(values.items(), key=lambda kv: kv[1].lower()))
                return f'<th><select name="{name}" data-col="{col}" aria-label="{name}"><option value="">all</option>{options}</select></th>'

            def search(name: str, col: int, hint: str) -> str:
                return f'<th><input name="{name}" data-col="{col}" placeholder="{hint}" aria-label="{name}"></th>'

            filters = ("<tr class='f'>" + search("date", 0, "2026-10")
                       + choose("source", 1, {r["source"]: titles.get(r["source"], r["source"]) for r in plans})
                       + choose("account", 2, {(r["account"] or "").lower(): r["account"] or "(none)" for r in plans})
                       + search("name", 3, "search") + search("amount", 4, "12.34")
                       + choose("status", 5, {r["status"]: r["status"].replace("_", " ") for r in plans})
                       + search("what", 6, "search") + "</tr>") if plans else ""

            runs = state.last_runs(1)
            last = "never"
            if runs:
                r = runs[0]
                last = html.escape(r["started_at"])
                if r["error"]:
                    last += f' — <span class="bad">failed: {html.escape(r["error"])}</span>'

            cards = []
            for s in sources:
                if not (s.login_fields or s.name in lives):
                    continue
                if not s.connected():
                    badge, btn = '<span class="muted">Not connected</span>', "Connect"
                elif state.flag(login_flag(s)):
                    badge, btn = '<span class="warn">Sign-in needed</span>', "Reconnect"
                else:
                    badge, btn = '<span class="ok">Connected</span>', "Reconnect"
                disconnect = (f'<form class="inline" method="post" action="disconnect/{s.name}">'
                              f'<button>Disconnect</button></form>') if s.connected() else ""
                cards.append(f'<div class="card"><div class="grow"><b>{html.escape(s.title or s.name)}</b> · {badge}</div>'
                             f'<a href="connect/{s.name}{"" if s.login_fields else "/live"}"><button class="primary">{btn}</button></a> {disconnect}</div>')
            apprise_state = "configured" if notifier.configured else "not configured (set APPRISE_URL)"
            cards.append(f'<div class="card"><div class="grow"><b>Notifications</b> · '
                         f'<span class="muted">Apprise {apprise_state}</span></div>'
                         f'<form class="inline" method="post" action="notify/test"><button>Send test</button></form></div>')

            mode = "Dry run: nothing is written to Sure" if dry_run else "Live: writing to Sure"
            setup = f'<p class="bad">Setup problem: {html.escape(setup_error)}</p>' if setup_error else ""
            body = (f"<h1>Sure Itemizer</h1><div class='muted'>{mode} · last run: {last}</div>{setup}"
                    f'<form method="post" action="run" style="margin-top:8px"><button>Run now</button></form>'
                    f"<h2>Integrations</h2>{''.join(cards)}"
                    f"<h2>Transactions <span id='shown' class='muted'></span></h2><nav>{''.join(tabs)}</nav>"
                    f"<div class='wrap'><table id='txns'><tr><th>Date</th><th>Source</th><th>Account</th>"
                    f"<th>Transaction</th><th class='n'>Amount</th><th>Status</th><th>What</th></tr>{filters}"
                    f"{''.join(rows) or '<tr><td colspan=7 class=muted>Nothing yet.</td></tr>'}</table></div>"
                    f"{FILTER_JS if plans else ''}")
            return page("Sure Itemizer", body)

        def _connect_page(self, source: Source, flow: LoginFlow) -> str:
            title = html.escape(source.title or source.name)
            snap = flow.snapshot()
            if snap["state"] in ("running", "need_input"):
                inner = '<div id="flow"></div>' + CONNECT_JS
            else:
                def field(f) -> str:
                    n, label, t = f[:3]
                    optional = len(f) > 3 and f[3]
                    auto = "username" if t == "email" else "current-password" if t == "password" else "off"
                    return (f'<label for="{n}">{html.escape(label)}</label>'
                            f'<input id="{n}" name="{n}" type="{t}" {"" if optional else "required"} '
                            f'autocomplete="{auto}">')

                fields = "".join(field(f) for f in source.login_fields)
                if source.help_url:
                    fields = (f'<p><a href="{html.escape(source.help_url)}" target="_blank" rel="noopener">'
                              f'How to get these →</a></p>') + fields
                inner = (f'<form method="post">{fields}<p><button class="primary">Sign in</button></p></form>'
                         f'<p class="muted">Used once to sign in, then discarded. Only the session {title} issues '
                         f'is kept. If {title} asks for a verification code, you\'ll be prompted here.</p>')
                if source.name in lives:
                    inner += (f'<p><a href="{source.name}/live">Or sign in yourself on {title}\'s own page, '
                              f'in a live browser view →</a></p>')
            return page(f"Connect {title}", f'<p><a href="../">← Status</a></p><h1>Connect {title}</h1>'
                                            f'<div class="panel">{inner}</div>')

        def _live_page(self, source: Source) -> str:
            title = html.escape(source.title or source.name)
            body = (f'<p><a href="../../">← Status</a></p><h1>Sign in to {title}</h1>'
                    f'<p class="muted">This is a browser running inside the container, on {title}\'s own page. '
                    f'Click and type as usual. Nothing you type is stored; once you\'re signed in, only the '
                    f'session {title} gives the browser is kept.</p>'
                    '<div class="bar"><button id="back">Back</button> <button id="done" class="primary">'
                    'I\'m signed in</button> <button id="cancel">Cancel</button> <span id="status" class="muted">'
                    'Starting the browser…</span></div>'
                    '<div id="screen" tabindex="0"><img id="frame" alt=""></div>'
                    '<div class="bar"><input id="typed" placeholder="On a phone? Type here, then Send" '
                    'autocomplete="off" autocapitalize="off"> <button id="send">Send</button> '
                    '<button id="enter">Enter ↵</button> <button id="bksp">⌫</button></div>' + LIVE_JS)
            return page(f"Sign in to {title}", body)

        # POST -----------------------------------------------------------

        def do_POST(self):
            if not self._same_origin():
                return self._send(403, "cross-origin request refused", "text/plain")
            path = urlparse(self.path).path.rstrip("/")
            parts = path.strip("/").split("/")

            if path == "/run":
                threading.Thread(target=_safe_run, args=(runner,), daemon=True).start()
                return self._redirect("./")
            if path == "/notify/test":
                notifier.send("test", "Test notification: Apprise is working.")
                return self._redirect("../")
            if len(parts) == 2 and parts[0] == "disconnect" and parts[1] in by_name:
                by_name[parts[1]].logout()
                return self._redirect("../")
            if (len(parts) == 4 and parts[0] == "connect" and parts[2] == "live" and parts[1] in by_name
                    and parts[1] in lives):
                source, live = by_name[parts[1]], lives[parts[1]]
                try:
                    if parts[3] == "start":
                        live.start(source.live_url, source.live_signed_in, source.live_save,
                                   user_agent=source.live_user_agent, init_script=source.live_init_script,
                                   on_success=lambda: _connected(source))
                        return self._send(204, "")
                    if parts[3] == "input":
                        n = int(self.headers.get("Content-Length") or 0)
                        live.send(json.loads(self.rfile.read(n) or b"{}"))
                        return self._send(204, "")
                except (RuntimeError, ValueError) as e:
                    return self._send(409, str(e), "text/plain")
            if len(parts) >= 2 and parts[0] == "connect" and parts[1] in flows:
                source, flow = by_name[parts[1]], flows[parts[1]]
                form = self._form()
                try:
                    if len(parts) == 2:
                        fields = {f[0]: form.get(f[0], "") for f in source.login_fields}
                        flow.start(lambda io: source.login(fields, io), on_success=lambda: _connected(source))
                        return self._redirect(source.name)
                    if parts[2] == "answer":
                        flow.answer(form.get("value", ""))
                        return self._send(204, "")
                    if parts[2] == "cancel":
                        flow.cancel()
                        return self._send(204, "")
                except RuntimeError as e:
                    return self._send(409, str(e), "text/plain")
            self._send(404, "not found", "text/plain")

    def _connected(source: Source) -> None:
        notifier.clear(login_flag(source))
        # Catch up straight away instead of waiting for the next scheduled run.
        threading.Thread(target=_safe_run, args=(runner,), daemon=True).start()

    return Handler


def _safe_run(runner: Runner) -> None:
    try:
        runner.run()
    except Exception:
        pass  # already logged and recorded by Runner.run


def serve(port: int, state: State, runner: Runner, notifier: Notifier, sources: list[Source],
          dry_run: bool, setup_error: str | None = None) -> ThreadingHTTPServer:
    lives = {s.name: LiveBrowser() for s in sources if s.live_url}
    flows = {s.name: LoginFlow(live=lives.get(s.name)) for s in sources if s.login_fields}
    handler = make_handler(state, runner, notifier, sources, flows, dry_run, setup_error, lives)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("status page on :%d", port)
    return server

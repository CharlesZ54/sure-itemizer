"""sure-itemizer: split merchant charges in Sure into the items they paid for.

  itemizer serve           run on the SCHEDULE (cron) and serve the web UI (default)
  itemizer run             do one run now and exit
  itemizer login SOURCE    sign in to a merchant from the console (the web UI's Connect is easier)
  itemizer status          print what has been planned/applied
  itemizer probe SOURCE    record a merchant's own order data (to build its parser)
"""
from __future__ import annotations

import getpass
import logging
import sys
import time
from datetime import datetime

from .config import Config
from .cron import Cron
from .notify import Notifier
from .runner import Runner, login_flag
from .sources import build_sources
from .state import State
from .sure import SureClient


class ConsoleIO:
    def echo(self, msg, **kwargs):
        print(msg)

    def handoff(self, context, pages, reason, until):
        raise SystemExit(f"{reason}. That step needs the web UI's Connect page (live view).")

    def prompt(self, msg, type=None, **kwargs):
        for choice in kwargs.get("choices") or []:
            print(" ", choice)
        if kwargs.get("img_url"):
            print("  image:", kwargs["img_url"])
        answer = input(f"{msg}: ").strip()
        return type(answer) if type else answer


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = argv[0] if argv else "serve"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cfg = Config.from_env()
    state = State(cfg.data_dir / "itemizer.db")
    notifier = Notifier(cfg.apprise_url, cfg.public_url, state)

    if cmd == "status":
        for r in state.plans():
            print(f"{r['txn_date']}  {r['txn_amount']:>9}  {r['status']:<13} {r['txn_name'][:50]:<50} {r['reason'] or ''}")
        return 0

    setup_error = None
    try:
        sources = build_sources(cfg)
    except (SystemExit, ValueError) as e:
        if cmd != "serve":
            raise
        # Keep the web UI up so the problem is visible, instead of a
        # container that restarts forever.
        setup_error = str(e)
        logging.getLogger("itemizer").error("not running any sources: %s", e)
        sources = []

    if cmd == "login":
        by_name = {s.name: s for s in sources if s.login_fields}
        if len(argv) < 2 or argv[1] not in by_name:
            print(f"usage: itemizer login {{{','.join(by_name)}}}")
            return 2
        source = by_name[argv[1]]
        defaults = source.login_defaults()

        def ask(name: str, label: str, kind) -> str:
            if kind == "password":
                return getpass.getpass(f"{label}: ")
            if isinstance(kind, list):  # a choice: list it, Enter keeps the default
                print(f"{label}: " + ", ".join(v for v, _ in kind))
                return input(f"{label} [{defaults.get(name, kind[0][0])}]: ").strip() or defaults.get(name, kind[0][0])
            return input(f"{label}: ")

        fields = {f[0]: ask(*f[:3]) for f in source.login_fields}
        source.login(fields, ConsoleIO())
        notifier.clear(login_flag(source))
        print(f"{source.name}: signed in; session saved")
        return 0

    if cmd == "probe":
        by_name = {s.name: s for s in sources if hasattr(s, "probe")}
        if len(argv) < 2 or argv[1] not in by_name:
            print(f"usage: itemizer probe {{{','.join(by_name)}}}")
            return 2
        print(f"saved to {by_name[argv[1]].probe()}")
        return 0

    runner = Runner(cfg, SureClient(cfg.sure_url, cfg.sure_api_key), sources, state, notifier)
    if cmd == "run":
        print(runner.run())
        return 0
    if cmd != "serve":
        print(__doc__)
        return 2

    from .web import serve

    log = logging.getLogger("itemizer")
    serve(cfg.web_port, state, runner, notifier, sources, cfg.dry_run, setup_error)
    log.info("mode: %s; sources: %s; schedule: %s", "DRY RUN" if cfg.dry_run else "LIVE",
             ",".join(cfg.sources), cfg.schedule)
    if not sources:
        while True:  # nothing to do until the configuration is fixed and the container restarted
            time.sleep(3600)
    if cfg.run_on_start:
        _guarded(runner, log)
    schedule = Cron(cfg.schedule)
    while True:
        at = schedule.next_after(datetime.now())
        log.info("next run at %s", at)
        time.sleep(max(1, (at - datetime.now()).total_seconds()))
        _guarded(runner, log)


def _guarded(runner: Runner, log: logging.Logger) -> None:
    try:
        runner.run()
    except Exception:
        log.error("run failed; will try again at the next scheduled time")


def _cli() -> None:
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    _cli()

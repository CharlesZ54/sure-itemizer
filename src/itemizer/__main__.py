"""sure-itemizer: split merchant charges in Sure into the items they paid for.

  itemizer serve    run on the RUN_TIMES schedule and serve the status page (default)
  itemizer run      do one run now and exit
  itemizer login    check the merchant logins (Amazon) and save the session
  itemizer status   print what has been planned/applied
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta

from .config import Config
from .runner import Runner
from .sources import build_sources
from .state import State
from .sure import SureClient


def next_run(now: datetime, times: tuple[str, ...]) -> datetime:
    candidates = []
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidates.append(at if at > now else at + timedelta(days=1))
    return min(candidates)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = argv[0] if argv else "serve"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cfg = Config.from_env()
    state = State(cfg.data_dir / "itemizer.db")

    if cmd == "status":
        for r in state.plans():
            print(f"{r['txn_date']}  {r['txn_amount']:>9}  {r['status']:<13} {r['txn_name'][:50]:<50} {r['reason'] or ''}")
        return 0

    try:
        sources = build_sources(cfg)
    except (SystemExit, ValueError) as e:
        if cmd != "serve":
            raise
        # Keep the status page up so the problem is visible, instead of a
        # container that restarts forever.
        logging.getLogger("itemizer").error("not running any sources: %s", e)
        state.finish_run(state.start_run(), {}, error=f"setup: {e}")
        sources = []
    if cmd == "login":
        for s in sources:
            if hasattr(s, "session"):
                s.session()
                print(f"{s.name}: logged in, session saved")
        return 0

    runner = Runner(cfg, SureClient(cfg.sure_url, cfg.sure_api_key), sources, state)
    if cmd == "run":
        print(runner.run())
        return 0
    if cmd != "serve":
        print(__doc__)
        return 2

    from .web import serve

    log = logging.getLogger("itemizer")
    serve(cfg.web_port, state, runner, cfg.dry_run)
    log.info("mode: %s; sources: %s; schedule: %s", "DRY RUN" if cfg.dry_run else "LIVE",
             ",".join(cfg.sources), ",".join(cfg.run_times))
    if not sources:
        while True:  # nothing to do until the template is fixed and the container restarted
            time.sleep(3600)
    if cfg.run_on_start:
        _guarded(runner, log)
    while True:
        at = next_run(datetime.now(), cfg.run_times)
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

from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import date, timedelta

from .config import Config
from .matcher import match
from .models import Plan
from .notify import Notifier
from .planner import plan
from .sources import Source
from .sources.base import LoginRequired
from .state import State
from .sure import SplitsUnsupported, SureClient, SureError

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def login_flag(source: Source) -> str:
    return f"login_required:{source.name}"


class Runner:
    def __init__(self, cfg: Config, sure: SureClient, sources: list[Source], state: State,
                 notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.sure = sure
        self.sources = sources
        self.state = state
        self.notifier = notifier or Notifier(None, None, state)

    def run(self) -> dict:
        if not _run_lock.acquire(blocking=False):
            log.info("a run is already in progress; skipping")
            return {"skipped": "already running"}
        run_id = self.state.start_run()
        tally: Counter = Counter()
        try:
            for source in self.sources:
                try:
                    self._run_source(source, tally)
                except LoginRequired as e:
                    # Never sign in unattended: stop this source and ask the
                    # user, once, to reconnect it from the web UI.
                    log.warning("%s: login required: %s", source.name, e)
                    tally[f"{source.name}_login_required"] += 1
                    self.notifier.once(login_flag(source), "login_required",
                                       f"{source.title or source.name} needs you to sign in "
                                       f"({e}). Open the status page and click Connect.", source.name)
            summary = {"dry_run": self.cfg.dry_run, **tally}
            self.state.finish_run(run_id, summary)
            self.notifier.clear("run_failed")
            log.info("run finished: %s", summary)
            return summary
        except Exception as e:
            log.exception("run failed")
            self.state.finish_run(run_id, dict(tally), error=str(e))
            self.notifier.once("run_failed", "run_failed", f"A run failed: {e}")
            raise
        finally:
            _run_lock.release()

    def _run_source(self, source: Source, tally: Counter) -> None:
        since = date.today() - timedelta(days=self.cfg.lookback_days)
        ignored = self.state.ignored_ids()
        children = self.state.children_ids()
        claimed = [t for t in self.sure.transactions(since, source.sure_search)
                   if source.claims(t) and t.id not in children]
        txns = [t for t in claimed if t.id not in ignored]
        log.info("%s: %d Sure transactions to look at since %s", source.name, len(txns), since)
        if not txns:
            return

        purchases = source.fetch(since - timedelta(days=source.date_window[1]),
                                 amounts={abs(t.amount_cents) for t in txns}, txns=claimed)
        pairs = match(txns, purchases, source.date_window)
        matched = {t.id for t, _ in pairs}

        for t in txns:
            if t.id not in matched:
                self.state.record(t, source.name, "unmatched",
                                  reason=f"no {source.name} charge with this amount near this date")
                tally["unmatched"] += 1

        for t, p in pairs:
            status = self._apply(plan(t, p), source.name)
            tally[status] += 1

    def _apply(self, p: Plan, source: str) -> str:
        t = p.txn
        try:
            if p.action == "skip":
                return self._record(p, source, "skipped")

            if p.action == "rename":
                if self.cfg.dry_run:
                    return self._record(p, source, "planned", "dry run")
                self.sure.describe(t.id, p.name, p.notes)
                log.info("renamed %s %s -> %s", t.date, t.name, p.name)
                return self._record(p, source, "applied")

            try:
                if self.sure.split_state(t.id):
                    return self._record(p, source, "already_split", "already split in Sure")
            except SplitsUnsupported:
                status = "planned" if self.cfg.dry_run else "deferred"
                return self._record(p, source, status, "waiting for Sure v0.7.6 (split API)")
            if self.cfg.dry_run:
                return self._record(p, source, "planned", "dry run")
            child_ids = self.sure.split(t.id, p.children)
            self.state.record(t, source, "applied", p, children_ids=child_ids)
            log.info("split %s %s into %d items", t.date, t.name, len(child_ids))
            return "applied"
        except SureError as e:
            # Pending card transactions can't be split yet; try again next run.
            status = "deferred" if "pending" in str(e).lower() else "error"
            log.warning("%s %s %s: %s", status, t.date, t.name, e)
            return self._record(p, source, status, str(e))

    def _record(self, p: Plan, source: str, status: str, reason: str = "") -> str:
        self.state.record(p.txn, source, status, p, reason=reason)
        return status

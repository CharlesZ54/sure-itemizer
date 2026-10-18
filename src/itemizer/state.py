"""SQLite record of what was decided and done for each Sure transaction."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Plan, SureTxn
from .money import cents_to_str

# Never touched again once reached.
FINAL = ("applied", "already_split")

SCHEMA = """
create table if not exists plans (
  txn_id      text primary key,
  source      text not null,
  status      text not null,   -- applied | already_split | planned | deferred | skipped | unmatched | error
  action      text,            -- split | skip
  reason      text,
  txn_date    text,
  txn_amount  text,
  txn_name    text,
  account     text,
  reference   text,
  detail      text,            -- json: the split rows
  updated_at  text not null,
  applied_at  text
);
create table if not exists children (
  child_id  text primary key,
  parent_id text not null
);
create table if not exists runs (
  id          integer primary key autoincrement,
  started_at  text not null,
  finished_at text,
  summary     text,
  error       text
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def ignored_ids(self) -> set[str]:
        """Transactions to leave alone: finished ones and children we created."""
        with self.lock:
            q = f"select txn_id from plans where status in ({','.join('?' * len(FINAL))})"
            done = {r[0] for r in self.db.execute(q, FINAL)}
            kids = {r[0] for r in self.db.execute("select child_id from children")}
        return done | kids

    def record(self, txn: SureTxn, source: str, status: str, plan: Plan | None = None,
               reason: str = "", children_ids: list[str] | None = None) -> None:
        detail = None
        reference = None
        if plan:
            reason = reason or plan.reason
            reference = plan.purchase.reference if plan.purchase else None
            if plan.children:
                detail = json.dumps([{"name": n, "amount": cents_to_str(c)} for n, c in plan.children])
        now = _now()
        with self.lock, self.db:
            self.db.execute(
                """insert into plans (txn_id, source, status, action, reason, txn_date, txn_amount, txn_name,
                                      account, reference, detail, updated_at, applied_at)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(txn_id) do update set
                     status=excluded.status, action=excluded.action, reason=excluded.reason,
                     txn_amount=excluded.txn_amount, txn_name=excluded.txn_name, reference=excluded.reference,
                     detail=excluded.detail, updated_at=excluded.updated_at,
                     applied_at=coalesce(excluded.applied_at, plans.applied_at)""",
                (txn.id, source, status, plan.action if plan else None, reason, txn.date.isoformat(),
                 cents_to_str(txn.amount_cents), txn.name, txn.account_name, reference, detail, now,
                 now if status == "applied" else None),
            )
            for cid in children_ids or []:
                self.db.execute("insert or ignore into children (child_id, parent_id) values (?, ?)", (cid, txn.id))

    def start_run(self) -> int:
        with self.lock, self.db:
            return self.db.execute("insert into runs (started_at) values (?)", (_now(),)).lastrowid

    def finish_run(self, run_id: int, summary: dict, error: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("update runs set finished_at=?, summary=?, error=? where id=?",
                            (_now(), json.dumps(summary), error, run_id))

    def plans(self, status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        with self.lock:
            if status:
                return self.db.execute("select * from plans where status=? order by txn_date desc limit ?",
                                       (status, limit)).fetchall()
            return self.db.execute("select * from plans order by txn_date desc limit ?", (limit,)).fetchall()

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute("select status, count(*) from plans group by status")}

    def last_runs(self, n: int = 5) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("select * from runs order by id desc limit ?", (n,)).fetchall()

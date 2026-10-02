"""集計結果・保留一覧・実行ログを SQLite に保存する。

月次記録は (月, 担当社員) を主キーに上書き保存するため、同じ日に何度実行しても重複しない。
確定済みの月は、再集計（--month 指定）のときだけ書き換える。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .aggregate import MonthResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly (
    month TEXT NOT NULL,
    employee TEXT NOT NULL,
    headcount INTEGER, targets INTEGER, continued INTEGER, withdrawn INTEGER,
    pending INTEGER, undecided INTEGER,
    rate REAL, judgement TEXT, status TEXT, finalized INTEGER, points REAL,
    updated_at TEXT,
    PRIMARY KEY (month, employee)
);
CREATE TABLE IF NOT EXISTS pending (
    month TEXT, employee TEXT, name TEXT, reason TEXT, paid_count INTEGER,
    last_error_date TEXT, customer_ids TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT, mode TEXT, status TEXT,
    subscriptions INTEGER, invoices INTEGER, customers INTEGER, api_requests INTEGER,
    months TEXT, messages TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- monthly
    def is_finalized(self, month: str) -> bool:
        row = self.db.execute("SELECT COUNT(*) n, MIN(finalized) f FROM monthly WHERE month=?", (month,)).fetchone()
        return bool(row["n"]) and bool(row["f"])

    def finalized_months(self) -> set[str]:
        return {r["month"] for r in self.db.execute(
            "SELECT month FROM monthly GROUP BY month HAVING MIN(finalized)=1")}

    def save_month(self, result: MonthResult, updated_at: str, force: bool = False) -> bool:
        """月の集計結果を保存する。確定済みの月は force=True のときだけ上書き。"""
        if self.is_finalized(result.month) and not force:
            return False
        with self.db:
            self.db.execute("DELETE FROM monthly WHERE month=?", (result.month,))
            self.db.executemany(
                "INSERT INTO monthly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r.month, r.employee, r.headcount, r.targets, r.continued, r.withdrawn, r.pending,
                  r.undecided, r.rate, r.judgement, r.status, int(r.finalized), r.points, updated_at)
                 for r in result.rows])
        return True

    def monthly_records(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM monthly ORDER BY month DESC, employee")]

    # ---------------------------------------------------------------- pending
    def replace_pending(self, result: MonthResult) -> None:
        with self.db:
            self.db.execute("DELETE FROM pending")
            self.db.executemany(
                "INSERT INTO pending VALUES (?,?,?,?,?,?,?)",
                [(p.month, p.employee, p.name, p.reason, p.paid_count,
                  p.last_error_date.isoformat() if p.last_error_date else "", ",".join(p.customer_ids))
                 for p in result.pending])

    def pending_records(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM pending ORDER BY month, employee, name")]

    # ------------------------------------------------------------------- runs
    def log_run(self, run_at: str, mode: str, status: str, counts: dict, months: list[str],
                messages: list[str]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO runs (run_at, mode, status, subscriptions, invoices, customers, api_requests,"
                " months, messages) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_at, mode, status, counts.get("subscriptions"), counts.get("invoices"),
                 counts.get("customers"), counts.get("api_requests"), ",".join(months),
                 json.dumps(messages, ensure_ascii=False)))

    def run_records(self, limit: int = 500) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
        for r in rows:
            r["messages"] = json.loads(r["messages"] or "[]")
        return rows

    def last_success(self) -> str | None:
        row = self.db.execute("SELECT MAX(run_at) t FROM runs WHERE status != '失敗'").fetchone()
        return row["t"]

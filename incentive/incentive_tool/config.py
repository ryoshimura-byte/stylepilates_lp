"""設定ファイル（config.toml）と .env の読み込み。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """.env を読み込んで環境変数に設定する（既に設定済みの値は上書きしない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    raw: dict
    base_dir: Path
    # rules
    pass_line: float = 0.25
    min_headcount: int = 20
    points_divisor: float = 10
    continuation_payments: int = 4
    paid_statuses: tuple = ("PAID", "PARTIALLY_REFUNDED")
    error_statuses: tuple = ("UNPAID", "FAILED", "PARTIALLY_PAID")
    plan_change_gap_days: int = 31
    rejoin_as_pause: bool = True
    pending_lookback_months: int = 12
    zero_target_is_achieved: bool = True
    total_amount: int = 300000
    employees: list = field(default_factory=list)
    not_eligible: set = field(default_factory=set)
    start_month: str = "2026-10"
    exclude_customer_ids: set = field(default_factory=set)
    timezone: str = "Asia/Tokyo"
    schedule_time: str = "06:00"

    def path(self, key: str) -> Path:
        p = Path(self.raw["files"][key])
        return p if p.is_absolute() else self.base_dir / p

    @property
    def square(self) -> dict:
        return self.raw.get("square", {})

    @property
    def output(self) -> dict:
        return self.raw.get("output", {})


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else BASE_DIR / "config.toml"
    base_dir = path.resolve().parent
    load_dotenv(base_dir / ".env")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = raw.get("rules", {})
    emp = raw.get("employees", {})
    tracking = raw.get("tracking", {})
    sched = raw.get("schedule", {})
    return Config(
        raw=raw,
        base_dir=base_dir,
        pass_line=float(rules.get("pass_line", 0.25)),
        min_headcount=int(rules.get("min_headcount", 20)),
        points_divisor=float(rules.get("points_divisor", 10)),
        continuation_payments=int(rules.get("continuation_payments", 4)),
        paid_statuses=tuple(rules.get("paid_statuses", ["PAID", "PARTIALLY_REFUNDED"])),
        error_statuses=tuple(rules.get("error_statuses", ["UNPAID", "FAILED", "PARTIALLY_PAID"])),
        plan_change_gap_days=int(rules.get("plan_change_gap_days", 31)),
        rejoin_as_pause=bool(rules.get("rejoin_as_pause", True)),
        pending_lookback_months=int(rules.get("pending_lookback_months", 12)),
        zero_target_is_achieved=bool(rules.get("zero_target_is_achieved", True)),
        total_amount=int(raw.get("payout", {}).get("total_amount", 300000)),
        employees=list(emp.get("names", [])),
        not_eligible=set(emp.get("not_eligible", [])),
        start_month=tracking.get("start_month", "2026-10"),
        exclude_customer_ids=set(tracking.get("exclude_customer_ids", [])),
        timezone=sched.get("timezone", "Asia/Tokyo"),
        schedule_time=sched.get("time", "06:00"),
    )

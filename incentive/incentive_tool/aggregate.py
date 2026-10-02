"""月次集計・半期集計のロジック（Square への通信を含まない純粋な計算部分）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .members import Membership, add_months, month_end, month_of, month_start
from .names import UNASSIGNED

FINAL = "確定"
PROVISIONAL = "途中経過"


@dataclass
class PendingMember:
    month: str            # 判定対象月（4回目の決済予定月）
    employee: str
    name: str
    reason: str
    paid_count: int
    last_error_date: date | None
    customer_ids: list[str]


@dataclass
class MonthlyRow:
    month: str
    employee: str
    headcount: int
    targets: int
    continued: int
    withdrawn: int
    pending: int
    undecided: int        # 当月でまだ決済日が来ていない等の未確定（途中経過のみ）
    rate: float | None
    achieved: bool
    finalized: bool
    points: float | None

    @property
    def judgement(self) -> str:
        return ("達成" if self.achieved else "未達") + f"（{self.status}）"

    @property
    def status(self) -> str:
        return FINAL if self.finalized else PROVISIONAL


@dataclass
class MonthResult:
    month: str
    finalized: bool
    rows: list[MonthlyRow]
    pending: list[PendingMember] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)  # 会員ごとの判定（確認用）


def judgement_month(m: Membership, cfg, as_of: date) -> tuple[str, str | None, date | None]:
    """(4回目の決済予定月, 判定結果 or None, 判定日)"""
    sched = month_of(m.billing_date(cfg.continuation_payments))
    res = m.resolution(cfg.continuation_payments, as_of)
    if res is None:
        return sched, None, None
    kind, when = res
    return sched, kind, when


def _pending_reason(m: Membership, cfg, as_of: date) -> tuple[str, date | None]:
    errs = m.error_invoices(as_of, cfg.error_statuses)
    last_err = max((i.due_date for i in errs), default=None)
    if m.status == "PAUSED" or m.on_break:
        return "休会中", last_err
    if m.status == "DEACTIVATED":
        return "決済エラー（契約停止）", last_err
    if errs:
        return "決済エラー（カード期限切れ・残高不足など）", last_err
    return "決済未完了", last_err


def compute_month(memberships: list[Membership], month: str, cfg, as_of: date) -> MonthResult:
    """対象月 month を as_of 時点のデータで集計する。

    - 月末を過ぎていれば「確定」、そうでなければ「途中経過」
    - 判定月 ＝ max(4回目の決済予定月, 継続/退会が決まった月)
      → 決済エラーや休会で持ち越した会員は、決済完了（または解約）した月に判定される
    """
    finalized = as_of > month_end(month)
    cutoff = month_end(month)
    employees = list(cfg.employees)
    stats = {e: dict(headcount=0, targets=0, continued=0, withdrawn=0, pending=0, undecided=0)
             for e in employees}

    def bucket(emp: str) -> dict:
        if emp not in stats:
            stats[emp] = dict(headcount=0, targets=0, continued=0, withdrawn=0, pending=0, undecided=0)
        return stats[emp]

    pending: list[PendingMember] = []
    details: list[dict] = []
    lookback_start = add_months(month, -cfg.pending_lookback_months)

    for m in memberships:
        # 担当人数：当月は実行時点のステータス、確定月は月末時点
        live = m.is_live_on(cutoff) if finalized else m.is_live_now()
        if live:
            bucket(m.employee)["headcount"] += 1

        if m.start_date is None or m.start_date > cutoff:
            continue
        sched, kind, when = judgement_month(m, cfg, as_of)
        if sched > month:
            continue
        decided_month = month_of(when) if when else None
        # 対象月の末日より後に決まったものは、この月の時点では未確定
        if decided_month and decided_month > month:
            kind, decided_month = None, None
        j_month = max(sched, decided_month) if decided_month else None

        b = bucket(m.employee)
        if j_month == month:
            b["targets"] += 1
            b["continued" if kind == "継続" else "withdrawn"] += 1
            details.append(dict(month=month, employee=m.employee, name=m.name, scheduled=sched,
                                result=kind, date=when.isoformat()))
        elif kind is None:
            if sched == month and not finalized:
                b["targets"] += 1
                b["undecided"] += 1
                reason, last_err = _pending_reason(m, cfg, as_of)
                if last_err or m.status in ("PAUSED", "DEACTIVATED"):
                    pending.append(PendingMember(sched, m.employee, m.name, "当月：" + reason,
                                                 m.paid_count(as_of), last_err, m.customer_ids))
                details.append(dict(month=month, employee=m.employee, name=m.name, scheduled=sched,
                                    result="未確定", date=""))
            elif sched >= lookback_start:
                b["pending"] += 1
                reason, last_err = _pending_reason(m, cfg, min(as_of, cutoff) if finalized else as_of)
                pending.append(PendingMember(sched, m.employee, m.name, reason,
                                             m.paid_count(cutoff if finalized else as_of), last_err,
                                             m.customer_ids))

    rows = []
    for emp, s in stats.items():
        if emp == UNASSIGNED and not any(s.values()):
            continue
        rate = s["withdrawn"] / s["targets"] if s["targets"] else None
        achieved = (rate <= cfg.pass_line + 1e-12) if rate is not None else cfg.zero_target_is_achieved
        points: float | None
        if emp == UNASSIGNED:
            points = None
        elif achieved and s["headcount"] >= cfg.min_headcount:
            points = round(s["headcount"] / cfg.points_divisor, 4)
        else:
            points = 0.0
        rows.append(MonthlyRow(month, emp, s["headcount"], s["targets"], s["continued"], s["withdrawn"],
                               s["pending"], s["undecided"], rate, achieved, finalized, points))
    pending.sort(key=lambda p: (p.month, p.employee, p.name))
    return MonthResult(month, finalized, rows, pending, details)


# ------------------------------------------------------------------ half year
def half_of(month: str) -> tuple[str, str, str]:
    """(ラベル, 開始月, 終了月)。4〜9月 / 10〜3月。"""
    d = month_start(month)
    if 4 <= d.month <= 9:
        return f"{d.year}年度 上期（{d.year}/04〜{d.year}/09）", f"{d.year}-04", f"{d.year}-09"
    fy = d.year if d.month >= 10 else d.year - 1
    return f"{fy}年度 下期（{fy}/10〜{fy + 1}/03）", f"{fy}-10", f"{fy + 1}-03"


@dataclass
class HalfRow:
    half: str
    employee: str
    eligible: bool
    final_points: float
    expected_points: float
    final_ratio: float | None
    final_amount: int | None
    expected_ratio: float | None
    expected_amount: int | None


def compute_half(records: list[dict], cfg) -> list[HalfRow]:
    """records: 月次記録（dict: month, employee, points, finalized）。半期ごとに社員別に集計する。"""
    halves: dict[str, dict] = {}
    for r in records:
        if r["employee"] == UNASSIGNED:
            continue
        label = half_of(r["month"])[0]
        h = halves.setdefault(label, {e: [0.0, 0.0] for e in cfg.employees})
        pts = r["points"] or 0.0
        acc = h.setdefault(r["employee"], [0.0, 0.0])
        if r["finalized"]:
            acc[0] += pts
        acc[1] += pts

    out: list[HalfRow] = []
    for label in sorted(halves, reverse=True):
        h = halves[label]
        elig = [e for e in h if e not in cfg.not_eligible]
        tot_final = sum(h[e][0] for e in elig)
        tot_exp = sum(h[e][1] for e in elig)
        for e, (fp, ep) in h.items():
            eligible = e not in cfg.not_eligible
            fr = (fp / tot_final if tot_final else 0.0) if eligible else None
            er = (ep / tot_exp if tot_exp else 0.0) if eligible else None
            out.append(HalfRow(
                label, e, eligible, round(fp, 4), round(ep, 4),
                fr, math.floor(cfg.total_amount * fr) if fr is not None else None,
                er, math.floor(cfg.total_amount * er) if er is not None else None,
            ))
    return out

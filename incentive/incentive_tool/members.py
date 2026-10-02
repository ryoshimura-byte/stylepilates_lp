"""Square のサブスクリプション・請求書を「会員の在籍期間（メンバーシップ）」に組み立てる。

- 同じ会員（担当表の同一行、または同一の顧客ID）のサブスクリプションを開始日順に並べ、
  前の契約の終了から plan_change_gap_days 以内に始まった契約はプラン変更とみなして1つに通算する。
- それより空けて再開した契約は「休会明け」とみなして同じ在籍に通算する（rejoin_as_pause = true のとき）。
  契約と契約の間の空白期間は休会中（PAUSED と同じ扱い）。
- 決済回数は、通算したすべてのサブスクリプションの invoice のうち支払い済みのものを数える。
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .names import UNASSIGNED, Assignments, customer_display_name

log = logging.getLogger(__name__)

LIVE_STATUSES = ("ACTIVE", "PAUSED")


# ---------------------------------------------------------------- month helpers
def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_start(m: str) -> date:
    y, mo = map(int, m.split("-"))
    return date(y, mo, 1)


def month_end(m: str) -> date:
    y, mo = map(int, m.split("-"))
    return date(y, mo, calendar.monthrange(y, mo)[1])


def add_months(m: str, n: int) -> str:
    y, mo = map(int, m.split("-"))
    idx = y * 12 + (mo - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _anchor_date(y: int, mo: int, day: int) -> date:
    return date(y, mo, min(day, calendar.monthrange(y, mo)[1]))


# ---------------------------------------------------------------------- model
@dataclass
class Invoice:
    id: str
    status: str
    due_date: date | None
    paid_date: date | None  # 支払い済みの場合のみ


@dataclass
class Membership:
    person_key: str
    name: str
    employee: str
    customer_ids: list[str]
    subscriptions: list[dict]
    invoices: list[Invoice] = field(default_factory=list)
    start_date: date = date.min
    anchor_day: int = 1
    end_date: date | None = None       # 契約終了日（解約済み・解約予定）。継続中は None
    status: str = "ACTIVE"             # 代表ステータス（ACTIVE/PAUSED/DEACTIVATED/PENDING/CANCELED）

    def billing_date(self, n: int) -> date:
        """n回目（1始まり）の決済予定日。1回目は開始日、以降は請求基準日（anchor）。"""
        if n == 1:
            return self.start_date
        y, mo = self.start_date.year, self.start_date.month
        first = _anchor_date(y, mo, self.anchor_day)
        if first <= self.start_date:
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        idx = y * 12 + (mo - 1) + (n - 2)
        return _anchor_date(idx // 12, idx % 12 + 1, self.anchor_day)

    def paid_dates(self) -> list[date]:
        return sorted(i.paid_date for i in self.invoices if i.paid_date)

    def nth_paid_date(self, n: int) -> date | None:
        p = self.paid_dates()
        return p[n - 1] if len(p) >= n else None

    def paid_count(self, as_of: date) -> int:
        return sum(1 for p in self.paid_dates() if p <= as_of)

    def error_invoices(self, as_of: date, error_statuses) -> list[Invoice]:
        return [i for i in self.invoices
                if i.status in error_statuses and i.due_date and i.due_date <= as_of]

    def resolution(self, required: int, as_of: date) -> tuple[str, date] | None:
        """('継続', 決済完了日) / ('退会', 解約日) / None（未確定）。as_of 時点で分かっている情報で判定。"""
        nth = self.nth_paid_date(required)
        if nth and nth <= as_of:
            return "継続", nth
        if self.end_date is not None and self._cancel_is_final(required, as_of):
            return "退会", self.end_date
        return None

    def _cancel_is_final(self, required: int, as_of: date) -> bool:
        """解約予定日が、次に予定されていた決済日以前なら、もう決済は来ない＝退会確定。"""
        if self.end_date <= as_of:
            return True
        paid = [p for p in self.paid_dates() if p <= as_of]
        if not paid:
            return self.end_date <= self.start_date
        return self.end_date <= self.next_anchor_after(paid[-1])

    def next_anchor_after(self, d: date) -> date:
        """d より後の最初の請求基準日。"""
        cand = _anchor_date(d.year, d.month, self.anchor_day)
        if cand <= d:
            y, mo = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            cand = _anchor_date(y, mo, self.anchor_day)
        return cand

    def is_live_on(self, d: date) -> bool:
        """d 時点で契約が有効か（月末時点の担当人数の算出用）。"""
        for s in self.subscriptions:
            start = _d(s.get("start_date"))
            if not start or start > d:
                continue
            if s.get("status") == "DEACTIVATED":
                end = _d(s.get("charged_through_date")) or start
            else:
                end = _d(s.get("canceled_date"))
            if end is None or end > d:
                return True
        return self.in_pause_gap(d)

    def in_pause_gap(self, d: date) -> bool:
        """d が契約と契約の間の空白期間（休会中）にあたるか。"""
        started = any(_d(s.get("start_date")) <= d for s in self.subscriptions if s.get("start_date"))
        resumes = any(
            _d(s.get("start_date")) > d and not (
                s.get("status") == "CANCELED" and s.get("canceled_date", "")[:10] <= s["start_date"][:10])
            for s in self.subscriptions if s.get("start_date"))
        return started and resumes

    def is_live_now(self, today: date | None = None) -> bool:
        if any(s.get("status") in LIVE_STATUSES for s in self.subscriptions):
            return True
        # 休会明けの契約が開始待ち（PENDING）で、すでに決済実績がある → 休会中
        return any(s.get("status") == "PENDING" for s in self.subscriptions) and bool(self.paid_dates())

    @property
    def on_break(self) -> bool:
        """現在、契約の空白期間（休会）中か。"""
        return (not any(s.get("status") in LIVE_STATUSES for s in self.subscriptions)
                and self.is_live_now())


# -------------------------------------------------------------------- builder
@dataclass
class BuildResult:
    memberships: list[Membership]
    warnings: list[str]
    unmatched: list[str]


def _sub_end(s: dict) -> date | None:
    if s.get("status") == "DEACTIVATED":
        return _d(s.get("charged_through_date")) or _d(s.get("start_date"))
    return _d(s.get("canceled_date"))


@dataclass
class Override:
    """Square 以外（Airペイ・現金など）での支払い実績の補正。"""
    joined: date | None      # 実際の入会日
    extra_payments: int      # Square 以外で決済した回数
    note: str = ""
    keep_member: bool = False  # Square 上は解約でも退会扱いしない（登録し直し予定など）


def load_overrides(path) -> dict[str, Override]:
    """data/member_overrides.csv（列：会員名 or 顧客ID, 実際の入会日, Square外の決済回数, 退会扱いしない, 備考）。"""
    import csv

    from .names import normalize
    out: dict[str, Override] = {}
    if not path or not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
            key = r.get("顧客ID") or normalize(r.get("会員名"))
            if not key:
                continue
            joined = _d(r.get("実際の入会日").replace("/", "-")) if r.get("実際の入会日") else None
            keep = r.get("退会扱いしない", "").lower() in ("1", "true", "yes", "○", "◯", "はい")
            out[key] = Override(joined, int(r.get("Square外の決済回数") or 0), r.get("備考", ""), keep)
    return out


def _apply_override(m: "Membership", ov: Override) -> None:
    if ov.joined and ov.joined < m.start_date:
        m.start_date = ov.joined
    # Square 外の決済は、Square の初回決済より前に毎月1回ずつ行われたものとして扱う
    first_square = min(m.paid_dates(), default=m.start_date)
    for i in range(ov.extra_payments):
        d = first_square - timedelta(days=1 + 30 * i)
        m.invoices.append(Invoice(f"external:{i + 1}", "PAID", d, d))
    if m.paid_dates():
        m.start_date = min(m.start_date, m.paid_dates()[0])
    if ov.keep_member:
        m.end_date = None


def build_memberships(data: dict, assignments: Assignments, cfg,
                      overrides: dict[str, Override] | None = None) -> BuildResult:
    tz = ZoneInfo(cfg.timezone)
    warnings: list[str] = []
    customers = {c["id"]: c for c in data.get("customers", [])}

    invoices_by_sub: dict[str, list[Invoice]] = {}
    for inv in data.get("invoices", []):
        sid = inv.get("subscription_id")
        if not sid:
            continue
        due = None
        reqs = inv.get("payment_requests") or []
        dues = [r.get("due_date") for r in reqs if r.get("due_date")]
        if dues:
            due = _d(min(dues))
        paid = None
        if inv.get("status") in cfg.paid_statuses:
            ts = inv.get("updated_at") or inv.get("created_at")
            paid = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()
        invoices_by_sub.setdefault(sid, []).append(Invoice(inv["id"], inv.get("status", ""), due, paid))

    # 会員（担当表の行 or 顧客ID）ごとにサブスクリプションをまとめる
    persons: dict[str, dict] = {}
    unmatched: dict[str, str] = {}
    for s in data.get("subscriptions", []):
        cid = s.get("customer_id")
        if not cid or cid in cfg.exclude_customer_ids:
            continue
        cust = customers.get(cid, {"id": cid})
        a, how = assignments.match(cust)
        if a:
            key, name, emp = f"A:{a.employee}:{a.key}", a.member_name, a.employee
        else:
            key, name, emp = f"C:{cid}", customer_display_name(cust) or cid, UNASSIGNED
            # 過去に退会済みの会員まで警告すると埋もれるので、契約が残っている会員だけ警告する
            if assignments.loaded and cid not in unmatched and s.get("status") != "CANCELED":
                unmatched[cid] = f"担当表にない会員: {name}（顧客ID {cid}）{(' ' + how) if how else ''}"
        p = persons.setdefault(key, {"name": name, "employee": emp, "customer_ids": set(), "subs": []})
        p["customer_ids"].add(cid)
        p["subs"].append(s)

    memberships: list[Membership] = []
    gap = timedelta(days=cfg.plan_change_gap_days)
    skipped_never_paid = 0
    for key, p in persons.items():
        subs = sorted(p["subs"], key=lambda s: (s.get("start_date") or "", s.get("created_at") or ""))
        groups: list[list[dict]] = []
        group_end: date | None = None
        open_ended = False
        for s in subs:
            start = _d(s.get("start_date"))
            if groups and (open_ended or cfg.rejoin_as_pause
                           or (group_end and start and start <= group_end + gap)):
                groups[-1].append(s)
            else:
                groups.append([s])
                group_end, open_ended = None, False
            end = _sub_end(s)
            if end is None:
                open_ended = True
            elif group_end is None or end > group_end:
                group_end = end
        for gi, g in enumerate(groups):
            m = Membership(
                person_key=key if len(groups) == 1 else f"{key}#{gi + 1}",
                name=p["name"], employee=p["employee"],
                customer_ids=sorted(p["customer_ids"]), subscriptions=g,
            )
            first = g[0]
            m.start_date = _d(first.get("start_date"))
            m.anchor_day = int(first.get("monthly_billing_anchor_date") or m.start_date.day)
            for s in g:
                m.invoices.extend(invoices_by_sub.get(s["id"], []))
            statuses = [s.get("status") for s in g]
            ends = [_sub_end(s) for s in g]
            for st in ("ACTIVE", "PAUSED", "DEACTIVATED", "PENDING", "CANCELED"):
                if st in statuses:
                    m.status = st
                    break
            # 終了日：期限なしで続いている契約が1つでもあれば None
            if any(e is None and s.get("status") != "DEACTIVATED" for e, s in zip(ends, g)):
                m.end_date = None
            elif m.status == "DEACTIVATED":
                m.end_date = None  # 決済エラーで停止。退会ではなく保留扱い
            else:
                m.end_date = max(e for e in ends if e)
            if overrides and gi == 0:
                from .names import normalize
                ov = next((overrides[k] for k in [*m.customer_ids, normalize(m.name)] if k in overrides), None)
                if ov:
                    _apply_override(m, ov)
            if m.end_date is not None and not m.paid_dates() and m.status == "CANCELED":
                # 一度も決済されずに終了した契約（申込みの取り消し・重複登録など）は会員として数えない
                skipped_never_paid += 1
                continue
            memberships.append(m)
    if skipped_never_paid:
        log.info("一度も決済されずに終了した契約 %d 件を集計から除外しました", skipped_never_paid)
    return BuildResult(memberships, warnings, list(unmatched.values()))

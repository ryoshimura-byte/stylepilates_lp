"""架空データによるテスト。 実行: python -m unittest discover -s tests"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from incentive_tool.aggregate import compute_half, compute_month, half_of  # noqa: E402
from incentive_tool.cli import main  # noqa: E402
from incentive_tool.config import load_config  # noqa: E402
from incentive_tool.members import add_months, build_memberships  # noqa: E402
from incentive_tool.names import Assignment, Assignments, normalize  # noqa: E402


# ----------------------------------------------------------------- fixtures
class Fixture:
    def __init__(self):
        self.subs, self.invoices, self.customers = [], [], []
        self._n = 0

    def customer(self, family, given):
        self._n += 1
        cid = f"CUST{self._n:03d}"
        self.customers.append({"id": cid, "family_name": family, "given_name": given})
        return cid

    def sub(self, cid, start, status="ACTIVE", canceled=None, paid=(), unpaid=(), anchor=None):
        """paid/unpaid: 決済日（または期日）の 'YYYY-MM-DD' のリスト。"""
        sid = f"SUB{len(self.subs) + 1:03d}"
        ids = []
        for d in paid:
            ids.append(self._inv(sid, cid, d, "PAID"))
        for d in unpaid:
            ids.append(self._inv(sid, cid, d, "UNPAID"))
        s = {"id": sid, "customer_id": cid, "start_date": start, "status": status,
             "monthly_billing_anchor_date": anchor or int(start[8:]), "invoice_ids": ids}
        if canceled:
            s["canceled_date"] = canceled
            s["charged_through_date"] = canceled
        self.subs.append(s)
        return sid

    def _inv(self, sid, cid, d, status):
        iid = f"inv:{len(self.invoices) + 1}"
        self.invoices.append({
            "id": iid, "subscription_id": sid, "status": status,
            "payment_requests": [{"due_date": d}],
            "primary_recipient": {"customer_id": cid},
            "created_at": f"{d}T00:00:00Z", "updated_at": f"{d}T03:00:00Z",
        })
        return iid

    def data(self):
        return {"subscriptions": self.subs, "invoices": self.invoices, "customers": self.customers}


def monthly_paid(start: str, n: int) -> list[str]:
    m, day = start[:7], start[8:]
    return [f"{add_months(m, i)}-{day}" for i in range(n)]


def cfg_with(**overrides):
    cfg = load_config(ROOT / "config.toml")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def row(result, emp):
    return next(r for r in result.rows if r.employee == emp)


# -------------------------------------------------------------------- tests
class NameTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("篠原　見佳 様"), normalize("篠原見佳"))
        self.assertEqual(normalize("ｼﾉﾊﾗ ﾐｶ"), normalize("しのはら みか"))
        self.assertEqual(normalize("髙橋"), normalize("高橋"))

    def test_match_alias_reversed_and_unknown(self):
        a = Assignments([Assignment("AMI", "篠原 見佳"), Assignment("CHINA", "山田 花子")],
                        {normalize("篠原 みか"): normalize("篠原 見佳")}, loaded=True)
        self.assertEqual(a.match({"id": "x", "family_name": "篠原", "given_name": "みか"})[0].employee, "AMI")
        # 姓と名が逆に登録されている
        self.assertEqual(a.match({"id": "y", "family_name": "花子", "given_name": "山田"})[0].employee, "CHINA")
        self.assertIsNone(a.match({"id": "z", "family_name": "知らない", "given_name": "人"})[0])

    def test_customer_id_column_wins(self):
        a = Assignments([Assignment("MIZUKI", "別表記", "CUST9")], loaded=True)
        self.assertEqual(a.match({"id": "CUST9", "family_name": "全然", "given_name": "違う"})[0].employee, "MIZUKI")


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with(employees=["CHINA", "TOMOKA", "AMI", "MIZUKI", "RIHO"])
        self.f = Fixture()
        self.rows = []

    def assign(self, emp, family, given):
        cid = self.f.customer(family, given)
        self.rows.append(Assignment(emp, f"{family} {given}"))
        return cid

    def build(self):
        return build_memberships(self.f.data(), Assignments(self.rows, loaded=True), self.cfg)

    def test_june_join_is_judged_in_september(self):
        cid = self.assign("CHINA", "山田", "花子")
        self.f.sub(cid, "2026-06-10", paid=monthly_paid("2026-06-10", 4))
        r = compute_month(self.build().memberships, "2026-09", self.cfg, date(2026, 10, 2))
        c = row(r, "CHINA")
        self.assertEqual((c.targets, c.continued, c.withdrawn), (1, 1, 0))
        self.assertTrue(r.finalized)
        self.assertEqual(compute_month(self.build().memberships, "2026-08", self.cfg, date(2026, 10, 2))
                         .rows[0].targets, 0)

    def test_cancel_before_fourth_payment_is_withdrawal(self):
        cid = self.assign("CHINA", "退会", "太郎")
        self.f.sub(cid, "2026-06-10", status="CANCELED", canceled="2026-08-10",
                   paid=monthly_paid("2026-06-10", 2))
        r = compute_month(self.build().memberships, "2026-09", self.cfg, date(2026, 10, 2))
        c = row(r, "CHINA")
        self.assertEqual((c.targets, c.continued, c.withdrawn, c.rate), (1, 0, 1, 1.0))

    def test_payment_error_is_pending_then_judged_when_paid(self):
        cid = self.assign("TOMOKA", "保留", "花子")
        # 4回目（9/10）がカードエラー → 10/5 に支払い完了
        self.f.sub(cid, "2026-06-10", paid=monthly_paid("2026-06-10", 3) + ["2026-10-05"])
        ms = self.build().memberships
        sep = compute_month(ms, "2026-09", self.cfg, date(2026, 10, 2))
        self.assertEqual((row(sep, "TOMOKA").targets, row(sep, "TOMOKA").pending), (0, 1))
        # 10/2 時点ではまだ未払い（10/5 の決済は見えない）
        self.f.invoices[-1]["status"] = "UNPAID"
        self.f.invoices[-1]["payment_requests"] = [{"due_date": "2026-09-10"}]
        ms_before = self.build().memberships
        sep2 = compute_month(ms_before, "2026-09", self.cfg, date(2026, 10, 2))
        self.assertEqual(row(sep2, "TOMOKA").withdrawn, 0)
        self.assertIn("決済エラー", sep2.pending[0].reason)
        # 支払い完了後、10月に継続として判定
        oct_ = compute_month(ms, "2026-10", self.cfg, date(2026, 11, 1))
        t = row(oct_, "TOMOKA")
        self.assertEqual((t.targets, t.continued, t.withdrawn), (1, 1, 0))

    def test_paused_is_not_withdrawal(self):
        cid = self.assign("AMI", "休会", "花子")
        self.f.sub(cid, "2026-06-10", status="PAUSED", paid=monthly_paid("2026-06-10", 2))
        r = compute_month(self.build().memberships, "2026-09", self.cfg, date(2026, 10, 2))
        a = row(r, "AMI")
        self.assertEqual((a.targets, a.withdrawn, a.pending, a.headcount), (0, 0, 1, 1))
        self.assertEqual(r.pending[0].reason, "休会中")

    def test_plan_change_counts_payments_cumulatively(self):
        cid = self.assign("MIZUKI", "変更", "花子")
        # 30%offプラン（6〜7月）→ 通常プラン（8月〜）
        self.f.sub(cid, "2026-06-10", status="CANCELED", canceled="2026-08-10",
                   paid=monthly_paid("2026-06-10", 2))
        self.f.sub(cid, "2026-08-10", paid=monthly_paid("2026-08-10", 2))
        b = self.build()
        self.assertEqual(len(b.memberships), 1)
        r = compute_month(b.memberships, "2026-09", self.cfg, date(2026, 10, 2))
        m = row(r, "MIZUKI")
        self.assertEqual((m.targets, m.continued, m.withdrawn, m.headcount), (1, 1, 0, 1))

    def test_resume_after_long_gap_is_pause(self):
        cid = self.assign("AMI", "休会明け", "花子")
        # 6/10・7/10 決済 → 8/10 で一旦解約（2ヶ月休み）→ 10/10 再開
        self.f.sub(cid, "2026-06-10", status="CANCELED", canceled="2026-08-10",
                   paid=monthly_paid("2026-06-10", 2))
        self.f.sub(cid, "2026-10-10", paid=monthly_paid("2026-10-10", 2))
        ms = self.build().memberships
        self.assertEqual(len(ms), 1)
        sep = compute_month(ms, "2026-09", self.cfg, date(2026, 12, 1))
        a = row(sep, "AMI")
        self.assertEqual((a.headcount, a.targets, a.withdrawn, a.pending), (1, 0, 0, 1))
        nov = compute_month(ms, "2026-11", self.cfg, date(2026, 12, 1))
        self.assertEqual(row(nov, "AMI").continued, 1)

    def test_override_for_payments_outside_square(self):
        from incentive_tool.members import Override
        cid = self.assign("MIZUKI", "長期", "会員")
        # Square では6月開始・3回決済で10月解約だが、それ以前にAirペイで8回払っている
        self.f.sub(cid, "2026-06-16", status="CANCELED", canceled="2026-10-16",
                   paid=monthly_paid("2026-06-16", 3))
        b = build_memberships(self.f.data(), Assignments(self.rows, loaded=True), self.cfg,
                              {normalize("長期 会員"): Override(date(2025, 10, 16), 8)})
        r = compute_month(b.memberships, "2026-10", self.cfg, date(2026, 10, 20))
        self.assertEqual((row(r, "MIZUKI").targets, row(r, "MIZUKI").withdrawn), (0, 0))

    def test_override_external_member_counts_in_headcount(self):
        from incentive_tool.members import Override
        self.rows.append(Assignment("MIZUKI", "外部 払い"))
        b = build_memberships(self.f.data(), Assignments(self.rows, loaded=True), self.cfg,
                              {normalize("外部 払い"): Override(date(2025, 8, 21), 14, external_active=True)})
        r = compute_month(b.memberships, "2026-10", self.cfg, date(2026, 10, 3))
        self.assertEqual((row(r, "MIZUKI").headcount, row(r, "MIZUKI").targets), (1, 0))

    def test_override_keep_member(self):
        from incentive_tool.members import Override
        cid = self.assign("CHINA", "再登録", "予定")
        self.f.sub(cid, "2026-07-04", status="CANCELED", canceled="2026-10-04",
                   paid=monthly_paid("2026-07-04", 3))
        b = build_memberships(self.f.data(), Assignments(self.rows, loaded=True), self.cfg,
                              {normalize("再登録 予定"): Override(None, 0, keep_member=True)})
        c = row(compute_month(b.memberships, "2026-10", self.cfg, date(2026, 10, 10)), "CHINA")
        self.assertEqual((c.targets, c.withdrawn, c.undecided), (1, 0, 1))

    def test_current_month_is_provisional(self):
        c1 = self.assign("CHINA", "途中", "一")
        c2 = self.assign("CHINA", "途中", "二")
        self.f.sub(c1, "2026-07-01", paid=monthly_paid("2026-07-01", 4))      # 10/1 に4回目完了
        self.f.sub(c2, "2026-07-20", paid=monthly_paid("2026-07-20", 3))      # 10/20 が4回目（未到来）
        r = compute_month(self.build().memberships, "2026-10", self.cfg, date(2026, 10, 2))
        c = row(r, "CHINA")
        self.assertFalse(r.finalized)
        self.assertEqual((c.targets, c.continued, c.undecided), (2, 1, 1))
        self.assertEqual(c.judgement, "達成（途中経過）")
        self.assertEqual(c.headcount, 2)
        # 当月中に退会予定の会員は、途中経過の担当人数に含めない
        c3 = self.assign("CHINA", "途中", "三")
        self.f.sub(c3, "2026-01-15", canceled="2026-10-15", paid=monthly_paid("2026-01-15", 9))
        r = compute_month(self.build().memberships, "2026-10", self.cfg, date(2026, 10, 2))
        self.assertEqual(row(r, "CHINA").headcount, 2)

    def test_unassigned_customer(self):
        cid = self.f.customer("担当", "なし")
        self.f.sub(cid, "2026-06-10", paid=monthly_paid("2026-06-10", 4))
        b = self.build()
        self.assertEqual(len(b.unmatched), 1)
        r = compute_month(b.memberships, "2026-09", self.cfg, date(2026, 10, 2))
        self.assertEqual(row(r, "未割当").continued, 1)
        self.assertIsNone(row(r, "未割当").points)

    def test_points_rules(self):
        # 担当25人、判定対象4人のうち退会1人（25%）→ 達成、2.5ポイント
        for i in range(25):
            cid = self.assign("CHINA", "会員", f"{i:02d}")
            if i < 3:
                self.f.sub(cid, "2026-06-10", paid=monthly_paid("2026-06-10", 4))
            elif i == 3:
                self.f.sub(cid, "2026-06-10", status="CANCELED", canceled="2026-09-10",
                           paid=monthly_paid("2026-06-10", 3))
            else:
                self.f.sub(cid, "2026-01-05", paid=monthly_paid("2026-01-05", 9))
        r = compute_month(self.build().memberships, "2026-09", self.cfg, date(2026, 10, 2))
        c = row(r, "CHINA")
        self.assertEqual((c.headcount, c.targets, c.withdrawn), (24, 4, 1))  # 退会者は9月末時点で人数外
        self.assertTrue(c.achieved)
        self.assertEqual(c.points, 2.4)
        # 20人未満ならポイント0
        self.cfg.min_headcount = 30
        self.assertEqual(row(compute_month(self.build().memberships, "2026-09", self.cfg,
                                           date(2026, 10, 2)), "CHINA").points, 0)

    def test_half_year_excludes_riho_from_payout(self):
        recs = [
            {"month": "2026-10", "employee": "CHINA", "points": 3.0, "finalized": 1},
            {"month": "2026-10", "employee": "AMI", "points": 1.0, "finalized": 1},
            {"month": "2026-10", "employee": "RIHO", "points": 4.0, "finalized": 1},
            {"month": "2026-11", "employee": "CHINA", "points": 2.0, "finalized": 0},
        ]
        rows = {h.employee: h for h in compute_half(recs, self.cfg)}
        self.assertEqual(rows["CHINA"].final_amount, 225000)
        self.assertEqual(rows["AMI"].final_amount, 75000)
        self.assertIsNone(rows["RIHO"].final_amount)
        self.assertEqual(rows["RIHO"].final_points, 4.0)
        self.assertEqual(rows["CHINA"].expected_points, 5.0)
        self.assertEqual(half_of("2026-03")[1:], ("2025-10", "2026-03"))
        self.assertEqual(half_of("2026-04")[1:], ("2026-04", "2026-09"))


class EndToEndTests(unittest.TestCase):
    def test_daily_runs_freeze_previous_month(self):
        f = Fixture()
        cid = f.customer("山田", "花子")
        f.sub(cid, "2026-07-10", paid=monthly_paid("2026-07-10", 4))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "dump").mkdir()
            for k, v in f.data().items():
                (tmp / "dump" / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            (tmp / "assign.csv").write_text("担当社員,会員名\nCHINA,山田 花子\n", encoding="utf-8")
            conf = (ROOT / "config.toml").read_text(encoding="utf-8")
            conf = conf.replace('"data/assignments.csv"', json.dumps(str(tmp / "assign.csv")))
            conf = conf.replace('"state/incentive.db"', json.dumps(str(tmp / "state/db.sqlite")))
            conf = conf.replace('"output"', json.dumps(str(tmp / "out")))
            (tmp / "config.toml").write_text(conf, encoding="utf-8")
            base = ["--config", str(tmp / "config.toml"), "run", "--from-dump", str(tmp / "dump")]

            self.assertEqual(main(base + ["--as-of", "2026-10-05"]), 0)
            self.assertEqual(main(base + ["--as-of", "2026-10-05"]), 0)  # 同日2回目
            rows = read_csv(tmp / "out/1_月次記録.csv")
            oct_rows = [r for r in rows[1:] if r[0] == "2026-10"]
            self.assertEqual(len(oct_rows), 5)  # 重複しない
            self.assertTrue(all("途中経過" in r[7] for r in oct_rows))

            # 月が変わったら10月は確定し、データが変わっても上書きされない
            self.assertEqual(main(base + ["--as-of", "2026-11-02"]), 0)
            f.invoices[-1]["status"] = "REFUNDED"
            (tmp / "dump/invoices.json").write_text(json.dumps(f.invoices), encoding="utf-8")
            self.assertEqual(main(base + ["--as-of", "2026-11-03"]), 0)
            rows = read_csv(tmp / "out/1_月次記録.csv")
            china_oct = next(r for r in rows if r[0] == "2026-10" and r[1] == "CHINA")
            self.assertEqual((china_oct[4], china_oct[7]), ("1", "達成（確定）"))

            # 再集計を指示したときだけ更新される
            self.assertEqual(main(base + ["--as-of", "2026-11-03", "--month", "2026-10"]), 0)
            rows = read_csv(tmp / "out/1_月次記録.csv")
            china_oct = next(r for r in rows if r[0] == "2026-10" and r[1] == "CHINA")
            self.assertEqual(china_oct[4], "0")
            log_rows = read_csv(tmp / "out/4_更新ログ.csv")
            self.assertEqual(len(log_rows), 6)

    def test_failed_run_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            conf = (ROOT / "config.toml").read_text(encoding="utf-8")
            conf = conf.replace('"state/incentive.db"', json.dumps(str(tmp / "db.sqlite")))
            conf = conf.replace('"output"', json.dumps(str(tmp / "out")))
            (tmp / "config.toml").write_text(conf, encoding="utf-8")
            import os
            old = os.environ.pop("SQUARE_ACCESS_TOKEN", None)
            try:
                self.assertEqual(main(["--config", str(tmp / "config.toml"), "run"]), 1)
            finally:
                if old is not None:
                    os.environ["SQUARE_ACCESS_TOKEN"] = old
            log_rows = read_csv(tmp / "out/4_更新ログ.csv")
            self.assertEqual(log_rows[1][2], "失敗")
            self.assertIn("SQUARE_ACCESS_TOKEN", log_rows[1][8])


if __name__ == "__main__":
    unittest.main()


class SquareClientTests(unittest.TestCase):
    def test_pagination_and_rate_limit_retry(self):
        import io
        import urllib.error
        from unittest import mock

        from incentive_tool.square_client import SquareClient

        calls = []
        pages = [
            urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "0"}, io.BytesIO(b"{}")),
            {"invoices": [{"id": "a"}], "cursor": "c1"},
            {"invoices": [{"id": "b"}]},
        ]

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            item = pages.pop(0)
            if isinstance(item, Exception):
                raise item
            return Resp(json.dumps(item).encode())

        client = SquareClient("token", request_interval_sec=0)
        with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch("time.sleep"):
            out = client.list_invoices("LOC")
        self.assertEqual([i["id"] for i in out], ["a", "b"])
        self.assertEqual(len(calls), 3)
        self.assertIn("cursor=c1", calls[2])
        self.assertNotIn("cursor", calls[0])

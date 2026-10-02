"""4つのシート（月次記録 / 設定・集計 / 保留一覧 / 更新ログ）の表を作り、CSV か Googleスプレッドシートに書き込む。"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

from .aggregate import compute_half

log = logging.getLogger(__name__)

SHEET_MONTHLY = "月次記録"
SHEET_SUMMARY = "設定・集計"
SHEET_PENDING = "保留一覧"
SHEET_LOG = "更新ログ"


def _pct(v) -> str:
    return "" if v is None else f"{v * 100:.1f}%"


def _num(v):
    if v is None:
        return "-"
    return int(v) if float(v).is_integer() else round(v, 2)


def build_tables(store, cfg, updated_at: str) -> dict[str, list[list]]:
    monthly = store.monthly_records()
    t_month = [["月", "担当社員", "担当人数", "判定対象人数", "継続人数", "退会人数", "退会率",
                "判定（確定／途中経過）", "ポイント", "保留人数（持ち越し）", "未確定人数（当月）", "更新日時"]]
    order = {e: i for i, e in enumerate(cfg.employees)}
    for r in sorted(monthly, key=lambda r: (r["month"], order.get(r["employee"], 99)), reverse=False):
        t_month.append([r["month"], r["employee"], r["headcount"], r["targets"], r["continued"],
                        r["withdrawn"], _pct(r["rate"]) if r["rate"] is not None else "-",
                        r["judgement"], _num(r["points"]), r["pending"], r["undecided"], r["updated_at"]])

    t_sum = [
        ["項目", "値", "備考"],
        ["合格ライン（退会率）", _pct(cfg.pass_line), "この値以下なら達成"],
        ["最低担当人数", cfg.min_headcount, "達成かつこの人数以上でポイント付与"],
        ["ポイント計算", f"担当人数 ÷ {_num(cfg.points_divisor)}", ""],
        ["配る総額", cfg.total_amount, "円（config.toml の [payout] total_amount で変更）"],
        ["支給対象外", "、".join(sorted(cfg.not_eligible)) or "なし", "集計表には表示、支給額の計算からは除外"],
        ["最終更新日時", updated_at, ""],
        [],
        ["半期", "担当社員", "支給区分", "確定ポイント合計", "見込みポイント合計（当月含む）",
         "構成比（確定）", "支給額（確定）", "構成比（見込み）", "支給額（見込み）"],
    ]
    for h in compute_half(monthly, cfg):
        t_sum.append([h.half, h.employee, "支給対象" if h.eligible else "対象外（役員）",
                      _num(h.final_points), _num(h.expected_points),
                      _pct(h.final_ratio) if h.eligible else "対象外",
                      h.final_amount if h.eligible else "対象外",
                      _pct(h.expected_ratio) if h.eligible else "対象外",
                      h.expected_amount if h.eligible else "対象外"])

    t_pend = [["対象月（4回目の決済予定月）", "担当社員", "会員名", "理由", "決済完了回数", "最終エラー日", "Square顧客ID"]]
    for p in store.pending_records():
        t_pend.append([p["month"], p["employee"], p["name"], p["reason"], p["paid_count"],
                       p["last_error_date"], p["customer_ids"]])

    t_log = [["実行日時", "実行内容", "結果", "取得件数（サブスク）", "取得件数（請求書）", "取得件数（顧客）",
              "APIリクエスト数", "集計した月", "警告・エラー"]]
    for r in store.run_records():
        t_log.append([r["run_at"], r["mode"], r["status"], r["subscriptions"], r["invoices"], r["customers"],
                      r["api_requests"], r["months"], "\n".join(r["messages"])])
    return {SHEET_MONTHLY: t_month, SHEET_SUMMARY: t_sum, SHEET_PENDING: t_pend, SHEET_LOG: t_log}


def write_csv(tables: dict[str, list[list]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (name, rows) in enumerate(tables.items(), start=1):
        p = out_dir / f"{i}_{name.replace('・', '_')}.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)
        paths.append(p)
    return paths


def write_sheets(tables: dict[str, list[list]], spreadsheet_id: str) -> None:
    """サービスアカウントで Googleスプレッドシートに書き込む（gspread が必要）。"""
    try:
        import gspread  # type: ignore
    except ImportError as e:
        raise RuntimeError("Googleスプレッドシートへの書き込みには `pip install -r requirements.txt` が必要です") from e
    key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not key_file or not spreadsheet_id:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE（環境変数）と spreadsheet_id（config.toml）を設定してください")
    gc = gspread.service_account(filename=key_file)
    sh = gc.open_by_key(spreadsheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}
    for name, rows in tables.items():
        width = max((len(r) for r in rows), default=1)
        rows = [list(r) + [""] * (width - len(r)) for r in rows]
        ws = existing.get(name) or sh.add_worksheet(title=name, rows=max(len(rows), 10), cols=width)
        ws.clear()
        ws.update(rows, "A1", value_input_option="USER_ENTERED")

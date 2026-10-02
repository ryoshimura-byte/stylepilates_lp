"""コマンドライン入口。

  python -m incentive_tool run                    # 日次実行（当月を上書き、前月までを確定）
  python -m incentive_tool run --month 2026-09    # 指定月を再集計（確定済みでも上書き）
  python -m incentive_tool run --from-dump DIR    # 保存済みJSONで集計（APIを呼ばない）
  python -m incentive_tool fetch --dump DIR       # Square から取得したデータを保存
  python -m incentive_tool install-cron           # config.toml の時刻で cron に登録
  python -m incentive_tool serve                  # cron が使えない環境向けの常駐スケジューラ
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import shlex
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .aggregate import compute_month
from .config import load_config
from .members import add_months, build_memberships, month_of, month_range
from .names import load_assignments
from .outputs import build_tables, write_csv, write_sheets
from .square_client import SquareClient, fetch_all, load_dump, save_dump
from .store import Store

log = logging.getLogger("incentive_tool")


class RunLog(logging.Handler):
    """実行中の警告・エラーを集めて「更新ログ」に残す。"""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(f"[{record.levelname}] {record.getMessage()}")


def _setup_logging(cfg) -> RunLog:
    log_dir = cfg.path("state_db").parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(sys.stderr), logging.FileHandler(log_dir / "run.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    collector = RunLog()
    root.addHandler(collector)
    return collector


def _valid_month(s: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s):
        raise argparse.ArgumentTypeError("年月は YYYY-MM の形式で指定してください（例: 2026-10）")
    return s


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    collector = _setup_logging(cfg)
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    run_at = now.strftime("%Y-%m-%d %H:%M:%S")
    current = month_of(as_of)
    store = Store(cfg.path("state_db"))
    counts: dict = {}
    months_done: list[str] = []
    mode = f"再集計 {args.month}" if args.month else "日次更新"
    status = "失敗"
    try:
        if args.from_dump:
            data = load_dump(args.from_dump)
            log.info("保存済みデータ %s を使用します", args.from_dump)
        else:
            client = SquareClient.from_config(cfg.square)
            data = fetch_all(client, cfg.square.get("location_ids") or None)
            counts["api_requests"] = client.request_count
        counts.update({k: len(v) for k, v in data.items()})
        log.info("取得件数: サブスク %d / 請求書 %d / 顧客 %d",
                 counts["subscriptions"], counts["invoices"], counts["customers"])

        assignments = load_assignments(cfg.path("assignments"), cfg.path("name_aliases"), cfg.employees)
        built = build_memberships(data, assignments, cfg)
        for msg in built.unmatched:
            log.warning(msg + " → 未割当として集計")

        # 集計する月：開始月〜前月のうち未確定の月（失敗日の取り返し）＋当月＋指定月
        finalized = store.finalized_months()
        months = [m for m in month_range(cfg.start_month, add_months(current, -1)) if m not in finalized]
        if current >= cfg.start_month:
            months.append(current)
        if args.month and args.month not in months:
            months.append(args.month)
        months = sorted(set(months))

        updated_at = run_at
        current_result = None
        for m in months:
            result = compute_month(built.memberships, m, cfg, as_of)
            saved = store.save_month(result, updated_at, force=(m == args.month))
            if saved:
                months_done.append(m)
                log.info("%s を%sとして保存しました", m, "確定" if result.finalized else "途中経過")
            else:
                log.info("%s は確定済みのため上書きしません（再集計は --month %s）", m, m)
            _write_details(cfg, result)
            if m == current:
                current_result = result
        if current_result is None:
            current_result = compute_month(built.memberships, current, cfg, as_of)
        store.replace_pending(current_result)
        status = "成功（警告あり）" if collector.messages else "成功"
    except Exception as e:  # noqa: BLE001 失敗も必ずログに残す
        log.error("実行に失敗しました: %s", e)
        log.debug(traceback.format_exc())
        traceback.print_exc()
    finally:
        store.log_run(run_at, mode, status, counts, months_done, collector.messages)
        try:
            _write_outputs(cfg, store, run_at)
        except Exception as e:  # noqa: BLE001
            log.error("出力に失敗しました: %s", e)
            status = "失敗"
        store.close()
    return 0 if status != "失敗" else 1


def _write_outputs(cfg, store, updated_at: str) -> None:
    tables = build_tables(store, cfg, updated_at)
    mode = cfg.output.get("mode", "csv")
    if mode in ("csv", "both"):
        for p in write_csv(tables, cfg.path("output_dir")):
            log.info("CSV を出力しました: %s", p)
    if mode in ("sheets", "both"):
        write_sheets(tables, cfg.output.get("spreadsheet_id", ""))
        log.info("Googleスプレッドシートに書き込みました")


def _write_details(cfg, result) -> None:
    """会員ごとの判定結果（確認用・ローカルのみ）。"""
    d = cfg.path("output_dir") / "会員別判定"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{result.month}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["月", "担当社員", "会員名", "4回目の決済予定月", "判定", "判定日"])
        for x in sorted(result.details, key=lambda x: (x["employee"], x["name"])):
            w.writerow([x["month"], x["employee"], x["name"], x["scheduled"], x["result"], x["date"]])
        for p in result.pending:
            w.writerow([result.month, p.employee, p.name, p.month, "保留：" + p.reason,
                        p.last_error_date or ""])


def cmd_fetch(args) -> int:
    cfg = load_config(args.config)
    _setup_logging(cfg)
    client = SquareClient.from_config(cfg.square)
    data = fetch_all(client, cfg.square.get("location_ids") or None)
    save_dump(data, args.dump)
    log.info("保存しました: %s（%s）", args.dump, {k: len(v) for k, v in data.items()})
    return 0


def _run_command(cfg_path: str | None) -> str:
    base = Path(__file__).resolve().parent.parent
    cmd = f"cd {shlex.quote(str(base))} && {shlex.quote(sys.executable)} -m incentive_tool run"
    if cfg_path:
        cmd += f" --config {shlex.quote(str(Path(cfg_path).resolve()))}"
    return cmd + " >> state/logs/cron.log 2>&1"


def cmd_install_cron(args) -> int:
    cfg = load_config(args.config)
    hh, mm = map(int, cfg.schedule_time.split(":"))
    marker = "# incentive_tool daily"
    line = f"CRON_TZ={cfg.timezone}\n{mm} {hh} * * * {_run_command(args.config)} {marker}"
    if args.print_only:
        print(line)
        return 0
    (cfg.path("state_db").parent / "logs").mkdir(parents=True, exist_ok=True)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = cur.stdout.splitlines() if cur.returncode == 0 else []
    kept, skip_tz = [], False
    for ln in lines:
        if marker in ln:
            if kept and kept[-1].startswith("CRON_TZ=") and skip_tz:
                kept.pop()
            continue
        skip_tz = ln.startswith("CRON_TZ=")
        kept.append(ln)
    kept.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
    print(f"cron に登録しました（毎日 {cfg.schedule_time} {cfg.timezone}）:\n{line}")
    return 0


def cmd_serve(args) -> int:
    """毎日 config.toml の時刻に run を実行し続ける（時刻の変更は次回から反映）。"""
    while True:
        cfg = load_config(args.config)
        tz = ZoneInfo(cfg.timezone)
        hh, mm = map(int, cfg.schedule_time.split(":"))
        now = datetime.now(tz)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        print(f"次回実行: {nxt:%Y-%m-%d %H:%M} ({cfg.timezone})", flush=True)
        time.sleep(max(1.0, (nxt - datetime.now(tz)).total_seconds()))
        cmd_run(argparse.Namespace(config=args.config, month=None, from_dump=None, as_of=None))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="incentive_tool", description="担当顧客インセンティブ集計")
    ap.add_argument("--config", help="設定ファイル（既定: config.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="集計を実行して出力する")
    r.add_argument("--month", type=_valid_month, help="再集計する年月（YYYY-MM）。確定済みでも上書きします")
    r.add_argument("--from-dump", help="Square API の代わりに保存済みJSONを使う")
    r.add_argument("--as-of", help="基準日（YYYY-MM-DD、テスト用。既定は今日）")
    r.set_defaults(func=cmd_run)

    f = sub.add_parser("fetch", help="Square のデータを取得してJSONに保存する")
    f.add_argument("--dump", required=True)
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("install-cron", help="config.toml の時刻で毎日実行するよう cron に登録する")
    c.add_argument("--print-only", action="store_true", help="登録せずに cron の行を表示するだけ")
    c.set_defaults(func=cmd_install_cron)

    s = sub.add_parser("serve", help="常駐して毎日 config.toml の時刻に実行する")
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)

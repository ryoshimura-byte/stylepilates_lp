"""Square コネクタ（MCP）で取得したページ（JSON）をまとめて、`run --from-dump` 用のフォルダを作る。

使い方:
  python tools/build_dump.py OUT_DIR PAGE.json [PAGE.json ...]

各ページは subscriptions/search または invoices/list の応答そのまま。顧客名は請求書の宛先から取る。
"""
import json
import sys
from pathlib import Path


def main(out_dir, pages):
    subs, invoices, customers = {}, {}, {}
    for p in pages:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for s in data.get("subscriptions", []):
            subs[s["id"]] = s
        for i in data.get("invoices", []):
            invoices[i["id"]] = i
        for c in data.get("customers", []):
            customers[c["id"]] = c
    for i in sorted(invoices.values(), key=lambda x: x.get("updated_at", "")):
        r = i.get("primary_recipient") or {}
        cid = r.get("customer_id")
        if cid and cid not in customers:
            customers[cid] = {"id": cid, "given_name": r.get("given_name"), "family_name": r.get("family_name")}
    referenced = {x for s in subs.values() for x in s.get("invoice_ids", [])}
    missing = referenced - set(invoices)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("subscriptions", subs), ("invoices", invoices), ("customers", customers)):
        (out / f"{name}.json").write_text(json.dumps(list(rows.values()), ensure_ascii=False), encoding="utf-8")
    print(f"サブスク {len(subs)} / 請求書 {len(invoices)} / 顧客 {len(customers)} / 取得漏れの請求書 {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2:]))

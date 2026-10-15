"""退会届（Googleフォームの回答）の get_values 応答から、退会予定 CSV（会員名, 退会希望日）を作る。

使い方:
  python tools/forms_to_planned.py RESPONSE.json data/planned_cancellations.csv
列は A タイムスタンプ, C お名前（漢字）, E 退会希望日 を使う。同じ人が複数回出していればタイムスタンプが最新の行を採用する。
"""
import csv
import json
import sys

values = json.loads(open(sys.argv[1], encoding="utf-8").read()).get("values", [])
latest = {}
for row in values[1:]:
    row = row + [""] * (5 - len(row))
    ts, name, wish = row[0].strip(), row[2].replace("　", " ").strip(), row[4].strip()
    if not name or not wish:
        continue
    key = name.replace(" ", "")
    if key not in latest or ts >= latest[key][0]:
        latest[key] = (ts, name, wish.replace("/", "-"))
with open(sys.argv[2], "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["会員名", "退会希望日"])
    for ts, name, wish in latest.values():
        w.writerow([name, wish])
print(f"{sys.argv[2]}: {len(latest)} 名")

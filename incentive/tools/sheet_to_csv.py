"""Googleスプレッドシートの get_values の応答（JSON）を CSV に変換する。

使い方:
  python tools/sheet_to_csv.py RESPONSE.json OUT.csv
RESPONSE.json は {"values": [[...], ...]} の形（get_values の応答そのまま）。
"""
import csv
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
values = json.loads(open(src, encoding="utf-8").read()).get("values", [])
with open(dst, "w", encoding="utf-8", newline="") as f:
    csv.writer(f).writerows(values)
print(f"{dst}: {len(values)} 行")

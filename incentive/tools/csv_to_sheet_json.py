"""ツールが出力した CSV を、Googleスプレッドシートの update_values にそのまま渡せる JSON に変換する。

年月（YYYY-MM）と顧客IDは日付や数値に変換されないよう、先頭に ' を付ける。
使い方:
  python tools/csv_to_sheet_json.py OUT.csv
"""
import csv
import json
import re
import sys

rows = list(csv.reader(open(sys.argv[1], encoding="utf-8-sig")))
header = rows[0] if rows else []
id_cols = {i for i, h in enumerate(header) if "顧客ID" in h}
out = [header]
for r in rows[1:]:
    cells = []
    for i, v in enumerate(r):
        if v and (re.fullmatch(r"\d{4}-\d{2}", v) or i in id_cols):
            v = "'" + v
        cells.append(v)
    out.append(cells)
print(json.dumps(out, ensure_ascii=False))

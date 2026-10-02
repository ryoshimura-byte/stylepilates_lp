"""担当表の読み込みと、Square の顧客名との突き合わせ（表記ゆれ対応）。"""
from __future__ import annotations

import csv
import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

UNASSIGNED = "未割当"

_SPACES = re.compile(r"[\s　・･.,，、]+")
_HONORIFICS = ("さま", "様", "さん")


def normalize(name: str | None) -> str:
    """全角/半角・空白・カタカナ/ひらがな・大文字/小文字・敬称の違いを吸収したキー。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip()
    for h in _HONORIFICS:
        if s.endswith(h) and len(s) > len(h) + 1:
            s = s[: -len(h)]
    s = _SPACES.sub("", s).lower()
    # カタカナ → ひらがな
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)
    # 異体字のよくある例
    return s.replace("髙", "高").replace("﨑", "崎").replace("邊", "辺").replace("邉", "辺")


def customer_display_name(c: dict) -> str:
    family, given = (c.get("family_name") or "").strip(), (c.get("given_name") or "").strip()
    if family or given:
        return f"{family} {given}".strip()
    return (c.get("company_name") or c.get("nickname") or c.get("email_address") or c.get("id", "")).strip()


def customer_name_keys(c: dict) -> list[str]:
    """姓名の順序が逆に登録されていても照合できるよう、両方の並びを候補にする。"""
    family, given = c.get("family_name") or "", c.get("given_name") or ""
    keys = [normalize(family + given), normalize(given + family),
            normalize(c.get("nickname")), normalize(c.get("company_name"))]
    return [k for i, k in enumerate(keys) if k and k not in keys[:i]]


@dataclass
class Assignment:
    employee: str
    member_name: str
    customer_id: str = ""

    @property
    def key(self) -> str:
        return normalize(self.member_name)


@dataclass
class Assignments:
    rows: list[Assignment] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # normalize(Square名) -> normalize(担当表名)
    loaded: bool = False

    def __post_init__(self):
        self.by_key: dict[str, Assignment] = {}
        self.by_customer_id: dict[str, Assignment] = {}
        for a in self.rows:
            if a.customer_id:
                self.by_customer_id[a.customer_id] = a
            prev = self.by_key.get(a.key)
            if prev and prev.employee != a.employee:
                log.warning("担当表で「%s」が %s と %s に重複しています（%s を採用）",
                            a.member_name, prev.employee, a.employee, prev.employee)
                continue
            self.by_key.setdefault(a.key, a)

    def match(self, customer: dict) -> tuple[Assignment | None, str]:
        """(担当行, 照合方法) を返す。見つからなければ (None, 候補メッセージ)。"""
        cid = customer.get("id", "")
        if cid in self.by_customer_id:
            return self.by_customer_id[cid], "顧客ID"
        keys = customer_name_keys(customer)
        for k in keys:
            if k in self.by_key:
                return self.by_key[k], "名前"
        for k in keys:
            if k in self.aliases and self.aliases[k] in self.by_key:
                return self.by_key[self.aliases[k]], "別名表"
        hint = difflib.get_close_matches(keys[0] if keys else "", list(self.by_key), n=1, cutoff=0.5)
        if hint:
            return None, f"候補: {self.by_key[hint[0]].member_name}（{self.by_key[hint[0]].employee}）"
        return None, ""


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def load_assignments(path: Path, alias_path: Path | None, employees: list[str]) -> Assignments:
    if not path.exists():
        log.warning("担当表 %s が見つかりません。全員を「%s」として集計します", path, UNASSIGNED)
        return Assignments()
    rows = []
    known = {e.upper(): e for e in employees}
    for i, r in enumerate(_read_csv(path), start=2):
        emp = r.get("担当社員", "")
        name = r.get("会員名", "")
        if not emp or not name:
            continue
        canonical = known.get(unicodedata.normalize("NFKC", emp).strip().upper())
        if not canonical:
            log.warning("担当表 %d行目: 社員「%s」は設定ファイルの社員一覧にありません", i, emp)
            canonical = emp
        rows.append(Assignment(canonical, name, r.get("顧客ID", "")))
    aliases = {}
    if alias_path and alias_path.exists():
        for r in _read_csv(alias_path):
            src, dst = r.get("Square上の名前", ""), r.get("担当表の会員名", "")
            if src and dst:
                aliases[normalize(src)] = normalize(dst)
    return Assignments(rows, aliases, loaded=True)

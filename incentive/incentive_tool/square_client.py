"""Square API からサブスクリプション・請求書・顧客を取得する。

- ページネーション（cursor）を最後まで辿る
- 429（レート制限）/ 5xx / 通信エラーは Retry-After または指数バックオフで再試行
- アクセストークンは環境変数 SQUARE_ACCESS_TOKEN から読む
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE_URLS = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}


class SquareError(RuntimeError):
    pass


class SquareClient:
    def __init__(self, token: str, environment: str = "production", api_version: str = "2025-01-23",
                 request_interval_sec: float = 0.25, max_retries: int = 5):
        if not token:
            raise SquareError("SQUARE_ACCESS_TOKEN が設定されていません（環境変数または .env に設定してください）")
        self.token = token
        self.base_url = BASE_URLS[environment]
        self.api_version = api_version
        self.interval = request_interval_sec
        self.max_retries = max_retries
        self.request_count = 0
        self._last_request = 0.0

    @classmethod
    def from_config(cls, square_cfg: dict) -> "SquareClient":
        return cls(
            token=os.environ.get("SQUARE_ACCESS_TOKEN", ""),
            environment=square_cfg.get("environment", "production"),
            api_version=square_cfg.get("api_version", "2025-01-23"),
            request_interval_sec=float(square_cfg.get("request_interval_sec", 0.25)),
            max_retries=int(square_cfg.get("max_retries", 5)),
        )

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Square-Version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for attempt in range(self.max_retries + 1):
            wait = self.interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.request_count += 1
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code == 429 or e.code >= 500:
                    if attempt < self.max_retries:
                        retry_after = e.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                        log.warning("Square API %s %s: HTTP %s。%.0f秒後に再試行します", method, path, e.code, delay)
                        time.sleep(delay)
                        continue
                raise SquareError(f"Square API {method} {path} が失敗しました: HTTP {e.code} {detail}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    delay = 2 ** attempt
                    log.warning("Square API %s %s: 通信エラー(%s)。%.0f秒後に再試行します", method, path, e, delay)
                    time.sleep(delay)
                    continue
                raise SquareError(f"Square API {method} {path} に接続できません: {e}") from e
        raise SquareError("unreachable")

    # ------------------------------------------------------------ endpoints
    def list_location_ids(self) -> list[str]:
        res = self._request("GET", "/v2/locations")
        return [loc["id"] for loc in res.get("locations", [])]

    def search_subscriptions(self, location_ids: list[str] | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            body: dict = {"limit": 200}
            if location_ids:
                body["query"] = {"filter": {"location_ids": location_ids}}
            if cursor:
                body["cursor"] = cursor
            res = self._request("POST", "/v2/subscriptions/search", body=body)
            out.extend(res.get("subscriptions", []))
            cursor = res.get("cursor")
            if not cursor:
                return out

    def list_invoices(self, location_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            res = self._request("GET", "/v2/invoices",
                                params={"location_id": location_id, "limit": 200, "cursor": cursor})
            out.extend(res.get("invoices", []))
            cursor = res.get("cursor")
            if not cursor:
                return out

    def bulk_retrieve_customers(self, customer_ids: list[str]) -> list[dict]:
        out = []
        ids = sorted(set(customer_ids))
        for i in range(0, len(ids), 100):
            res = self._request("POST", "/v2/customers/bulk-retrieve", body={"customer_ids": ids[i:i + 100]})
            for cid, item in (res.get("responses") or {}).items():
                if item.get("customer"):
                    out.append(item["customer"])
                else:
                    log.warning("顧客 %s を取得できませんでした: %s", cid, item.get("errors"))
        return out


def fetch_all(client: SquareClient, location_ids: list[str] | None = None) -> dict:
    """集計に必要なデータを一括取得する。"""
    locs = location_ids or client.list_location_ids()
    subs = client.search_subscriptions(locs)
    invoices: list[dict] = []
    for loc in locs:
        invoices.extend(client.list_invoices(loc))
    customers = client.bulk_retrieve_customers([s["customer_id"] for s in subs if s.get("customer_id")])
    return {"subscriptions": subs, "invoices": invoices, "customers": customers}


def load_dump(directory: str | Path) -> dict:
    """fetch --dump で保存したJSON（またはテスト用データ）を読み込む。"""
    d = Path(directory)
    data = {}
    for key in ("subscriptions", "invoices", "customers"):
        p = d / f"{key}.json"
        data[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return data


def save_dump(data: dict, directory: str | Path) -> None:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        (d / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

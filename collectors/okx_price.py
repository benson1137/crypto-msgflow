"""OKX 1H OHLCV price collector — recoverable price history for verdict backfill.

verdicts.realized_ret needs the price at an arbitrary past ts (judgment time)
and at ts+window. oi_funding has no continuous price series, so this collector
maintains one in price_candles. Uses history-candles (paginates back via
`after`), so a cron outage self-heals on the next run.
"""
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

OKX_BASE = "https://www.okx.com"
HISTORY_CANDLES = "/api/v5/market/history-candles"


class OkxPriceCollector(BaseCollector):
    """1H OHLCV → price_candles. Backbone for verdict return calculation."""

    name = "okx_price"
    schedule = "20 */12 * * *"  # twice daily, offset from okx_oi_1h (:30)
    max_staleness = timedelta(hours=14)
    use_env_proxy = True

    # How far back to keep the price series. verdict windows are ≤7d, but a
    # wider buffer lets us backfill verdicts logged during an outage.
    LOOKBACK_DAYS = 45

    def __init__(self):
        super().__init__()
        self.instruments = self.config.okx.instruments
        self.limiter = get_limiter("okx", rate_per_min=300)

    def fetch(self) -> list[dict[str, Any]]:
        cutoff_ms = int((utcnow() - timedelta(days=self.LOOKBACK_DAYS))
                        .replace(tzinfo=UTC).timestamp() * 1000)
        rows = []
        with self.make_client() as client:
            for inst_id in self.instruments:
                after = None
                for _ in range(20):  # 20×100 = 2000 bars cap (~83 days of 1H)
                    self.limiter.wait()
                    params = {"instId": inst_id, "bar": "1H", "limit": "100"}
                    if after:
                        params["after"] = after
                    resp = client.get(f"{OKX_BASE}{HISTORY_CANDLES}", params=params)
                    if resp.status_code == 429:
                        from collectors.rate_limit import RateLimited
                        raise RateLimited(int(resp.headers.get("Retry-After", 60)))
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("code") != "0":
                        raise SchemaDrift(f"OKX error {data.get('code')}: {data.get('msg')}")
                    batch = data.get("data", [])
                    if not batch:
                        break
                    for c in batch:
                        ts_ms = int(c[0])
                        rows.append({
                            "inst_id": inst_id,
                            "ts": datetime.fromtimestamp(ts_ms / 1000, UTC).replace(tzinfo=None),
                            "bar": "1H",
                            "open": float(c[1]), "high": float(c[2]),
                            "low": float(c[3]), "close": float(c[4]),
                            "vol": float(c[5]) if c[5] else None,
                            "fetched_at": utcnow(),
                        })
                    oldest = min(int(c[0]) for c in batch)
                    if oldest <= cutoff_ms:
                        break
                    after = str(oldest)
                    time.sleep(0.15)
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"inst_id", "ts", "bar", "close"}
        if not required.issubset(set(rows[0].keys())):
            raise SchemaDrift(f"Missing fields: {required - set(rows[0].keys())}")
        if len(rows) < 2 * len(self.instruments):
            raise SchemaDrift(f"Only {len(rows)} candles — history endpoint drift?")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        import duckdb
        conn = duckdb.connect(str(self.db_path))
        n = 0
        for r in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO price_candles
                    (inst_id, ts, bar, open, high, low, close, vol, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [r["inst_id"], r["ts"], r["bar"], r["open"], r["high"],
                     r["low"], r["close"], r["vol"], r["fetched_at"]],
                )
                n += 1
            except Exception:
                pass  # duplicate (inst_id, ts, bar)
        conn.close()
        return n


def main():
    OkxPriceCollector().run()


if __name__ == "__main__":
    main()

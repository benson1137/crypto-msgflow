"""OKX Open Interest + Funding collector — P0 (unrecoverable data)."""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

OKX_BASE = "https://www.okx.com"


class OkxOiCollector(BaseCollector):
    """
    OKX Open Interest + Funding Rate collector.

    P0: OI history API window is limited. Data not collected tonight is
    permanently lost. Gap backfill is mandatory (see backfill()).

    Endpoints (public, no auth):
    - /api/v5/public/open-interest?instId=...      → current OI
    - /api/v5/public/funding-rate?instId=...        → current funding
    - /api/v5/public/mark-price?instId=...          → mark price
    """

    name = "okx_oi"
    schedule = "*/5 * * * *"  # every 5 min (1m granularity)
    max_staleness = timedelta(minutes=15)

    # OKX is only reachable via the mihomo proxy from this datacenter IP.
    use_env_proxy = True

    def __init__(self, granularity: str = "1m"):
        super().__init__()
        self.instruments = self.config.okx.instruments
        self.granularity = granularity
        self.limiter = get_limiter("okx", rate_per_min=300)  # 5 req/s

    def _client(self) -> httpx.Client:
        """HTTP client honoring proxy policy (OKX needs mihomo, §3.3)."""
        return self.make_client()

    def _get(self, client: httpx.Client, path: str, params: dict) -> dict:
        self.limiter.wait()
        resp = client.get(f"{OKX_BASE}{path}", params=params)
        if resp.status_code == 429:
            from collectors.rate_limit import RateLimited
            raise RateLimited(int(resp.headers.get("Retry-After", 60)))
        resp.raise_for_status()
        data = resp.json()
        # OKX wraps everything: {"code":"0","msg":"","data":[...]}
        if data.get("code") != "0":
            raise SchemaDrift(f"OKX error code={data.get('code')} msg={data.get('msg')}")
        return data

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch current OI + funding + mark price for each instrument."""
        rows = []
        now = utcnow()

        with self._client() as client:
            for inst_id in self.instruments:
                # Open interest
                oi_data = self._get(client, "/api/v5/public/open-interest",
                                    {"instType": "SWAP", "instId": inst_id})
                oi = oi_data["data"][0] if oi_data["data"] else {}

                # Funding rate
                fr_data = self._get(client, "/api/v5/public/funding-rate",
                                    {"instId": inst_id})
                fr = fr_data["data"][0] if fr_data["data"] else {}

                # Mark price
                mp_data = self._get(client, "/api/v5/public/mark-price",
                                    {"instType": "SWAP", "instId": inst_id})
                mp = mp_data["data"][0] if mp_data["data"] else {}

                rows.append({
                    "inst_id": inst_id,
                    "ts": now,
                    "granularity": self.granularity,
                    "oi_ccy": float(oi["oiCcy"]) if oi.get("oiCcy") else None,
                    "oi_usd": float(oi["oiUsd"]) if oi.get("oiUsd") else None,
                    "funding_rate": float(fr["fundingRate"]) if fr.get("fundingRate") else None,
                    "mark_price": float(mp["markPx"]) if mp.get("markPx") else None,
                    "fetched_at": now,
                })

        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"inst_id", "ts", "granularity", "oi_ccy", "oi_usd",
                    "funding_rate", "mark_price", "fetched_at"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        import duckdb
        conn = duckdb.connect(str(self.db_path))
        inserted = 0
        for row in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO oi_funding
                    (inst_id, ts, granularity, oi_ccy, oi_usd, funding_rate, mark_price, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [row["inst_id"], row["ts"], row["granularity"], row["oi_ccy"],
                     row["oi_usd"], row["funding_rate"], row["mark_price"], row["fetched_at"]],
                )
                inserted += 1
            except Exception:
                pass  # duplicate (inst_id, ts, granularity)
        conn.close()
        return inserted

    def detect_gaps(self, hours: int = 24) -> list[tuple]:
        """
        Detect gaps in 1m data over the last N hours.

        Returns list of (inst_id, gap_start, gap_end) tuples.
        A gap is any interval > 2x the expected cadence with no data.
        """
        import duckdb
        conn = duckdb.connect(str(self.db_path))
        gaps = []

        for inst_id in self.instruments:
            df = conn.execute(
                """
                SELECT ts FROM oi_funding
                WHERE inst_id = ? AND granularity = ?
                  AND ts > ? - INTERVAL 1 HOUR * ?
                ORDER BY ts
                """,
                [inst_id, self.granularity, utcnow(), hours],
            ).fetchdf()

            if len(df) < 2:
                continue

            expected = timedelta(minutes=5)  # 1m granularity, collected every 5min
            for i in range(1, len(df)):
                delta = df.iloc[i]["ts"] - df.iloc[i - 1]["ts"]
                if delta > 2 * expected:
                    gaps.append((inst_id, df.iloc[i - 1]["ts"], df.iloc[i]["ts"]))

        conn.close()
        return gaps


def main():
    collector = OkxOiCollector()
    collector.run()

    # Report gaps on each run (P0: gaps = permanent loss if not backfilled)
    gaps = collector.detect_gaps()
    if gaps:
        print(f"⚠️  Detected {len(gaps)} gaps in OI data:")
        for inst_id, start, end in gaps:
            print(f"   {inst_id}: {start} → {end}")


if __name__ == "__main__":
    main()

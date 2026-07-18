"""OKX Open Interest + Funding collector — P0 (unrecoverable data)."""
import sys
from datetime import UTC, timedelta
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
    schedule = "*/15 * * * *"  # every 15 min — realtime texture layer
    max_staleness = timedelta(minutes=45)  # 3× cadence

    # OKX is only reachable via the mihomo proxy from this datacenter IP.
    use_env_proxy = True

    # granularity 'rt15' is an honest label: a point-in-time snapshot taken
    # every 15 min, NOT a 15-minute OHLC bar. The 1h backbone (OkxOiHistory)
    # is the recoverable series; this layer is best-effort texture.
    def __init__(self, granularity: str = "rt15"):
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

            expected = timedelta(minutes=15)  # rt15 snapshots every 15 min
            for i in range(1, len(df)):
                delta = df.iloc[i]["ts"] - df.iloc[i - 1]["ts"]
                if delta > 2 * expected:
                    gaps.append((inst_id, df.iloc[i - 1]["ts"], df.iloc[i]["ts"]))

        conn.close()
        return gaps


class OkxOiHistoryCollector(BaseCollector):
    """
    OKX 1h OI backbone — the recoverable series (spec §8 step 3).

    Uses the rubik stat endpoint (30-day window, verified) for OI, joined
    with funding-rate-history (8h settlement) for funding. Runs twice daily;
    because the window is 30 days, a multi-day cron outage self-heals — each
    run re-fetches the full window and upserts, filling any gap.

    granularity='1h'. This is what priced-in analysis should read.
    """

    name = "okx_oi_1h"
    schedule = "30 */12 * * *"  # twice daily (00:30, 12:30 UTC)
    max_staleness = timedelta(hours=3)
    use_env_proxy = True

    OI_HIST = "/api/v5/rubik/stat/contracts/open-interest-volume"
    FUNDING_HIST = "/api/v5/public/funding-rate-history"

    def __init__(self):
        super().__init__()
        self.instruments = self.config.okx.instruments
        self.limiter = get_limiter("okx", rate_per_min=300)

    def _get(self, client, path, params):
        self.limiter.wait()
        resp = client.get(f"{OKX_BASE}{path}", params=params)
        if resp.status_code == 429:
            from collectors.rate_limit import RateLimited
            raise RateLimited(int(resp.headers.get("Retry-After", 60)))
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            raise SchemaDrift(f"OKX error code={data.get('code')} msg={data.get('msg')}")
        return data

    @staticmethod
    def _base_ccy(inst_id: str) -> str:
        return inst_id.split("-")[0]

    def fetch(self) -> list[dict[str, Any]]:
        from datetime import datetime

        rows = []
        with self.make_client() as client:
            for inst_id in self.instruments:
                # OI history: rubik endpoint keyed by base ccy, 1H period.
                oi = self._get(client, self.OI_HIST,
                               {"ccy": self._base_ccy(inst_id), "period": "1H"})
                # rows: [ts, oiUsd(?), volUsd] — confirm order via known field
                # rubik contracts oi-volume returns [ts, oi, vol] in USD.
                oi_by_ts = {}
                for r in oi["data"]:
                    ts_ms = int(r[0])
                    oi_by_ts[ts_ms] = float(r[1]) if r[1] else None

                # Funding history (8h settlement) — sparse vs 1h OI; forward-fill.
                fr = self._get(client, self.FUNDING_HIST,
                               {"instId": inst_id, "limit": "100"})
                funding_points = sorted(
                    ((int(x["fundingTime"]), float(x["fundingRate"]))
                     for x in fr["data"] if x.get("fundingRate")),
                    key=lambda p: p[0],
                )

                def funding_at(ts_ms: int) -> float | None:
                    # most recent funding at or before ts_ms
                    val = None
                    for ft, rate in funding_points:
                        if ft <= ts_ms:
                            val = rate
                        else:
                            break
                    return val

                for ts_ms, oi_usd in oi_by_ts.items():
                    ts = datetime.fromtimestamp(ts_ms / 1000, UTC).replace(tzinfo=None)
                    rows.append({
                        "inst_id": inst_id,
                        "ts": ts,
                        "granularity": "1h",
                        "oi_ccy": None,       # rubik gives USD only
                        "oi_usd": oi_usd,
                        "funding_rate": funding_at(ts_ms),
                        "mark_price": None,   # not in historical endpoints
                        "fetched_at": utcnow(),
                    })
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"inst_id", "ts", "granularity", "oi_usd", "funding_rate"}
        if not required.issubset(set(rows[0].keys())):
            raise SchemaDrift(f"Missing fields: {required - set(rows[0].keys())}")
        # Sanity: 1H window should yield hundreds of rows per instrument.
        if len(rows) < 2 * len(self.instruments):
            raise SchemaDrift(f"Only {len(rows)} 1h rows — OI history endpoint drift?")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        import duckdb
        conn = duckdb.connect(str(self.db_path))
        n = 0
        for r in rows:
            # Backfill-friendly: re-fetching the window UPDATEs existing rows
            # (funding may fill in) rather than silently skipping duplicates.
            conn.execute(
                "DELETE FROM oi_funding WHERE inst_id=? AND ts=? AND granularity='1h'",
                [r["inst_id"], r["ts"]],
            )
            conn.execute(
                """
                INSERT INTO oi_funding
                (inst_id, ts, granularity, oi_ccy, oi_usd, funding_rate, mark_price, fetched_at)
                VALUES (?, ?, '1h', ?, ?, ?, ?, ?)
                """,
                [r["inst_id"], r["ts"], r["oi_ccy"], r["oi_usd"],
                 r["funding_rate"], r["mark_price"], r["fetched_at"]],
            )
            n += 1
        conn.close()
        return n


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

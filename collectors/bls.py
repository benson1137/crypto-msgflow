"""BLS (Bureau of Labor Statistics) macro collector — collector.

Fetches labor/inflation time series via the official BLS v2 API:
- CPI, Core CPI, unemployment rate, nonfarm payrolls, avg hourly earnings.

Why direct BLS (not just FRED): BLS publishes at the release instant,
FRED mirrors with lag. For surprise-based analysis the release moment matters.
Historical values still land in macro_series for backtesting.
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

# period code (M01..M12) → month number. M13=annual avg, S01/S02=semiannual,
# Q01..Q04=quarter. We only store monthly/quarterly points as a real date.
_MONTH_PERIODS = {f"M{m:02d}": m for m in range(1, 13)}
_QUARTER_PERIODS = {"Q01": 1, "Q02": 4, "Q03": 7, "Q04": 10}


class BlsCollector(BaseCollector):
    """
    BLS v2 time-series collector → macro_series (source='bls').

    P1: authoritative but re-fetchable (BLS keeps long history).
    """

    name = "bls"
    schedule = "0 13 * * *"  # 1pm UTC, after US morning releases
    max_staleness = timedelta(days=45)  # monthly data, 2-6wk publication lag
    # obs_date (观测月) structurally lags publication by 4-6 weeks, so it
    # never reflects collector health — a June CPI row is published mid-July
    # and is already ~46d "old" by obs_date. Liveness comes from heartbeat
    # (§6.2) + consecutive-empty (§6.3) instead. Same reasoning as x_kol.
    staleness_by_data_ts = False
    # Counterintuitive: BLS times out on direct connection from this
    # datacenter IP but succeeds via mihomo. Opposite of FRED (§3.3).
    use_env_proxy = True

    def __init__(self):
        super().__init__()
        self.api_key = self.config.bls.api_key  # optional; raises daily quota 25→500
        self.series_ids = self.config.bls.series
        self.base_url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        self.limiter = get_limiter("bls", rate_per_min=30)

    def fetch(self) -> list[dict[str, Any]]:
        this_year = utcnow().year
        payload: dict[str, Any] = {
            "seriesid": self.series_ids,
            "startyear": str(this_year - 1),  # incremental: last 2 years
            "endyear": str(this_year),
        }
        if self.api_key:
            payload["registrationkey"] = self.api_key

        # mihomo has partially-broken exit nodes: BLS via proxy succeeds in
        # ~1.4s on a good node but hangs to timeout on a bad one (~25% hit
        # rate observed). Short timeout + retries lands on a good node fast
        # instead of blocking the whole run on one stuck connection.
        import httpx

        self.limiter.wait()
        data = None
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                with self.make_client(timeout=8) as client:
                    resp = client.post(self.base_url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                continue
        if data is None:
            raise SchemaDrift(f"BLS unreachable after 6 attempts (proxy flaky): {last_err}")

        if data.get("status") != "REQUEST_SUCCEEDED":
            raise SchemaDrift(f"BLS status={data.get('status')}: {data.get('message')}")

        results = data.get("Results", {}).get("series")
        if results is None:
            raise SchemaDrift("Missing Results.series in BLS response")

        all_rows = []
        for series in results:
            sid = series["seriesID"]
            for obs in series.get("data", []):
                obs_date = self._period_to_date(obs["year"], obs["period"])
                if obs_date is None:
                    continue  # skip annual (M13) / semiannual (S01/S02) aggregates
                # BLS uses "-" for missing/unavailable values
                raw = obs.get("value", "")
                value = None if raw in ("", "-") else float(raw)
                all_rows.append({
                    "series_id": sid,
                    "obs_date": obs_date.isoformat(),
                    "value": value,
                    "source": "bls",
                    "fetched_at": utcnow(),
                })
        return all_rows

    @staticmethod
    def _period_to_date(year: str, period: str) -> date | None:
        """Map (year, period) to first-of-period date. None for aggregates."""
        y = int(year)
        if period in _MONTH_PERIODS:
            return date(y, _MONTH_PERIODS[period], 1)
        if period in _QUARTER_PERIODS:
            return date(y, _QUARTER_PERIODS[period], 1)
        return None  # M13 annual, S01/S02 semiannual — skip

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"series_id", "obs_date", "value", "source", "fetched_at"}
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
                    INSERT INTO macro_series (series_id, obs_date, value, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [row["series_id"], row["obs_date"], row["value"],
                     row["source"], row["fetched_at"]],
                )
                inserted += 1
            except Exception:
                pass  # duplicate PK, skip
        conn.close()
        return inserted


def main():
    BlsCollector().run()


if __name__ == "__main__":
    main()

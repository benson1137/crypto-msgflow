"""BEA (Bureau of Economic Analysis) collector.

Fetches the PCE price index — the Fed's preferred inflation gauge — direct
from the source (NIPA table T20804), rather than via FRED's mirror.

We store two lines into macro_series (source='bea'):
- Line 1  → headline PCE price index          (series_id 'PCEPI')
- Line 25 → core PCE (ex food & energy)       (series_id 'PCEPILFE')

series_id names mirror FRED's for downstream consistency, but source='bea'
keeps them distinct from any FRED-sourced copy (§2.1 source-in-PK).

NOTE: BEA returns HTTP 200 even for errors (inactive key, bad params) — the
error hides in Results.Error. We check that explicitly (classic §P1 trap).
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

BASE_URL = "https://apps.bea.gov/api/data"

# NIPA T20804 line → our series_id. Only the two the Fed watches.
LINE_TO_SERIES = {
    "1": "PCEPI",       # headline PCE price index
    "25": "PCEPILFE",   # core PCE (ex food & energy)
}


class BeaCollector(BaseCollector):
    """BEA PCE price index → macro_series (source='bea')."""

    name = "bea"
    schedule = "0 14 * * *"  # 2pm UTC; PCE releases ~8:30am ET late in month
    # Monthly series, published ~4wk after the reference month, so obs_date
    # lags structurally — same reasoning as bls (§staleness_by_data_ts).
    staleness_by_data_ts = False
    max_staleness = timedelta(days=45)
    use_env_proxy = False  # apps.bea.gov reachable direct (US gov, like FRED)

    def __init__(self):
        super().__init__()
        self.api_key = self.config.bea.api_key
        self.limiter = get_limiter("bea", rate_per_min=60)

    def fetch(self) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SchemaDrift("bea.api_key not configured")

        this_year = utcnow().year
        params = {
            "UserID": self.api_key,
            "method": "GetData",
            "DataSetName": "NIPA",
            "TableName": "T20804",
            "Frequency": "M",
            "Year": f"{this_year - 1},{this_year}",  # incremental: last 2 years
            "ResultFormat": "JSON",
        }

        self.limiter.wait()
        with self.make_client(timeout=30) as client:
            resp = client.get(BASE_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()

        results = payload.get("BEAAPI", {}).get("Results", {})
        # BEA returns 200 even on error — the failure hides in Results.Error.
        if isinstance(results, dict) and "Error" in results:
            err = results["Error"]
            raise SchemaDrift(f"BEA API error {err.get('APIErrorCode')}: {err.get('APIErrorDescription')}")

        data = results.get("Data") if isinstance(results, dict) else None
        if data is None:
            raise SchemaDrift("Missing Results.Data in BEA response")

        rows = []
        for row in data:
            line = row.get("LineNumber")
            if line not in LINE_TO_SERIES:
                continue  # only headline + core PCE
            obs_date = self._period_to_date(row.get("TimePeriod", ""))
            if obs_date is None:
                continue
            raw = row.get("DataValue", "")
            value = None if raw in ("", "NA") else float(raw.replace(",", ""))
            rows.append({
                "series_id": LINE_TO_SERIES[line],
                "obs_date": obs_date.isoformat(),
                "value": value,
                "source": "bea",
                "fetched_at": utcnow(),
            })
        return rows

    @staticmethod
    def _period_to_date(period: str) -> date | None:
        """'2025M01' → date(2025, 1, 1). None if not a monthly period."""
        if "M" not in period:
            return None
        try:
            y, m = period.split("M")
            return date(int(y), int(m), 1)
        except (ValueError, IndexError):
            return None

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"series_id", "obs_date", "value", "source", "fetched_at"}
        if not required.issubset(set(rows[0].keys())):
            raise SchemaDrift(f"Missing fields: {required - set(rows[0].keys())}")
        # We asked for 2 lines × ~months. If neither PCE line came back, the
        # table layout drifted — fail loud rather than store nothing silently.
        series = {r["series_id"] for r in rows}
        if not series & {"PCEPI", "PCEPILFE"}:
            raise SchemaDrift("Neither PCEPI nor PCEPILFE present — T20804 layout drift?")

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
                pass  # duplicate PK
        conn.close()
        return inserted


def main():
    BeaCollector().run()


if __name__ == "__main__":
    main()

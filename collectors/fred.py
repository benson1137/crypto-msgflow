"""FRED macro data collector."""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow


class FredCollector(BaseCollector):
    """
    Federal Reserve Economic Data (FRED) collector.

    Fetches macro time series:
    - WALCL: Fed balance sheet
    - WTREGEN: TGA (weekly avg)
    - RRPONTSYD: Overnight RRP
    - DGS10, T10YIE, DFII10: rates
    """

    name = "fred"
    schedule = "0 13 * * *"  # 1pm UTC (covers US morning releases)

    # Max staleness varies by series frequency
    # WALCL/WTREGEN: weekly (Wed) → 8 days
    # Others: daily → 4 days (covers weekend + holiday)
    max_staleness = timedelta(days=8)
    use_env_proxy = False  # FRED (US gov): direct, bypass mihomo (§3.3)

    def __init__(self):
        super().__init__()
        self.api_key = self.config.fred.api_key
        self.series_ids = self.config.fred.series
        self.base_url = "https://api.stlouisfed.org/fred/series/observations"
        self.limiter = get_limiter("fred", rate_per_min=60)

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch all configured series."""
        all_rows = []

        for series_id in self.series_ids:
            self.limiter.wait()

            # Fetch last 30 days (incremental)
            params = {
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "observation_start": (utcnow() - timedelta(days=30)).strftime("%Y-%m-%d"),
            }

            with self.make_client() as client:
                resp = client.get(self.base_url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if "observations" not in data:
                raise SchemaDrift(f"Missing 'observations' key in FRED response for {series_id}")

            for obs in data["observations"]:
                # FRED uses "." for missing values
                value = None if obs["value"] == "." else float(obs["value"])

                all_rows.append({
                    "series_id": series_id,
                    "obs_date": obs["date"],
                    "value": value,
                    "source": "fred",
                    "fetched_at": utcnow(),
                })

        return all_rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        """Validate schema."""
        if not rows:
            return

        required = {"series_id", "obs_date", "value", "source", "fetched_at"}
        actual = set(rows[0].keys())

        if not required.issubset(actual):
            missing = required - actual
            raise SchemaDrift(f"Missing fields: {missing}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Insert or ignore (ON CONFLICT DO NOTHING)."""
        if not rows:
            return 0

        import duckdb

        conn = duckdb.connect(str(self.db_path))

        # DuckDB doesn't have ON CONFLICT, use INSERT OR IGNORE via exception handling
        inserted = 0
        for row in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO macro_series (series_id, obs_date, value, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        row["series_id"],
                        row["obs_date"],
                        row["value"],
                        row["source"],
                        row["fetched_at"],
                    ],
                )
                inserted += 1
            except Exception:
                # Duplicate, skip
                pass

        conn.close()
        return inserted


def main():
    """CLI entry point."""
    collector = FredCollector()
    collector.run()


if __name__ == "__main__":
    main()

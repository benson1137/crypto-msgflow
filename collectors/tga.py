"""Treasury TGA (daily) collector."""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow


class TgaCollector(BaseCollector):
    """
    US Treasury General Account (TGA) - daily frequency.

    Complements FRED's WTREGEN (weekly avg) with daily data from fiscaldata.treasury.gov.
    """

    name = "tga"
    schedule = "0 22 * * 1-5"  # 10pm UTC on weekdays
    max_staleness = timedelta(days=4)  # covers weekend + holiday
    use_env_proxy = False  # US Treasury: direct, bypass mihomo (§3.3)

    def __init__(self):
        super().__init__()
        self.base_url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
        self.limiter = get_limiter("treasury_fiscal", rate_per_min=30)

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch last 30 days of TGA data."""
        self.limiter.wait()

        params = {
            "filter": f"record_date:gte:{(utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')}",
            "sort": "-record_date",
            "page[size]": 100,
        }

        with self.make_client() as client:
            resp = client.get(self.base_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if "data" not in data:
            raise SchemaDrift("Missing 'data' key in Treasury Fiscal response")

        rows = []
        for record in data["data"]:
            # Field: operating_cash_balance (in millions)
            value = float(record["open_today_bal"]) if record.get("open_today_bal") else None

            rows.append({
                "series_id": "TGA",
                "obs_date": record["record_date"],
                "value": value,
                "source": "treasury_fiscal",
                "fetched_at": utcnow(),
            })

        return rows

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
        """Insert or ignore duplicates."""
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
                pass

        conn.close()
        return inserted


def main():
    """CLI entry point."""
    collector = TgaCollector()
    collector.run()


if __name__ == "__main__":
    main()

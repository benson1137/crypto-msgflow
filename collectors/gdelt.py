"""GDELT GKG tone/theme collector via BigQuery (spec §1 GDELT).

NOT a crypto-news source — GDELT's direct crypto coverage is thin and noisy.
This pulls macro/geopolitical *reporting volume + tone* as a risk-on/off
background feature: rates, central banks, sanctions, conflict.

Signal (soft, leading, use as FEATURE not trigger): doc_count surge +
avg_tone drop = risk-off pressure.

COST: BigQuery bills by bytes scanned. gkg_partitioned is ingestion-time
partitioned on the _PARTITIONTIME pseudo-column (verified: GDELT's own
announcement). The WHERE _PARTITIONTIME >= ... clause is what keeps a query
inside the 1TB/month free tier. Do NOT remove it.

STATUS: requires a BigQuery billing project + credentials. With none
configured, this collector raises loudly on run — it does not silently skip.
Set gdelt.bq_project (+ GOOGLE_APPLICATION_CREDENTIALS or
gdelt.bq_credentials_json) then install the SDK:
    pip install 'crypto-msgflow[gdelt]'   # google-cloud-bigquery
"""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.timeutil import utcnow

# Theme LIKE-patterns → bucket. Edit to taste; keep buckets few and stable.
THEME_BUCKETS = {
    "rates": "V2Themes LIKE '%ECON_INTEREST_RATE%'",
    "centralbank": "V2Themes LIKE '%ECON_CENTRALBANK%'",
    "sanctions": "V2Themes LIKE '%SANCTIONS%'",
    "conflict": "V2Themes LIKE '%CRISISLEX_C07_SAFETY%'",
}


class GdeltCollector(BaseCollector):
    """GKG partitioned → hourly (theme_bucket, doc_count, avg_tone)."""

    name = "gdelt"
    schedule = "5 * * * *"  # hourly, a few min past the hour
    max_staleness = timedelta(hours=3)
    use_env_proxy = True  # BQ API egress via mihomo on this host
    staleness_by_data_ts = True  # GKG updates every 15m; old max ts = broken

    def __init__(self):
        super().__init__()
        self.project = self.config.gdelt.bq_project
        self.creds_path = self.config.gdelt.bq_credentials_json
        self.lookback_min = self.config.gdelt.lookback_minutes

    def _client(self):
        """Build a BigQuery client. Raises SchemaDrift with actionable text
        if project/SDK/creds are missing — this is the '待凭证' guard."""
        if not self.project:
            raise SchemaDrift(
                "gdelt.bq_project not set — GDELT needs a BigQuery billing "
                "project + credentials. Configure it before enabling this cron."
            )
        try:
            from google.cloud import bigquery
        except ImportError as e:
            raise SchemaDrift(
                "google-cloud-bigquery not installed. "
                "pip install 'crypto-msgflow[gdelt]'"
            ) from e

        if self.creds_path:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                self.creds_path
            )
            return bigquery.Client(project=self.project, credentials=creds)
        # else rely on GOOGLE_APPLICATION_CREDENTIALS / ADC
        return bigquery.Client(project=self.project)

    def _build_query(self) -> str:
        case_arms = "\n".join(
            f"      WHEN {cond} THEN '{bucket}'"
            for bucket, cond in THEME_BUCKETS.items()
        )
        where_or = "\n     OR ".join(THEME_BUCKETS.values())
        return f"""
        WITH tagged AS (
          SELECT
            TIMESTAMP_TRUNC(
              PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)), HOUR
            ) AS ts_hour,
            CAST(SPLIT(V2Tone, ',')[OFFSET(0)] AS FLOAT64) AS tone,
            CASE
{case_arms}
            END AS theme_bucket
          FROM `gdelt-bq.gdeltv2.gkg_partitioned`
          WHERE _PARTITIONTIME >= TIMESTAMP_SUB(
                  CURRENT_TIMESTAMP(), INTERVAL {self.lookback_min} MINUTE)
            AND ({where_or})
        )
        SELECT ts_hour, theme_bucket,
               COUNT(*) AS doc_count,
               AVG(tone) AS avg_tone
        FROM tagged
        WHERE theme_bucket IS NOT NULL
        GROUP BY ts_hour, theme_bucket
        """

    def fetch(self) -> list[dict[str, Any]]:
        client = self._client()
        now = utcnow()
        rows: list[dict[str, Any]] = []
        for r in client.query(self._build_query()).result():
            ts_hour = r["ts_hour"]
            # BQ returns tz-aware UTC; store naive-UTC per timeutil contract.
            if getattr(ts_hour, "tzinfo", None) is not None:
                ts_hour = ts_hour.replace(tzinfo=None)
            # Key is 'ts' (not 'ts_hour') so base.run() picks it up for the
            # data-ts staleness check; it maps to the ts_hour column on upsert.
            rows.append({
                "ts": ts_hour,
                "theme_bucket": r["theme_bucket"],
                "doc_count": int(r["doc_count"]),
                "avg_tone": float(r["avg_tone"]) if r["avg_tone"] is not None else None,
                "fetched_at": now,
            })
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"ts", "theme_bucket", "doc_count", "avg_tone", "fetched_at"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Upsert hourly buckets. The current hour's counts keep growing as
        GKG publishes, so re-runs must UPDATE the row, not skip it."""
        if not rows:
            return 0

        import duckdb

        conn = duckdb.connect(str(self.db_path))
        n = 0
        for r in rows:
            conn.execute(
                """
                DELETE FROM gdelt_tone WHERE ts_hour = ? AND theme_bucket = ?
                """,
                [r["ts"], r["theme_bucket"]],
            )
            conn.execute(
                """
                INSERT INTO gdelt_tone
                (ts_hour, theme_bucket, doc_count, avg_tone, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [r["ts"], r["theme_bucket"], r["doc_count"],
                 r["avg_tone"], r["fetched_at"]],
            )
            n += 1
        conn.close()
        return n


def main():
    GdeltCollector().run()


if __name__ == "__main__":
    main()

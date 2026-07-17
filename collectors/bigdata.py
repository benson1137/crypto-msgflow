"""BigData.com corporate events calendar collector.

Crypto-adjacent equity earnings/conference calls (COIN, MSTR, MARA, ...).
Their earnings move BTC sentiment. Forward-looking schedule → corp_events.

Distinct from events/sightings (news dedup): the key here is event_datetime
(a future date), not first_seen_ts. See §2.x.
"""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

BASE_URL = "https://api.bigdata.com/v1/events-calendar/query"


class BigdataCollector(BaseCollector):
    """
    Corporate events calendar via BigData.com.

    Tracks earnings-call / conference-call for crypto-adjacent equities.
    Forward-looking: writes future event_datetime into corp_events.
    """

    name = "bigdata"
    schedule = "0 1 * * *"  # daily; schedule shifts slowly, no need for high freq
    max_staleness = timedelta(days=30)
    use_env_proxy = True  # api.bigdata.com reachable; keep env proxy on
    # Forward-looking calendar: newest event_datetime is in the FUTURE, and
    # "no schedule change today" is normal. Data-ts staleness is meaningless
    # here — rely on heartbeat + consecutive-empty instead. See x_kol/bls.
    staleness_by_data_ts = False

    def __init__(self):
        super().__init__()
        self.api_key = self.config.bigdata.api_key
        self.entities = self.config.bigdata.entities
        self.limiter = get_limiter("bigdata", rate_per_min=30)

    def fetch(self) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SchemaDrift("bigdata.api_key not configured")
        if not self.entities:
            raise SchemaDrift("bigdata.entities empty — resolve entity_ids first")

        entity_ids = [e["id"] for e in self.entities]
        id_to_ticker = {e["id"]: e.get("ticker", "") for e in self.entities}
        id_to_company = {e["id"]: e.get("name", "") for e in self.entities}

        today = utcnow().date()
        payload = {
            "rp_entity_id": entity_ids,
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=120)).isoformat(),
            "categories": ["earnings-call", "conference-call"],
            "limit": 500,
        }

        self.limiter.wait()
        headers = {"X-API-KEY": self.api_key}
        with self.make_client(timeout=30, headers=headers) as client:
            resp = client.post(BASE_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results")
        if results is None:
            raise SchemaDrift("Missing 'results' in BigData response")

        rows = []
        for entity_id, entries in results.items():
            for ev in entries:
                rows.append({
                    "entity_id": entity_id,
                    "ticker": id_to_ticker.get(entity_id, ""),
                    "company": id_to_company.get(entity_id, ""),
                    "category": ev["category"],
                    "event_datetime": ev["event_datetime"],
                    "title": ev["title"],
                    "fiscal_year": ev.get("fiscal_year"),
                    "fiscal_period": ev.get("fiscal_period"),
                    "updated_at": ev.get("updated_at"),
                    "fetched_at": utcnow(),
                })
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"entity_id", "category", "event_datetime", "title"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Upsert into corp_events. Re-fetched schedules UPDATE (datetime can shift)."""
        if not rows:
            return 0

        import duckdb
        conn = duckdb.connect(str(self.db_path))

        changed = 0
        for r in rows:
            # Logical identity of an earnings call is (entity, fiscal_year,
            # fiscal_period); its scheduled datetime can be revised, so
            # DELETE-by-fiscal then INSERT reflects a reschedule as an update.
            # Conference calls have NO fiscal period — each is distinct, so
            # dedup by the table PK (entity, category, event_datetime) instead,
            # otherwise all null-fiscal events for one entity collapse into one.
            if r["fiscal_year"] is not None:
                conn.execute(
                    """
                    DELETE FROM corp_events
                    WHERE entity_id = ? AND category = ?
                      AND fiscal_year = ? AND COALESCE(fiscal_period, '') = COALESCE(?, '')
                    """,
                    [r["entity_id"], r["category"], r["fiscal_year"], r["fiscal_period"]],
                )
            else:
                conn.execute(
                    """
                    DELETE FROM corp_events
                    WHERE entity_id = ? AND category = ? AND event_datetime = ?
                    """,
                    [r["entity_id"], r["category"], r["event_datetime"]],
                )
            conn.execute(
                """
                INSERT INTO corp_events
                (entity_id, ticker, company, category, event_datetime, title,
                 fiscal_year, fiscal_period, updated_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    r["entity_id"], r["ticker"], r["company"], r["category"],
                    r["event_datetime"], r["title"], r["fiscal_year"],
                    r["fiscal_period"], r["updated_at"], r["fetched_at"],
                ],
            )
            changed += 1

        conn.close()
        return changed


def main():
    BigdataCollector().run()


if __name__ == "__main__":
    main()

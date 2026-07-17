"""RSS news collector — writes events + sightings (no full text)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import mktime
from typing import Any

import feedparser

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.dedup import content_hash, extract_coins
from collectors.timeutil import utcnow


class RssCollector(BaseCollector):
    """
    RSS feed collector.

    Stores only url + title + ts into events/sightings.
    Full text fetched on-demand into news_fulltext (7-day LRU) — NOT here.
    """

    name = "rss"
    schedule = "*/10 * * * *"
    max_staleness = timedelta(days=2)  # per-source override recommended

    def __init__(self):
        super().__init__()
        self.sources = self.config.rss.sources

    def fetch(self) -> list[dict[str, Any]]:
        """Parse all configured RSS feeds."""
        rows = []

        for source in self.sources:
            name = source["name"]
            url = source["url"]

            feed = feedparser.parse(url)

            # feedparser sets bozo=1 on malformed feeds, but often still parses
            if feed.bozo and not feed.entries:
                # Truly broken feed — this is a signal, not silent failure
                raise SchemaDrift(f"RSS feed '{name}' unparseable: {feed.get('bozo_exception')}")

            for entry in feed.entries:
                # Parse timestamp
                ts = utcnow()
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    ts = datetime.fromtimestamp(mktime(entry.published_parsed))
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    ts = datetime.fromtimestamp(mktime(entry.updated_parsed))

                title = entry.get("title", "")
                if not title:
                    continue

                rows.append({
                    "source": f"rss:{name}",
                    "title": title,
                    "url": entry.get("link", ""),
                    "ts": ts,
                })

        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"source", "title", "url", "ts"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Write to events + sightings."""
        if not rows:
            return 0

        import duckdb
        conn = duckdb.connect(str(self.db_path))

        inserted = 0
        for row in rows:
            event_id = content_hash(row["title"])
            coins = extract_coins(row["title"])

            # Upsert event (only if new — preserves first_seen_ts)
            existing = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", [event_id]
            ).fetchone()

            if not existing:
                conn.execute(
                    """
                    INSERT INTO events (event_id, canonical_title, first_seen_ts, category, coins)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [event_id, row["title"], row["ts"], "news", coins],
                )

            # Insert sighting
            try:
                conn.execute(
                    """
                    INSERT INTO sightings (event_id, source, seen_ts, url, raw_title)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [event_id, row["source"], row["ts"], row["url"], row["title"]],
                )
                inserted += 1
            except Exception:
                pass  # duplicate sighting

        conn.close()
        return inserted


def main():
    collector = RssCollector()
    collector.run()


if __name__ == "__main__":
    main()

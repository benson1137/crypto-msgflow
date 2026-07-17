"""X (Twitter) KOL collector via twitterapi.io — P0."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.dedup import content_hash, extract_coins
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

# twitterapi.io endpoint (verify against current docs — Appendix A #4)
TWITTERAPI_BASE = "https://api.twitterapi.io"


class XKolCollector(BaseCollector):
    """
    X/Twitter KOL tweet collector via third-party API.

    P0: tweets get deleted, accounts get banned — unrecoverable.

    Do NOT use browser + login session (ban risk unacceptable, §4.4).
    Stores only: id, author, ts, text, reply/rt/like counts. Not full JSON.
    """

    name = "x_kol"
    schedule = "*/10 * * * *"
    max_staleness = timedelta(minutes=30)
    # Content-driven: a KOL going quiet for days is normal, not a failure.
    # Liveness is guarded by heartbeat (§6.2) + consecutive-empty (§6.3),
    # NOT by newest-tweet age — otherwise this alerts almost every run.
    staleness_by_data_ts = False

    def __init__(self):
        super().__init__()
        self.api_key = self.config.twitter.api_key
        self.kols = self.config.twitter.kols
        self.limiter = get_limiter("twitterapi", rate_per_min=600)  # 10/s

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch recent tweets for each KOL."""
        if not self.api_key:
            raise SchemaDrift("twitter.api_key not configured")

        rows = []
        headers = {"X-API-Key": self.api_key}

        with httpx.Client(timeout=15, headers=headers) as client:
            for handle in self.kols:
                self.limiter.wait()

                # Endpoint shape per twitterapi.io — VERIFY (Appendix A #4)
                resp = client.get(
                    f"{TWITTERAPI_BASE}/twitter/user/last_tweets",
                    params={"userName": handle},
                )

                if resp.status_code == 429:
                    from collectors.rate_limit import RateLimited
                    raise RateLimited(int(resp.headers.get("Retry-After", 60)))
                resp.raise_for_status()

                data = resp.json()

                # twitterapi.io shape (verified 2026-07-17): data.data.tweets is the list
                inner = data.get("data")
                if isinstance(inner, dict):
                    tweets = inner.get("tweets", [])
                elif isinstance(inner, list):
                    tweets = inner
                else:
                    tweets = data.get("tweets", [])
                if not isinstance(tweets, list):
                    raise SchemaDrift(f"Unexpected tweets shape for {handle}: {type(tweets)}")

                for tw in tweets:
                    ts = self._parse_ts(tw.get("createdAt") or tw.get("created_at"))
                    rows.append({
                        "id": str(tw.get("id")),
                        "author": handle,
                        "ts": ts,
                        "text": tw.get("text", ""),
                        "reply_count": tw.get("replyCount", 0),
                        "rt_count": tw.get("retweetCount", 0),
                        "like_count": tw.get("likeCount", 0),
                    })

        return rows

    @staticmethod
    def _parse_ts(raw) -> datetime:
        """Parse various timestamp formats."""
        if not raw:
            return utcnow()
        # Twitter format: "Wed Oct 10 20:19:24 +0000 2018"
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
        return utcnow()

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"id", "author", "ts", "text", "reply_count", "rt_count", "like_count"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Write to events + sightings (source=x:@handle)."""
        if not rows:
            return 0

        import duckdb
        conn = duckdb.connect(str(self.db_path))

        inserted = 0
        for row in rows:
            if not row["text"]:
                continue

            event_id = content_hash(row["text"])
            coins = extract_coins(row["text"])
            source = f"x:@{row['author']}"

            existing = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", [event_id]
            ).fetchone()

            if not existing:
                conn.execute(
                    """
                    INSERT INTO events (event_id, canonical_title, first_seen_ts, category, coins)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [event_id, row["text"][:200], row["ts"], "social", coins],
                )

            try:
                conn.execute(
                    """
                    INSERT INTO sightings (event_id, source, seen_ts, url, raw_title)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [event_id, source, row["ts"],
                     f"https://x.com/{row['author']}/status/{row['id']}", row["text"][:500]],
                )
                inserted += 1
            except Exception:
                pass

        conn.close()
        return inserted


def main():
    collector = XKolCollector()
    collector.run()


if __name__ == "__main__":
    main()

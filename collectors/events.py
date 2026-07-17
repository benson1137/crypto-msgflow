"""Event management utilities."""
import sys
from datetime import datetime
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.config import get_config
from collectors.dedup import content_hash, extract_coins
from collectors.timeutil import utcnow


def record_event(
    title: str,
    source: str,
    url: str | None = None,
    category: str | None = None,
    seen_ts: datetime | None = None,
) -> str:
    """
    Record an event sighting.

    If event_id (content_hash) doesn't exist, creates new event.
    Always creates a sighting.

    Returns event_id.
    """
    if seen_ts is None:
        seen_ts = utcnow()

    event_id = content_hash(title)
    coins = extract_coins(title)

    config = get_config()
    db_path = Path(__file__).parent.parent / config.database.path
    conn = duckdb.connect(str(db_path))

    # Check if event exists
    existing = conn.execute(
        "SELECT event_id FROM events WHERE event_id = ?", [event_id]
    ).fetchone()

    if not existing:
        # Create new event
        conn.execute(
            """
            INSERT INTO events (event_id, canonical_title, first_seen_ts, category, coins)
            VALUES (?, ?, ?, ?, ?)
            """,
            [event_id, title, seen_ts, category, coins],
        )

    # Always create sighting (allows duplicate sources at different times)
    try:
        conn.execute(
            """
            INSERT INTO sightings (event_id, source, seen_ts, url, raw_title)
            VALUES (?, ?, ?, ?, ?)
            """,
            [event_id, source, seen_ts, url, title],
        )
    except Exception:
        # Duplicate sighting (same event_id + source + seen_ts), skip
        pass

    conn.close()
    return event_id


def get_event_breadth(event_id: str) -> int:
    """Get number of distinct sources for an event."""
    config = get_config()
    db_path = Path(__file__).parent.parent / config.database.path
    conn = duckdb.connect(str(db_path))

    result = conn.execute(
        """
        SELECT COUNT(DISTINCT source)
        FROM sightings
        WHERE event_id = ?
        """,
        [event_id],
    ).fetchone()

    conn.close()
    return result[0] if result else 0


def get_recent_events(hours: int = 24, min_breadth: int = 1) -> list[dict]:
    """
    Get recent events with their breadth.

    Args:
        hours: lookback window
        min_breadth: minimum number of sources

    Returns:
        List of dicts with event_id, canonical_title, first_seen_ts, breadth
    """
    config = get_config()
    db_path = Path(__file__).parent.parent / config.database.path
    conn = duckdb.connect(str(db_path))

    df = conn.execute(
        f"""
        SELECT e.event_id, e.canonical_title, e.first_seen_ts, e.category, e.coins,
               COUNT(DISTINCT s.source) AS breadth,
               MIN(s.seen_ts) AS first_source_ts
        FROM events e
        JOIN sightings s USING (event_id)
        WHERE e.first_seen_ts > ? - INTERVAL 1 HOUR * ?
        GROUP BY 1, 2, 3, 4, 5
        HAVING breadth >= {min_breadth}
        ORDER BY breadth DESC, first_seen_ts DESC
        """,
        [utcnow(), hours],
    ).fetchdf()

    conn.close()
    return df.to_dict('records')

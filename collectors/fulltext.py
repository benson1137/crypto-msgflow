"""On-demand news full-text fetch with a 7-day LRU cache (spec §2.4).

Full text is NOT stored by collectors (rss.py keeps only url+title). The
analysis layer calls get_fulltext() when it actually needs an article body;
this fetches on demand, caches for 7 days, and evicts older rows.

Uses curl_cffi (TLS fingerprint impersonation, spec §3.4) since news sites
often block plain clients from a datacenter IP.
"""
import sys
from datetime import timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.config import get_config  # noqa: E402
from collectors.timeutil import utcnow  # noqa: E402

CACHE_TTL = timedelta(days=7)


def _db_path() -> Path:
    return Path(__file__).parent.parent / get_config().database.path


def _fetch_url(url: str, timeout: int = 20) -> str | None:
    """Fetch a URL's text via curl_cffi (Chrome impersonation)."""
    try:
        from curl_cffi import requests as cffi
        r = cffi.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def get_fulltext(event_id: str, url: str, force: bool = False) -> str | None:
    """Return article body for (event_id, url).

    Cache-first: a fresh (<7d) cached body is returned without a network hit.
    On miss/stale/force, fetch → cache → return. Returns None if the fetch
    fails (caller decides how to handle a missing body).
    """
    conn = duckdb.connect(str(_db_path()))
    now = utcnow()

    if not force:
        row = conn.execute(
            "SELECT body, fetched_at FROM news_fulltext WHERE event_id=? AND url=?",
            [event_id, url],
        ).fetchone()
        if row and row[0] is not None and (now - row[1]) < CACHE_TTL:
            conn.close()
            return row[0]

    body = _fetch_url(url)
    if body is None:
        conn.close()
        return None

    # Upsert (DELETE+INSERT — body/fetched_at refresh on re-fetch)
    conn.execute("DELETE FROM news_fulltext WHERE event_id=? AND url=?", [event_id, url])
    conn.execute(
        "INSERT INTO news_fulltext (event_id, url, body, fetched_at) VALUES (?,?,?,?)",
        [event_id, url, body, now],
    )
    conn.close()
    return body


def evict_stale() -> int:
    """Delete cache rows older than 7 days. Returns rows removed.

    Run from cron (daily) to keep the LRU bounded (spec §7: cap ~50MB).
    """
    conn = duckdb.connect(str(_db_path()))
    before = conn.execute("SELECT COUNT(*) FROM news_fulltext").fetchone()[0]
    conn.execute(
        "DELETE FROM news_fulltext WHERE fetched_at < ? - INTERVAL 7 DAY",
        [utcnow()],
    )
    after = conn.execute("SELECT COUNT(*) FROM news_fulltext").fetchone()[0]
    conn.close()
    return before - after


def main():
    """CLI: evict stale rows (for cron)."""
    n = evict_stale()
    print(f"news_fulltext: evicted {n} stale rows")


if __name__ == "__main__":
    main()

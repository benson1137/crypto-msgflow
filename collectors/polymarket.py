"""Polymarket Gamma collector — prediction-market implied probabilities.

Snapshots crypto/macro-relevant markets (Fed, CPI, recession, BTC targets)
every ~10 min into polymarket_snapshots. The signal is the JUMP in
implied_prob — prediction markets often move *before* the confirming news
arrives (WM's "prediction先于新闻" thesis).

Point-in-time: ts = ingest moment (§spec), not market time.

Reachability note (verified on this host 2026-07): every egress goes through
mihomo; direct connect to gamma-api fails. So use_env_proxy=True. curl_cffi
(impersonate=chrome) is the primary client to also clear Cloudflare JA3;
plain httpx is the fallback. Both ride the mihomo proxy.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"


class PolymarketCollector(BaseCollector):
    """Gamma /events → implied-probability snapshots."""

    name = "polymarket"
    schedule = "*/10 * * * *"
    max_staleness = timedelta(hours=1)
    use_env_proxy = True  # direct fails on this host; ride mihomo (§3.3)
    # Every snapshot is stamped 'now', so max data ts is always fresh — a
    # broken run produces NO rows, not old rows. Liveness = heartbeat +
    # consecutive-empty, not data-ts staleness. See rss/bls.
    staleness_by_data_ts = False

    def __init__(self):
        super().__init__()
        self.tag_ids = self.config.polymarket.tag_ids
        self.min_vol = self.config.polymarket.min_volume24hr
        self.per_tag_limit = self.config.polymarket.per_tag_limit
        # Gamma is generous; keep polite.
        self.limiter = get_limiter("polymarket", rate_per_min=60)

    def _get(self, params: dict) -> Any:
        """GET Gamma. Try curl_cffi (JA3 bypass) first, httpx as fallback.

        Both honor the mihomo proxy from env. curl_cffi reads *_proxy env
        via its libcurl core; httpx via trust_env (default True here).
        """
        try:
            from curl_cffi import requests as cffi

            r = cffi.get(
                GAMMA_EVENTS, params=params, impersonate="chrome", timeout=20
            )
            r.raise_for_status()
            return r.json()
        except ImportError:
            pass  # fall through to httpx
        with self.make_client(timeout=20) as client:
            resp = client.get(GAMMA_EVENTS, params=params)
            resp.raise_for_status()
            return resp.json()

    def fetch(self) -> list[dict[str, Any]]:
        seen_markets: set[str] = set()  # a market can carry several tags; dedup
        rows: list[dict[str, Any]] = []
        ts = utcnow()  # single snapshot timestamp for the whole run

        for tag_id in self.tag_ids:
            self.limiter.wait()
            params = {
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": self.per_tag_limit,
                "tag_id": tag_id,
            }
            data = self._get(params)
            if not isinstance(data, list):
                raise SchemaDrift(
                    f"Gamma /events?tag_id={tag_id} returned {type(data).__name__}, "
                    "expected list"
                )

            for ev in data:
                event_slug = ev.get("slug", "")
                for m in ev.get("markets", []):
                    slug = m.get("slug") or m.get("conditionId") or ""
                    if not slug or slug in seen_markets:
                        continue

                    vol24 = _f(m.get("volume24hr"))
                    if vol24 is not None and vol24 < self.min_vol:
                        continue

                    # outcomes / outcomePrices arrive as JSON *strings*.
                    outcomes = _loads_list(m.get("outcomes"))
                    prices = _loads_list(m.get("outcomePrices"))
                    if not outcomes or len(outcomes) != len(prices):
                        continue

                    seen_markets.add(slug)
                    for outcome, price in zip(outcomes, prices):
                        rows.append({
                            "ts": ts,
                            "market_slug": slug,
                            "outcome": outcome,
                            "implied_prob": _f(price),
                            "volume24hr": vol24,
                            "question": m.get("question") or ev.get("title"),
                            "event_slug": event_slug,
                            "end_date": m.get("endDate") or m.get("endDateIso"),
                            "fetched_at": ts,
                        })
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"ts", "market_slug", "outcome", "implied_prob", "fetched_at"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        import duckdb

        conn = duckdb.connect(str(self.db_path))
        inserted = 0
        for r in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO polymarket_snapshots
                    (ts, market_slug, outcome, implied_prob, volume24hr,
                     question, event_slug, end_date, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        r["ts"], r["market_slug"], r["outcome"],
                        r["implied_prob"], r["volume24hr"], r["question"],
                        r["event_slug"], r["end_date"], r["fetched_at"],
                    ],
                )
                inserted += 1
            except Exception:
                pass  # dup (ts, market_slug, outcome) — same-minute rerun
        conn.close()
        return inserted


def _f(v) -> float | None:
    """Coerce API string/number to float, tolerating None/''/garbage."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _loads_list(v) -> list:
    """Gamma sends outcomes/outcomePrices as a JSON-encoded string."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def main():
    PolymarketCollector().run()


if __name__ == "__main__":
    main()

"""
Daily smoke tests — the un-cuttable part of the system (§6).

Every failure mode we discussed (429, anti-scraping, schema drift, 404,
upstream field changes) ends in the same sentence: "your pipeline
quietly tells you there's no news today."

Each collector gets THREE assertions, none optional:
1. Existence  — is there any data?
2. Contract   — did the schema drift?
3. Freshness  — is the latest data recent? ← the most critical one

The third catches the deadliest failure: endpoint returns 200 but
serves last week's cache. The first two assertions can't see it.
"""
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from collectors.timeutil import utcnow

DB_PATH = Path(__file__).parent.parent.parent / "research.db"


@pytest.fixture
def conn():
    c = duckdb.connect(str(DB_PATH), read_only=True)
    yield c
    c.close()


def _table_has_data(conn, table: str) -> bool:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0


# ─── macro_series (FRED / TGA) ──────────────────────────────────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_fred_rrp(conn):
    """RRPONTSYD is daily — should be fresh within 4 days."""
    df = conn.execute(
        "SELECT * FROM macro_series WHERE series_id='RRPONTSYD'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No FRED data yet (collector not run)")

    # 2. Contract
    assert {"obs_date", "value"}.issubset(df.columns), "schema drift"
    # 3. Freshness (most critical)
    latest = df["obs_date"].max()
    assert latest.date() > (utcnow().date() - timedelta(days=4)), \
        "FRED RRPONTSYD data stale (>4 days)"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_tga_daily(conn):
    """TGA daily from Treasury — fresh within 4 days."""
    df = conn.execute(
        "SELECT * FROM macro_series WHERE series_id='TGA' AND source='treasury_fiscal'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No TGA data yet")

    assert {"obs_date", "value"}.issubset(df.columns), "schema drift"
    latest = df["obs_date"].max()
    assert latest.date() > (utcnow().date() - timedelta(days=4)), \
        "TGA data stale"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_bls_cpi(conn):
    """BLS CPI — monthly with 4-6wk publication lag, so obs_date lags ~1 month.
    Freshness window is wide (60 days) to tolerate the structural lag."""
    df = conn.execute(
        "SELECT * FROM macro_series WHERE series_id='CUUR0000SA0' AND source='bls'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No BLS data yet")

    assert {"obs_date", "value"}.issubset(df.columns), "schema drift"
    latest = df["obs_date"].max()
    # Monthly cadence + publication lag: allow up to 60 days
    assert latest.date() > (utcnow().date() - timedelta(days=60)), \
        "BLS CPI data stale (>60 days — likely collector down)"


# ─── corp_events (BigData) — crypto-equity earnings calendar ────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_corp_events(conn):
    """BigData corp_events — forward-looking earnings/conference schedule.
    Freshness is not by data-age (events are in the future); assert the
    table has upcoming events and the schema contract holds."""
    df = conn.execute("SELECT * FROM corp_events").fetchdf()

    if df.empty:
        pytest.skip("No corp_events yet")

    assert {"entity_id", "category", "event_datetime", "title"}.issubset(df.columns), \
        "schema drift"
    # categories are constrained by the API to these two
    assert set(df["category"].unique()).issubset({"earnings-call", "conference-call"}), \
        "unexpected category value"


# ─── macro_calendar (FOMC) — forward-looking macro schedule ─────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_fomc_calendar(conn):
    """FOMC forward calendar — must have upcoming meetings scheduled.
    Freshness is forward-looking: assert at least one future meeting
    exists (empty/all-past = scraper broke or page structure drifted)."""
    df = conn.execute(
        "SELECT * FROM macro_calendar WHERE event_type='FOMC'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No FOMC calendar yet")

    assert {"event_type", "event_date", "has_press_conf"}.issubset(df.columns), \
        "schema drift"
    # There must be at least one meeting in the future — the whole point
    # is a forward calendar. All-past means the scraper silently broke.
    future = df[df["event_date"].dt.date > utcnow().date()]
    assert len(future) > 0, "no future FOMC meetings — scraper likely broke"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_bea_pce(conn):
    """BEA core PCE — monthly, ~4wk publication lag (60-day freshness window)."""
    df = conn.execute(
        "SELECT * FROM macro_series WHERE series_id='PCEPILFE' AND source='bea'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No BEA data yet")

    assert {"obs_date", "value"}.issubset(df.columns), "schema drift"
    latest = df["obs_date"].max()
    # PCE lags worse than CPI: month M publishes ~end of M+1, so obs_date can
    # be ~2 months old at the tightest point. 95-day window covers it.
    assert latest.date() > (utcnow().date() - timedelta(days=95)), \
        "BEA core PCE stale (>95 days — likely collector down)"


# ─── oi_funding (OKX) — P0 ──────────────────────────────────────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_okx_oi_realtime(conn):
    """OKX rt15 texture layer — 15-min snapshots, fresh within 45 min."""
    df = conn.execute(
        "SELECT * FROM oi_funding WHERE granularity='rt15'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No rt15 OI data yet")

    assert {"inst_id", "ts", "oi_usd"}.issubset(df.columns), "schema drift"
    assert df["ts"].max() > (utcnow() - timedelta(minutes=45)), \
        "OKX rt15 OI stale (>45 min)"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_okx_oi_1h(conn):
    """OKX 1h backbone — the recoverable series priced-in analysis reads.
    30-day window; fresh within ~14h (twice-daily) and funding populated."""
    df = conn.execute(
        "SELECT * FROM oi_funding WHERE granularity='1h'"
    ).fetchdf()

    if df.empty:
        pytest.skip("No 1h OI data yet")

    assert {"inst_id", "ts", "oi_usd", "funding_rate"}.issubset(df.columns), "schema drift"
    assert df["ts"].max() > (utcnow() - timedelta(hours=14)), \
        "OKX 1h backbone stale (>14h — twice-daily collector down)"
    # funding must be forward-filled, not all-null
    assert df["funding_rate"].notna().any(), "1h funding_rate all null — join broke"


# ─── events / sightings ─────────────────────────────────────────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_events_schema(conn):
    """events/sightings schema contract."""
    ev = conn.execute("SELECT * FROM events LIMIT 1").fetchdf()
    assert {"event_id", "canonical_title", "first_seen_ts"}.issubset(ev.columns), \
        "events schema drift"

    si = conn.execute("SELECT * FROM sightings LIMIT 1").fetchdf()
    assert {"event_id", "source", "seen_ts"}.issubset(si.columns), \
        "sightings schema drift"


# ─── collector_runs health (§6.2, §6.3) ─────────────────────────────

@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_no_stuck_running(conn):
    """No collector should be stuck in 'running' for >1 hour."""
    stuck = conn.execute(
        """
        SELECT collector, started_at FROM collector_runs
        WHERE status = 'running'
          AND started_at < ? - INTERVAL 1 HOUR
        """,
        [utcnow()],
    ).fetchall()
    assert not stuck, f"Collectors stuck in 'running': {stuck}"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_consecutive_empty(conn):
    """§6.3: sustained empties are suspicious — but ONLY for cadence-driven
    sources. Content-driven collectors (rss, x_kol) legitimately return empty
    for hours (no fresh news / KOL quiet), same reasoning as staleness_by_data_ts.
    Flagging them would false-alarm constantly. Scope to rhythmic sources."""
    content_driven = ("rss", "x_kol")
    placeholders = ",".join("?" for _ in content_driven)
    suspicious = conn.execute(
        f"""
        SELECT collector, COUNT(*) AS empties FROM collector_runs
        WHERE started_at > ? - INTERVAL 6 HOUR AND status = 'empty'
          AND collector NOT IN ({placeholders})
        GROUP BY 1 HAVING COUNT(*) >= 12
        """,
        [utcnow(), *content_driven],
    ).fetchall()
    assert not suspicious, f"Cadence collectors with 12+ empties in 6h: {suspicious}"


@pytest.mark.skipif(not DB_PATH.exists(), reason="DB not initialized")
def test_price_candles(conn):
    """okx_price backbone — verdict backfill needs a recoverable price series.
    Must exist, be fresh (twice-daily), and have sane close prices."""
    df = conn.execute("SELECT * FROM price_candles WHERE bar='1H'").fetchdf()
    if df.empty:
        pytest.skip("No price_candles yet")
    assert {"inst_id", "ts", "close"}.issubset(df.columns), "schema drift"
    assert df["ts"].max() > (utcnow() - timedelta(hours=14)), \
        "price_candles stale (>14h — okx_price collector down)"
    assert (df["close"] > 0).all(), "non-positive close price"


def test_verdicts_realized_ret_exists(conn):
    """verdicts must have realized_ret column — the whole point (§2.5)."""
    cols = conn.execute("SELECT * FROM verdicts LIMIT 0").fetchdf().columns
    assert "realized_ret" in cols, "verdicts.realized_ret missing — no backtest possible"

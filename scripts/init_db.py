#!/usr/bin/env python3
"""Initialize DuckDB schema."""
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent.parent / "research.db"

SCHEMA = """
-- §2.1 macro_series
CREATE TABLE IF NOT EXISTS macro_series (
  series_id   VARCHAR NOT NULL,
  obs_date    DATE    NOT NULL,
  value       DOUBLE,
  source      VARCHAR NOT NULL,
  fetched_at  TIMESTAMP NOT NULL,
  PRIMARY KEY (series_id, obs_date, source)
);

-- §2.2 oi_funding
CREATE TABLE IF NOT EXISTS oi_funding (
  inst_id      VARCHAR NOT NULL,
  ts           TIMESTAMP NOT NULL,
  granularity  VARCHAR NOT NULL,
  oi_ccy       DOUBLE,
  oi_usd       DOUBLE,
  funding_rate DOUBLE,
  mark_price   DOUBLE,
  fetched_at   TIMESTAMP NOT NULL,
  PRIMARY KEY (inst_id, ts, granularity)
);

-- §2.3 events / sightings
CREATE TABLE IF NOT EXISTS events (
  event_id        VARCHAR PRIMARY KEY,
  canonical_title VARCHAR NOT NULL,
  first_seen_ts   TIMESTAMP NOT NULL,
  category        VARCHAR,
  coins           VARCHAR[]
);

CREATE TABLE IF NOT EXISTS sightings (
  event_id   VARCHAR NOT NULL,
  source     VARCHAR NOT NULL,
  seen_ts    TIMESTAMP NOT NULL,
  url        VARCHAR,
  raw_title  VARCHAR,
  PRIMARY KEY (event_id, source, seen_ts)
);

-- §2.4 news_fulltext (7-day LRU cache)
CREATE TABLE IF NOT EXISTS news_fulltext (
  event_id   VARCHAR NOT NULL,
  url        VARCHAR NOT NULL,
  body       VARCHAR,
  fetched_at TIMESTAMP NOT NULL,
  PRIMARY KEY (event_id, url)
);

-- §2.5 verdicts (highest priority)
CREATE TABLE IF NOT EXISTS verdicts (
  verdict_id       VARCHAR PRIMARY KEY,
  ts               TIMESTAMP NOT NULL,
  event_id         VARCHAR,
  claim            VARCHAR NOT NULL,
  label            VARCHAR NOT NULL,
  confidence       VARCHAR NOT NULL,
  post_hoc         BOOLEAN DEFAULT FALSE,

  -- priced-in snapshot
  oi_pctile        DOUBLE,
  funding_pctile   DOUBLE,
  breadth          INTEGER,

  -- falsifiable predictions
  predicted_dir    VARCHAR,
  predicted_window VARCHAR,
  predicted_magn   DOUBLE,

  -- backfill (only UPDATE-allowed fields)
  realized_ret     DOUBLE,
  realized_at      TIMESTAMP
);

-- §2.6 collector_runs (health log)
CREATE TABLE IF NOT EXISTS corp_events (
  entity_id      VARCHAR NOT NULL,     -- BigData rp_entity_id (e.g. D69946=COIN)
  ticker         VARCHAR,              -- XNAS:COIN
  company        VARCHAR,
  category       VARCHAR NOT NULL,     -- earnings-call | conference-call
  event_datetime TIMESTAMP NOT NULL,   -- UTC, scheduled time (forward-looking)
  title          VARCHAR,
  fiscal_year    INTEGER,
  fiscal_period  VARCHAR,
  updated_at     TIMESTAMP,            -- source's last-updated (schedule can shift)
  fetched_at     TIMESTAMP NOT NULL,
  PRIMARY KEY (entity_id, category, event_datetime)
);

CREATE TABLE IF NOT EXISTS price_candles (
  inst_id  VARCHAR NOT NULL,        -- BTC-USDT-SWAP
  ts       TIMESTAMP NOT NULL,      -- candle open time, UTC
  bar      VARCHAR NOT NULL,        -- '1H'
  open     DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  vol      DOUBLE,                  -- contract volume
  fetched_at TIMESTAMP NOT NULL,
  PRIMARY KEY (inst_id, ts, bar)
);

CREATE TABLE IF NOT EXISTS macro_calendar (
  event_type     VARCHAR NOT NULL,     -- 'FOMC'
  event_date     DATE NOT NULL,        -- decision day (meeting end) = market mover
  detail         VARCHAR,              -- e.g. 'Jan 27-28'
  has_press_conf BOOLEAN DEFAULT FALSE,
  source         VARCHAR NOT NULL,     -- 'federalreserve'
  fetched_at     TIMESTAMP NOT NULL,
  PRIMARY KEY (event_type, event_date, source)
);

CREATE TABLE IF NOT EXISTS collector_runs (
  collector    VARCHAR NOT NULL,
  started_at   TIMESTAMP NOT NULL,
  finished_at  TIMESTAMP,
  status       VARCHAR NOT NULL,
  rows_written INTEGER,
  max_data_ts  TIMESTAMP,
  error_type   VARCHAR,
  error_msg    VARCHAR,
  PRIMARY KEY (collector, started_at)
);
"""


def main():
    print(f"Initializing database at {DB_PATH}")

    conn = duckdb.connect(str(DB_PATH))
    conn.execute(SCHEMA)

    # Verify tables
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()

    print(f"Created {len(tables)} tables:")
    for (name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name}: {count} rows")

    conn.close()
    print("✓ Database initialized")


if __name__ == "__main__":
    main()

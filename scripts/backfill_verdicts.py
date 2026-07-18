#!/usr/bin/env python3
"""Backfill realized returns for verdicts with predictions."""
import sys
from datetime import timedelta
from pathlib import Path

import duckdb

# Add project root to path BEFORE importing collectors.*
sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.timeutil import utcnow  # noqa: E402

DB_PATH = Path(__file__).parent.parent / "research.db"

# Coin → OKX perp instrument. Extend as coverage grows.
COIN_TO_INST = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
}


def _resolve_inst(conn, row) -> str | None:
    """Find the instrument a verdict is about.

    Priority: the linked event's coins[] → else scan the claim text for a
    known coin symbol. Returns None if nothing maps to a tracked instrument.
    """
    # 1. via linked event.coins
    if row.get("event_id"):
        ev = conn.execute(
            "SELECT coins FROM events WHERE event_id = ?", [row["event_id"]]
        ).fetchone()
        if ev and ev[0]:
            for coin in ev[0]:
                if coin in COIN_TO_INST:
                    return COIN_TO_INST[coin]
    # 2. fallback: scan claim text
    claim = (row.get("claim") or "").upper()
    for coin, inst in COIN_TO_INST.items():
        if coin in claim:
            return inst
    return None


def _price_at(conn, inst_id: str, when, tol_hours: int = 3) -> float | None:
    """Closest 1H close to `when` within tol_hours. None if none in range."""
    r = conn.execute(
        """
        SELECT close FROM price_candles
        WHERE inst_id = ? AND bar = '1H'
          AND abs(date_diff('minute', ts, ?)) <= ? * 60
        ORDER BY abs(date_diff('minute', ts, ?))
        LIMIT 1
        """,
        [inst_id, when, tol_hours, when],
    ).fetchone()
    return r[0] if r else None


def backfill_returns(conn: duckdb.DuckDBPyConnection, dry_run: bool = True):
    """
    Backfill realized_ret for verdicts with falsifiable predictions.

    Logic:
    1. Find verdicts with predicted_dir/window but NULL realized_ret
    2. For each, check if prediction_window has elapsed
    3. Fetch price at ts and ts + window
    4. Calculate realized return
    5. UPDATE verdict
    """

    # Find verdicts needing backfill
    pending = conn.execute("""
        SELECT verdict_id, ts, event_id, claim,
               predicted_dir, predicted_window, predicted_magn
        FROM verdicts
        WHERE predicted_dir IS NOT NULL
          AND predicted_window IS NOT NULL
          AND realized_ret IS NULL
          AND ts < ? - INTERVAL 1 HOUR  -- at least 1h old
        ORDER BY ts
    """, [utcnow()]).fetchdf()

    if pending.empty:
        print("No verdicts to backfill")
        return

    print(f"Found {len(pending)} verdicts with predictions awaiting backfill\n")

    for _, row in pending.iterrows():
        verdict_id = row['verdict_id']
        ts = row['ts']
        window = row['predicted_window']
        predicted_dir = row['predicted_dir']

        # Parse window: '4h', '24h', '7d'
        if window.endswith('h'):
            delta = timedelta(hours=int(window[:-1]))
        elif window.endswith('d'):
            delta = timedelta(days=int(window[:-1]))
        else:
            print(f"⚠️  {verdict_id}: unknown window format '{window}'")
            continue

        realized_at = ts + delta

        # Check if window has elapsed
        if utcnow() < realized_at:
            print(f"⏳ {verdict_id}: window not elapsed yet (need to wait until {realized_at})")
            continue

        inst_id = _resolve_inst(conn, row)
        if inst_id is None:
            print(f"⚠️  {verdict_id}: can't resolve a coin/inst_id → skip "
                  f"(claim: {row['claim'][:50]})")
            continue

        p0 = _price_at(conn, inst_id, ts)
        p1 = _price_at(conn, inst_id, realized_at)
        if p0 is None or p1 is None:
            print(f"⚠️  {verdict_id}: no price_candles near ts/realized_at for {inst_id} "
                  f"(need okx_price to have run) → skip")
            continue

        realized_ret = (p1 - p0) / p0
        hit = ((predicted_dir == 'up' and realized_ret > 0) or
               (predicted_dir == 'down' and realized_ret < 0) or
               (predicted_dir == 'none' and abs(realized_ret) < 0.005))

        print(f"🔍 {verdict_id} [{inst_id}]  {predicted_dir}/{window}")
        print(f"   {p0:.2f} → {p1:.2f}  ret={realized_ret:+.4f}  {'HIT' if hit else 'MISS'}")

        if dry_run:
            print("   [DRY RUN] not written\n")
        else:
            conn.execute(
                "UPDATE verdicts SET realized_ret=?, realized_at=? WHERE verdict_id=?",
                [realized_ret, realized_at, verdict_id],
            )
            print("   ✓ written\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill realized returns")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    conn = duckdb.connect(str(DB_PATH))

    print(f"Database: {DB_PATH}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

    backfill_returns(conn, dry_run=not args.apply)

    conn.close()


if __name__ == "__main__":
    main()

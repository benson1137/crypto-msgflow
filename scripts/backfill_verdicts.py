#!/usr/bin/env python3
"""Backfill realized returns for verdicts with predictions."""
import sys
from datetime import timedelta
from pathlib import Path

import duckdb

from collectors.timeutil import utcnow

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

DB_PATH = Path(__file__).parent.parent / "research.db"


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

        # TODO: fetch price at ts and realized_at from oi_funding.mark_price
        # For now, this is a placeholder — you need to:
        # 1. Extract coin from event_id or claim
        # 2. Map to inst_id (e.g., 'BTC' -> 'BTC-USDT-SWAP')
        # 3. Query oi_funding for closest mark_price

        print(f"🔍 {verdict_id}:")
        print(f"   Claim: {row['claim'][:80]}...")
        print(f"   Prediction: {predicted_dir} over {window}")
        print(f"   Window: {ts} → {realized_at}")
        print("   ❌ Price lookup not implemented yet\n")

        # Placeholder logic:
        # realized_ret = (price_at_realized - price_at_ts) / price_at_ts
        # if dry_run:
        #     print(f"   [DRY RUN] Would set realized_ret={realized_ret:.4f}")
        # else:
        #     conn.execute("""
        #         UPDATE verdicts
        #         SET realized_ret = ?, realized_at = ?
        #         WHERE verdict_id = ?
        #     """, [realized_ret, realized_at, verdict_id])
        #     print(f"   ✓ Updated")


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

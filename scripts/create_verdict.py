"""Verdicts management - create, update, backfill."""
import hashlib
import sys
from pathlib import Path

import duckdb

# If running as script, add project root to path
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.timeutil import utcnow

DB_PATH = Path(__file__).parent.parent / "research.db"


def create_verdict(
    claim: str,
    label: str,  # KNOWN|COMPUTED|INFERRED|COMMON|FRAME|GUESS
    confidence: str,  # HIGH|MED|LOW|VERY_LOW|UNKNOWN
    event_id: str | None = None,
    post_hoc: bool = False,
    oi_pctile: float | None = None,
    funding_pctile: float | None = None,
    breadth: int | None = None,
    predicted_dir: str | None = None,  # 'up'|'down'|'none'
    predicted_window: str | None = None,  # '4h'|'24h'|'7d'
    predicted_magn: float | None = None,
) -> str:
    """
    Create a new verdict entry.

    Returns verdict_id.
    """
    ts = utcnow()

    # Generate verdict_id from claim + ts
    verdict_id = hashlib.sha256(f"{claim}{ts}".encode()).hexdigest()[:16]

    conn = duckdb.connect(str(DB_PATH))
    conn.execute(
        """
        INSERT INTO verdicts (
            verdict_id, ts, event_id, claim, label, confidence, post_hoc,
            oi_pctile, funding_pctile, breadth,
            predicted_dir, predicted_window, predicted_magn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            verdict_id, ts, event_id, claim, label, confidence, post_hoc,
            oi_pctile, funding_pctile, breadth,
            predicted_dir, predicted_window, predicted_magn,
        ],
    )
    conn.close()

    print(f"✓ Created verdict {verdict_id}")
    return verdict_id


def main():
    """CLI for manual verdict creation."""
    import argparse

    parser = argparse.ArgumentParser(description="Create a verdict entry")
    parser.add_argument("claim", help="The judgment claim")
    parser.add_argument("--label", required=True,
                       choices=["KNOWN", "COMPUTED", "INFERRED", "COMMON", "FRAME", "GUESS"])
    parser.add_argument("--confidence", required=True,
                       choices=["HIGH", "MED", "LOW", "VERY_LOW", "UNKNOWN"])
    parser.add_argument("--event-id", help="Related event_id")
    parser.add_argument("--post-hoc", action="store_true", help="Mark as post-hoc explanation")
    parser.add_argument("--oi-pctile", type=float, help="OI 90d percentile snapshot")
    parser.add_argument("--funding-pctile", type=float, help="Funding 90d percentile snapshot")
    parser.add_argument("--breadth", type=int, help="Number of sources mentioning")
    parser.add_argument("--predict-dir", choices=["up", "down", "none"], help="Predicted direction")
    parser.add_argument("--predict-window", help="Prediction window (e.g., '4h', '24h', '7d')")
    parser.add_argument("--predict-magn", type=float, help="Predicted magnitude")

    args = parser.parse_args()

    verdict_id = create_verdict(
        claim=args.claim,
        label=args.label,
        confidence=args.confidence,
        event_id=args.event_id,
        post_hoc=args.post_hoc,
        oi_pctile=args.oi_pctile,
        funding_pctile=args.funding_pctile,
        breadth=args.breadth,
        predicted_dir=args.predict_dir,
        predicted_window=args.predict_window,
        predicted_magn=args.predict_magn,
    )

    print(f"\nVerdict ID: {verdict_id}")
    print("\nTo backfill realized returns later:")
    print("  python scripts/backfill_verdicts.py --apply")


if __name__ == "__main__":
    main()

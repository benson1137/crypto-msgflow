"""Entrypoint for the OKX 1d OI backbone collector (see okx_oi.py).

Long history (~180 days) — the sample base for 90-day OI percentiles.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.okx_oi import OkxOiDailyCollector


def main():
    OkxOiDailyCollector().run()


if __name__ == "__main__":
    main()

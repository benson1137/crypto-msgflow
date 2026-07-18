"""Entrypoint for the OKX 1h OI backbone collector (see okx_oi.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.okx_oi import OkxOiHistoryCollector


def main():
    OkxOiHistoryCollector().run()


if __name__ == "__main__":
    main()

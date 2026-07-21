"""OFAC SDN crypto-address blacklist collector (spec §3b).

Pulls the 0xB10C nightly-parsed per-symbol address lists (raw txt on the
'lists' branch), instead of parsing the 80MB sdn_advanced.xml ourselves.

The append-only signal is first_seen: a NEW (address, symbol) row = a fresh
OFAC designation landing. New sanctions on an exchange / mixer / entity are
the hardest crypto signal of the three geo sources.

Daily cadence. Every egress goes through mihomo on this host → use_env_proxy.
"""
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.rate_limit import get_limiter
from collectors.timeutil import utcnow


class OfacAddressesCollector(BaseCollector):
    """SDN digital-currency addresses → ofac_crypto_addresses."""

    name = "ofac_addresses"
    schedule = "40 0 * * *"  # daily, shortly after 0xB10C's 0-UTC refresh
    max_staleness = timedelta(days=7)
    use_env_proxy = True  # raw.githubusercontent needs mihomo on this host
    # The list only grows when OFAC designates something new — steady state is
    # zero new rows for weeks. Old "data ts" is NORMAL, not a fault. Liveness =
    # heartbeat + consecutive-empty. (n>0 only on a designation day.)
    staleness_by_data_ts = False

    def __init__(self):
        super().__init__()
        self.raw_base = self.config.ofac.raw_base.rstrip("/")
        self.symbols = self.config.ofac.symbols
        self.limiter = get_limiter("ofac_addresses", rate_per_min=60)

    def fetch(self) -> list[dict[str, Any]]:
        now = utcnow()
        rows: list[dict[str, Any]] = []
        errors: list[str] = []

        for symbol in self.symbols:
            url = f"{self.raw_base}/sanctioned_addresses_{symbol}.txt"
            self.limiter.wait()
            try:
                with self.make_client(timeout=20) as client:
                    resp = client.get(url)
                    # A missing per-symbol list is not fatal (0xB10C may drop a
                    # chain); a global outage shows up as ALL symbols failing.
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    text = resp.text
            except Exception as e:  # noqa: BLE001 — collect, decide after loop
                errors.append(f"{symbol}: {e}")
                continue

            for line in text.splitlines():
                addr = line.strip()
                if not addr or addr.startswith("#"):
                    continue
                rows.append({
                    "address": addr,
                    "symbol": symbol,
                    "first_seen": now,
                    "fetched_at": now,
                })

        # If every symbol failed we fetched nothing real — raise so run() marks
        # 'error' (and alerts), rather than silently logging an empty success.
        if not rows and errors:
            raise SchemaDrift(f"All OFAC symbol lists failed: {errors[:3]}")
        return rows

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"address", "symbol", "first_seen", "fetched_at"}
        actual = set(rows[0].keys())
        if not required.issubset(actual):
            raise SchemaDrift(f"Missing fields: {required - actual}")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Insert only genuinely-new (address, symbol) pairs.

        Returns the count of NEW addresses — the actual signal. Existing rows
        keep their original first_seen (we refresh fetched_at to mark "still
        present today", which also lets a future diff detect delistings).
        """
        if not rows:
            return 0

        import duckdb

        conn = duckdb.connect(str(self.db_path))
        new_count = 0
        for r in rows:
            existing = conn.execute(
                "SELECT 1 FROM ofac_crypto_addresses WHERE address = ? AND symbol = ?",
                [r["address"], r["symbol"]],
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE ofac_crypto_addresses SET fetched_at = ?
                    WHERE address = ? AND symbol = ?
                    """,
                    [r["fetched_at"], r["address"], r["symbol"]],
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ofac_crypto_addresses
                    (address, symbol, first_seen, fetched_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [r["address"], r["symbol"], r["first_seen"], r["fetched_at"]],
                )
                new_count += 1
        conn.close()
        return new_count


def main():
    OfacAddressesCollector().run()


if __name__ == "__main__":
    main()

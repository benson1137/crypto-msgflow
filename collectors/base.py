"""Base collector class - foundation for all data sources."""
import sys
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:
    import httpx

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.alerts import send_alert, send_stale_alert
from collectors.config import get_config
from collectors.timeutil import utcnow


class SchemaDrift(Exception):  # noqa: N818 — name matches spec §3.1
    """Raised when upstream data schema changes."""
    pass


class BaseCollector(ABC):
    """
    Base class for all collectors.

    Implements §3.1 contract:
    - fetch() → list[dict]
    - validate() raises SchemaDrift
    - run() orchestrates + logs to collector_runs
    - Distinguishes 'ok' / 'empty' / 'error' / 'stale'
    """

    name: str = ""
    schedule: str = ""  # cron expression (for docs)
    max_staleness: timedelta = timedelta(hours=24)

    # Per-source proxy policy (§3.3). The environment injects a global
    # mihomo proxy (HTTP_PROXY/HTTPS_PROXY). OKX needs it; US-gov sources
    # (FRED, Treasury) break through it. Subclasses set this explicitly.
    #   True  → honor env proxy (trust_env)
    #   False → bypass env proxy, direct connection
    use_env_proxy: bool = True

    # Whether "latest data timestamp is old" means the collector is broken.
    # True  → cadence-driven sources (OKX OI publishes every 5m; stale data
    #         = collector broken). Judge staleness by max data ts.
    # False → content-driven sources (X/RSS; a KOL not tweeting for days is
    #         normal, NOT a failure). Liveness is covered by heartbeat
    #         (§6.2) + consecutive-empty (§6.3), not by data ts.
    staleness_by_data_ts: bool = True

    def __init__(self):
        self.config = get_config()
        self.db_path = Path(__file__).parent.parent / self.config.database.path

    def make_client(self, **kwargs) -> "httpx.Client":
        """Create an httpx.Client honoring this collector's proxy policy."""
        import httpx
        kwargs.setdefault("timeout", 15)
        if not self.use_env_proxy:
            # Bypass the injected mihomo proxy; connect directly.
            kwargs["trust_env"] = False
        return httpx.Client(**kwargs)

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """Fetch data from source. Returns list of dicts."""
        pass

    @abstractmethod
    def validate(self, rows: list[dict[str, Any]]) -> None:
        """
        Validate schema contract.

        Raises SchemaDrift if fields changed.
        This is INTENTIONAL: we want to be loud when upstream breaks.
        """
        pass

    @abstractmethod
    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Write rows to database. Returns number of rows written."""
        pass

    def run(self):
        """Main entry point. Logs to collector_runs."""
        conn = duckdb.connect(str(self.db_path))
        started_at = utcnow()

        # Start run
        conn.execute(
            """
            INSERT INTO collector_runs (collector, started_at, status)
            VALUES (?, ?, 'running')
            """,
            [self.name, started_at],
        )

        try:
            rows = self.fetch()
            self.validate(rows)
            n = self.upsert(rows)

            # Extract max data timestamp. Rows use either 'ts' (TIMESTAMP) or
            # 'obs_date' (DATE, possibly still a string from the API).
            max_ts = None
            if rows and 'ts' in rows[0]:
                max_ts = max(r['ts'] for r in rows)
            elif rows and 'obs_date' in rows[0]:
                max_ts = max(r['obs_date'] for r in rows)
            max_ts = self._to_datetime(max_ts)

            # Determine status
            if n == 0:
                status = 'empty'
            elif (
                self.staleness_by_data_ts
                and max_ts
                and utcnow() - max_ts > self.max_staleness
            ):
                # Only content sources with a guaranteed publish cadence
                # (OKX OI, FRED) treat old data as a fault. Content-driven
                # sources (X, RSS) rely on heartbeat + consecutive-empty
                # instead — see staleness_by_data_ts docstring.
                status = 'stale'
                send_stale_alert(self.name, max_ts, self.max_staleness)
            else:
                status = 'ok'

            # Finish run
            conn.execute(
                """
                UPDATE collector_runs
                SET finished_at = ?, status = ?, rows_written = ?, max_data_ts = ?
                WHERE collector = ? AND started_at = ?
                """,
                [utcnow(), status, n, max_ts, self.name, started_at],
            )

            conn.close()

            if status == 'empty':
                print(f"⚠️  {self.name}: no new data (this is OK if expected)")
            elif status == 'stale':
                print(f"⚠️  {self.name}: data is stale (max_ts={max_ts})")
            else:
                print(f"✓ {self.name}: wrote {n} rows")

        except Exception as e:
            # Classify error
            error_type = self._classify_error(e)

            conn.execute(
                """
                UPDATE collector_runs
                SET finished_at = ?, status = 'error',
                    error_type = ?, error_msg = ?
                WHERE collector = ? AND started_at = ?
                """,
                [utcnow(), error_type, str(e), self.name, started_at],
            )
            conn.close()

            send_alert(self.name, e)
            raise

    @staticmethod
    def _to_datetime(value) -> datetime | None:
        """Normalize a ts/obs_date value (str | date | datetime) to datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        if isinstance(value, str):
            # Try date first (macro obs_date), then full timestamp
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
        return None

    def _classify_error(self, e: Exception) -> str:
        """Classify exception into error_type."""
        if isinstance(e, SchemaDrift):
            return 'schema_drift'
        elif 'timeout' in str(e).lower():
            return 'timeout'
        elif '429' in str(e):
            return 'http_429'
        elif '403' in str(e):
            return 'http_403'
        elif '404' in str(e):
            return 'http_404'
        else:
            return 'unknown'

"""FOMC forward meeting-schedule collector.

Parses federalreserve.gov/monetarypolicy/fomccalendars.htm into macro_calendar.
This is the FORWARD-LOOKING calendar (when the next decisions land) — the
complement to the FOMC statement RSS feed (which reports decisions after the
fact). Together they replace the FMP economic-calendar we can't access.

FOMC meetings are 2-day events; the policy statement drops on the SECOND day
(~2pm ET). That end day is the market-moving date, so it's what we store as
event_date. A trailing '*' on the date marks a scheduled press conference.
"""
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.base import BaseCollector, SchemaDrift
from collectors.timeutil import utcnow

CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


class FomcCollector(BaseCollector):
    """FOMC forward meeting schedule → macro_calendar (event_type='FOMC')."""

    name = "fomc"
    schedule = "0 12 * * 1"  # weekly Monday — schedule changes rarely
    # Forward-looking calendar: "newest row" age is meaningless (it lists
    # future dates). Liveness = heartbeat + consecutive-empty, not data ts.
    staleness_by_data_ts = False
    use_env_proxy = True  # federalreserve.gov works either way; mihomo is fine

    def fetch(self) -> list[dict[str, Any]]:
        with self.make_client(timeout=30) as client:
            resp = client.get(CALENDAR_URL)
            resp.raise_for_status()
            html = resp.text

        # Split the page into per-year segments so each meeting's year is
        # unambiguous. Year headings appear as <... >YYYY FOMC Meetings<.
        year_split = re.split(r"(\d{4})\s+FOMC Meetings", html)
        if len(year_split) < 3:
            raise SchemaDrift("FOMC calendar: no '<YYYY> FOMC Meetings' headings found")

        rows = []
        # year_split = [prefix, year1, body1, year2, body2, ...]
        for i in range(1, len(year_split) - 1, 2):
            year = int(year_split[i])
            body = year_split[i + 1]

            months = re.findall(r'fomc-meeting__month[^>]*>\s*(?:<[^>]+>)?\s*([A-Za-z]+)', body)
            dates = re.findall(r'fomc-meeting__date[^>]*>\s*([^<]+?)\s*<', body)

            for month_name, raw_date in zip(months, dates):
                parsed = self._parse_meeting(year, month_name, raw_date)
                if parsed:
                    rows.append(parsed)

        return rows

    def _parse_meeting(self, year: int, month_name: str, raw_date: str) -> dict | None:
        """Turn ('2026','January','27-28*') → statement-day row. None to skip."""
        month = MONTHS.get(month_name.strip().lower())
        if month is None:
            return None

        raw = raw_date.strip()
        has_press_conf = "*" in raw
        clean = raw.replace("*", "").strip()

        # Skip administrative entries like "22 (notation vote)"
        if "notation" in clean.lower() or "(" in clean:
            return None

        # Expect "N-N" (two-day) or bare "N". Take the LAST day = statement day.
        m = re.match(r"^(\d{1,2})(?:\s*-\s*(\d{1,2}))?$", clean)
        if not m:
            return None
        end_day = int(m.group(2) or m.group(1))

        try:
            event_date = date(year, month, end_day)
        except ValueError:
            return None

        return {
            "event_type": "FOMC",
            "event_date": event_date.isoformat(),
            "detail": f"{month_name[:3]} {raw}",
            "has_press_conf": has_press_conf,
            "source": "federalreserve",
            "fetched_at": utcnow(),
        }

    def validate(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        required = {"event_type", "event_date", "source"}
        if not required.issubset(set(rows[0].keys())):
            raise SchemaDrift(f"Missing fields: {required - set(rows[0].keys())}")
        # Sanity: FOMC has ~8 meetings/year. If we parse <5 total, the page
        # structure likely drifted — fail loud rather than store garbage.
        if len(rows) < 5:
            raise SchemaDrift(f"Only parsed {len(rows)} FOMC meetings — layout drift?")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Upsert; a meeting's date can shift, so DELETE+INSERT per PK."""
        if not rows:
            return 0

        import duckdb
        conn = duckdb.connect(str(self.db_path))

        changed = 0
        for r in rows:
            conn.execute(
                "DELETE FROM macro_calendar WHERE event_type=? AND event_date=? AND source=?",
                [r["event_type"], r["event_date"], r["source"]],
            )
            conn.execute(
                """
                INSERT INTO macro_calendar
                (event_type, event_date, detail, has_press_conf, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [r["event_type"], r["event_date"], r["detail"],
                 r["has_press_conf"], r["source"], r["fetched_at"]],
            )
            changed += 1

        conn.close()
        return changed


def main():
    FomcCollector().run()


if __name__ == "__main__":
    main()

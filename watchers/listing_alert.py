#!/usr/bin/env python3
"""
Listing announcement watcher — standalone process, low latency.

Polls OKX + Binance announcement endpoints every 5s.
New listing → Telegram push. Does NOT write DB, does NOT call LLM.

This is the highest-ROI, lowest-intelligence part of the system.

NOTE (Appendix A #1): verify whether OKX announcements are at
/api/v5/support/announcements before relying on it.
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.alerts import _send_telegram
from collectors.config import get_config

POLL_INTERVAL = 5  # seconds

OKX_ANNOUNCEMENTS = "https://www.okx.com/api/v5/support/announcements"
BINANCE_ANNOUNCEMENTS = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
    "?catalogId=48&pageNo=1&pageSize=10"
)

# Keywords that indicate a listing (multilingual)
LISTING_KEYWORDS = ["list", "上线", "上币", "will list", "launch", "spot trading"]


def is_listing(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in LISTING_KEYWORDS)


def poll_okx(client: httpx.Client, seen: set) -> list[str]:
    """Poll OKX announcements. Returns list of new listing titles."""
    new = []
    try:
        resp = client.get(OKX_ANNOUNCEMENTS, params={"annType": "announcements-new-listings"})
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", [{}])[0].get("details", []):
            title = item.get("title", "")
            url = item.get("url", "")
            key = f"okx:{url or title}"
            if key not in seen and is_listing(title):
                seen.add(key)
                new.append(f"🟢 [OKX] {title}\n{url}")
    except Exception as e:
        print(f"⚠️  OKX poll error: {e}", file=sys.stderr)
    return new


def poll_binance(client: httpx.Client, seen: set) -> list[str]:
    """Poll Binance announcements. Returns list of new listing titles."""
    new = []
    try:
        resp = client.get(BINANCE_ANNOUNCEMENTS)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("data", {}).get("articles", [])
        for item in articles:
            title = item.get("title", "")
            code = item.get("code", "")
            key = f"binance:{code or title}"
            if key not in seen and is_listing(title):
                seen.add(key)
                url = f"https://www.binance.com/en/support/announcement/{code}"
                new.append(f"🟡 [Binance] {title}\n{url}")
    except Exception as e:
        print(f"⚠️  Binance poll error: {e}", file=sys.stderr)
    return new


def main():
    config = get_config()
    token = config.alerts.telegram_token
    chat_id = config.alerts.telegram_chat_id
    proxy = config.proxy.https_proxy or config.proxy.http_proxy

    if not token or not chat_id:
        print("⚠️  Telegram not configured. Alerts will print to stdout only.", file=sys.stderr)

    seen: set = set()
    kwargs = {"timeout": 10}
    if proxy:
        kwargs["proxy"] = proxy

    print(f"👀 Listing watcher started (poll every {POLL_INTERVAL}s)")

    # Warm-up pass: mark existing announcements as seen (don't alert on startup)
    with httpx.Client(**kwargs) as client:
        poll_okx(client, seen)
        poll_binance(client, seen)
    print(f"   Warm-up complete: {len(seen)} existing announcements marked seen")

    while True:
        try:
            with httpx.Client(**kwargs) as client:
                alerts = poll_okx(client, seen) + poll_binance(client, seen)

            for msg in alerts:
                print(msg)
                if token and chat_id:
                    try:
                        _send_telegram(token, chat_id, msg)
                    except Exception as e:
                        print(f"⚠️  Telegram send failed: {e}", file=sys.stderr)

        except Exception as e:
            print(f"⚠️  Poll cycle error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

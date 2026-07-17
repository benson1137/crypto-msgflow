"""Alert channels."""
import sys
from pathlib import Path

# Add project root if running as module
if __name__ != "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.config import get_config


def send_alert(collector: str, error: Exception, context: str | None = None):
    """
    Send alert to configured channels.

    For now: print to stderr (cron will email).
    TODO: Telegram bot integration.
    """
    config = get_config()

    msg = f"🚨 Collector '{collector}' failed\n"
    msg += f"Error: {error.__class__.__name__}: {error}\n"
    if context:
        msg += f"Context: {context}\n"

    # Stderr for cron
    print(msg, file=sys.stderr)

    # TODO: Telegram
    if config.alerts.telegram_token and config.alerts.telegram_chat_id:
        try:
            _send_telegram(config.alerts.telegram_token, config.alerts.telegram_chat_id, msg)
        except Exception as e:
            print(f"⚠️  Failed to send Telegram alert: {e}", file=sys.stderr)


def _send_telegram(token: str, chat_id: str, message: str):
    """Send message via Telegram bot."""
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = httpx.post(
        url,
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    resp.raise_for_status()


def send_stale_alert(collector: str, max_data_ts, max_staleness):
    """Alert that data is stale."""
    msg = f"⚠️  Collector '{collector}' data is stale\n"
    msg += f"Latest data timestamp: {max_data_ts}\n"
    msg += f"Max allowed staleness: {max_staleness}\n"

    print(msg, file=sys.stderr)

    config = get_config()
    if config.alerts.telegram_token and config.alerts.telegram_chat_id:
        try:
            _send_telegram(config.alerts.telegram_token, config.alerts.telegram_chat_id, msg)
        except Exception:
            pass

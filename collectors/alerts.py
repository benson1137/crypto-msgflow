"""Alert channels."""
import sys
from pathlib import Path

# Add project root if running as module
if __name__ != "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.config import get_config


def send_alert(collector: str, error: Exception, context: str | None = None):
    """Send a failure alert to stderr + configured push channels."""
    config = get_config()

    msg = f"🚨 Collector '{collector}' failed\n"
    msg += f"Error: {error.__class__.__name__}: {error}\n"
    if context:
        msg += f"Context: {context}\n"

    print(msg, file=sys.stderr)  # stderr → cron MAILTO

    if config.alerts.lark_chat_id:
        try:
            _send_lark_cli(config.alerts.lark_chat_id, msg)
        except Exception as e:
            print(f"⚠️  Failed to send Lark alert: {e}", file=sys.stderr)

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


# Bridge profile env needed for lark-cli bot identity in a detached (cron)
# process. Values are the current profile's; lark-cli reads bot creds from
# LARKSUITE_CLI_CONFIG_DIR on disk, so they persist outside the bridge.
_LARK_ENV = {
    "LARK_CHANNEL": "1",
    "LARK_CHANNEL_HOME": "/path/to/lark-channel-home",
    "LARK_CHANNEL_PROFILE": "claude",
    "LARKSUITE_CLI_CONFIG_DIR": "/path/to/lark-cli-config",
    "LARK_CHANNEL_CONFIG": "/path/to/lark-cli-source/config.json",
}


def _send_lark_cli(chat_id: str, message: str):
    """Send a Lark message via lark-cli (bot identity).

    Runs lark-cli as a subprocess with the bridge profile env injected so a
    cron-detached process keeps the bot identity. Raises on non-zero exit or
    when the JSON result is not ok (lark-cli exits 0 on some logical errors).
    """
    import json
    import os
    import subprocess

    env = {**os.environ, **_LARK_ENV}
    proc = subprocess.run(
        ["lark-cli", "im", "+messages-send", "--chat-id", chat_id,
         "--text", message],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli exit {proc.returncode}: {proc.stderr[:200]}")
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli non-JSON output: {proc.stdout[:200]}")
    if not body.get("ok"):
        raise RuntimeError(f"lark-cli send failed: {proc.stdout[:200]}")


def send_stale_alert(collector: str, max_data_ts, max_staleness):
    """Alert that data is stale."""
    msg = f"⚠️  Collector '{collector}' data is stale\n"
    msg += f"Latest data timestamp: {max_data_ts}\n"
    msg += f"Max allowed staleness: {max_staleness}\n"

    print(msg, file=sys.stderr)

    config = get_config()
    if config.alerts.lark_chat_id:
        try:
            _send_lark_cli(config.alerts.lark_chat_id, msg)
        except Exception:
            pass
    if config.alerts.telegram_token and config.alerts.telegram_chat_id:
        try:
            _send_telegram(config.alerts.telegram_token, config.alerts.telegram_chat_id, msg)
        except Exception:
            pass


def _test():
    """python -m collectors.alerts → send a test alert to all channels."""
    cfg = get_config()
    msg = "✅ crypto-msgflow 告警测试 — 通道已接通（lark-cli bot）"
    print(msg, file=sys.stderr)
    if cfg.alerts.lark_chat_id:
        _send_lark_cli(cfg.alerts.lark_chat_id, msg)
        print("  → Lark sent", file=sys.stderr)
    else:
        print("  ⚠️  lark_chat_id 未配置", file=sys.stderr)


if __name__ == "__main__":
    _test()

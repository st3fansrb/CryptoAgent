"""Optional Telegram alerts for CryptoAgent.

Sends short messages on key events (position opened/closed, trading halted).
Completely optional: if ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` are not
set in ``.env``, every call is a silent no-op, so the agent runs unchanged.

Failures (network, bad token) are swallowed — an alert problem must NEVER
interrupt the trading cycle.
"""

from __future__ import annotations

import requests

import config


def is_configured() -> bool:
    """Return True if Telegram credentials are present."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def notify(message: str) -> bool:
    """Send a Telegram message if configured; never raise.

    Args:
        message: Plain-text message body.

    Returns:
        True if the message was sent, False if disabled or it failed.
    """
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": message},
            timeout=config.HTTP_TIMEOUT,
        )
        return resp.ok
    except Exception:  # noqa: BLE001 - alerts must never break the cycle
        return False


if __name__ == "__main__":
    # Quick test: `python notifier.py` sends a test message (or reports disabled).
    if not is_configured():
        print("Telegram NOT configured. Set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID in .env, then make sure you pressed Start in "
              "your bot's chat.")
    elif notify("✅ CryptoAgent test alert — alerts are working."):
        print("Sent! Check your Telegram.")
    else:
        print("Configured but send FAILED. Check the token/chat id, and that "
              "you pressed Start in the bot chat.")

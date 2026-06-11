"""Outbound Telegram notifications — Aria proactively messaging the user.

Used by unattended jobs (morning briefing, failure alerts) to push a message to the
allowlisted chat(s) without going through the agent loop. Needs TELEGRAM_BOT_TOKEN and
TELEGRAM_ALLOWED_CHAT_ID in .env. Returns True if at least one send succeeded — and
never raises, so notification failure can't take down the job doing the notifying.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_MSG_LIMIT = 4096


def send_telegram(text: str) -> bool:
    """Send a message to every allowlisted chat. Best-effort; never raises."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    raw_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "")
    chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]

    if not token or not chat_ids:
        print("⚠️ Telegram notify skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_CHAT_ID not set.")
        return False

    text = text or "(empty message)"
    any_sent = False
    for chat_id in chat_ids:
        try:
            for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_MSG_LIMIT]},
                    timeout=15,
                )
                resp.raise_for_status()
            any_sent = True
        except Exception as e:
            print(f"⚠️ Telegram notify to {chat_id} failed: {e}")

    return any_sent

"""Telegram chat interface (long-polling) — talk to Aria from your phone.

Zero infra: this long-polls the Telegram Bot API, so it needs no public endpoint,
no open port, and no server. It runs anywhere — including your laptop — yet is
reachable from your phone wherever you are. Reuses the agent from agent_core.py;
conversations are checkpointed to SQLite (thread id = chat id) and survive restarts.

Setup:
  1. Create a bot with @BotFather and copy the token into .env:
         TELEGRAM_BOT_TOKEN=123456:ABC-...
  2. Run this script, then message your bot. While no allowlist is set it's in
     "setup mode": it replies with your chat id. Put that in .env and restart:
         TELEGRAM_ALLOWED_CHAT_ID=123456789      # comma-separate for multiple
  3. After that, only allowlisted chats are served; everyone else is ignored.

The allowlist is REQUIRED in normal use — anyone who finds the bot can message it,
and Aria has access to your email. Run: `python3 telegram_bot.py`.
"""
import os
import time

import requests
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from agent_core import build_agent, open_checkpointer, thread_config, extract_text

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_MSG_LIMIT = 4096  # Telegram rejects messages longer than this


def allowed_chat_ids() -> set:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def send_message(chat_id, text: str):
    """Send a reply, splitting on Telegram's 4096-char limit."""
    text = text or "(no response)"
    for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
        try:
            requests.post(f"{API}/sendMessage",
                          json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_MSG_LIMIT]},
                          timeout=15)
        except Exception as e:
            print(f"[send error] {e}")


def show_typing(chat_id):
    try:
        requests.post(f"{API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass


def main():
    if not TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN in .env. Create a bot with @BotFather first.")
        return

    allowed = allowed_chat_ids()
    if not allowed:
        print("⚠️  TELEGRAM_ALLOWED_CHAT_ID not set — running in SETUP MODE: the bot will "
              "reply to anyone with their chat id so you can allowlist yourself, but will "
              "NOT answer questions. Set the env var and restart for normal use.")

    try:
        agent = build_agent(checkpointer=open_checkpointer())
    except Exception as e:
        print(f"Failed to initialize Aria's agent: {e}")
        return

    # Proactivity engine: reminders, important-email watch, Netflix automation.
    # Runs as a daemon thread so it lives and dies with the bot. Its notify path
    # both telegrams the user AND appends the message to each chat's conversation
    # thread, so the agent's history matches what the user actually saw.
    if allowed and not os.getenv("ARIA_ENGINE_DISABLED"):
        from langchain_core.messages import AIMessage
        from engine import start_engine_thread

        from notify import send_telegram as telegram_broadcast

        def engine_notify(text: str) -> bool:
            ok = telegram_broadcast(text)
            for chat_id in allowed:
                try:
                    agent.update_state(thread_config(f"telegram-{chat_id}"),
                                       {"messages": [AIMessage(content=text)]})
                except Exception as e:
                    print(f"[bot] couldn't append engine message to thread {chat_id}: {e}")
            return ok

        start_engine_thread(notify_fn=engine_notify)

    offset = None
    print("🎙️  Aria is live on Telegram. Press Ctrl+C to stop.")

    while True:
        try:
            resp = requests.get(f"{API}/getUpdates",
                                params={"timeout": 30, "offset": offset}, timeout=40)
            updates = resp.json().get("result", [])
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(3)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message or "text" not in message:
                continue

            chat_id = str(message["chat"]["id"])
            text = message["text"].strip()

            # Setup mode: help the user discover their chat id from the phone.
            if not allowed:
                send_message(chat_id,
                             "Setup: add this line to your .env and restart me —\n"
                             f"TELEGRAM_ALLOWED_CHAT_ID={chat_id}")
                print(f"[setup] message from chat_id={chat_id}: {text[:60]!r}")
                continue

            # Normal mode: silently ignore anyone not on the allowlist.
            if chat_id not in allowed:
                print(f"⛔ Ignored unauthorized chat_id={chat_id}: {text[:60]!r}")
                continue

            if text.lower() in ("/start", "/help"):
                send_message(chat_id, "Hi, I'm Aria. Just talk to me normally — I can manage "
                                      "your email, news, reminders, and more.")
                continue

            show_typing(chat_id)
            try:
                # The checkpointer holds the conversation; we send only the new message.
                result = agent.invoke({"messages": [HumanMessage(content=text)]},
                                      config=thread_config(f"telegram-{chat_id}"))
                reply = extract_text(result["messages"][-1].content)
            except Exception as e:
                reply = f"Sorry, something went wrong: {e}"
                print(f"[agent error] {e}")

            send_message(chat_id, reply)
            print(f"[{chat_id}] {text[:60]!r} -> {str(reply)[:80]!r}")


if __name__ == "__main__":
    main()

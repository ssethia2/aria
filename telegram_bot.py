"""Telegram chat interface (long-polling) — talk to Aria from your phone, by text
or voice note (voice is transcribed via Gemini and handled like text).

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
import subprocess
import tempfile
import threading
import time

import requests
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

from agent_core import (build_agent, open_checkpointer, thread_config, extract_text,
                        quick_answer)

# Brief, non-spammy "what I'm doing" notes by tool family (user asked for light
# progress updates during long tasks — not Claude-level detail).
_ACTIONS = [
    (('web_search', 'fetch_webpage', 'browse_and_report'), '🔍 Researching…'),
    (('create_calendar_event', 'get_calendar_events', 'list_my_calendars',
      'configure_shared_calendar'), '📅 Working with your calendar…'),
    (('draft_email_reply', 'read_and_summarize_emails', 'generate_morning_news'),
     '📧 Going through your email…'),
    (('add_commitment', 'list_commitments', 'complete_commitment', 'drop_commitment'),
     '✅ Updating your commitments…'),
    (('add_to_grocery_list', 'view_grocery_list', 'remove_from_grocery_list',
      'clear_grocery_list'), '🛒 On your grocery list…'),
    (('create_note', 'append_to_note', 'search_notes', 'read_note'), '📝 In your notes…'),
    (('get_weather',), '🌤 Checking the weather…'),
    (('check_packages',), '📦 Checking your packages…'),
    (('update_netflix_household',), '📺 Handling Netflix…'),
    (('list_lights', 'control_light'), '💡 Adjusting the lights…'),
    (('remember_person', 'get_person', 'list_people', 'add_memory', 'search_memory',
      'read_cold_storage', 'update_memory'), '🧠 Checking what I know…'),
    (('get_system_status',), '🩺 Running a self-check…'),
    (('add_standing_instruction', 'update_standing_instruction',
      'remove_standing_instruction'), '📌 Saving that as a standing rule…'),
]


def _friendly_action(tool_name: str) -> str:
    for names, note in _ACTIONS:
        if tool_name in names:
            return note
    return '⚙️ Working on it…'

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


# --- Interactive commitment checklist (tap to mark done, no typing) ---

def _checklist_markup():
    """(inline_keyboard or None, items). One full-width button per open commitment — tapping
    it marks that commitment done. callback_data is `done:<id>` (well under Telegram's 64B)."""
    from skills.commitment_manager import get_open_commitments
    items = get_open_commitments()
    if not items:
        return None, items
    rows = []
    for c in items:
        label = c["description"]
        if c.get("due_time"):
            label += f" · {c.get('due_date','')} {c['due_time']}"
        elif c.get("due_date"):
            label += f" · {c['due_date']}"
        rows.append([{"text": ("⬜ " + label)[:60], "callback_data": f"done:{c['id']}"}])
    return {"inline_keyboard": rows}, items


def send_checklist(chat_id, message_id=None):
    """Send the open-commitments checklist, or edit `message_id` in place to re-render it
    after a tap. Empty list → a clean 'all clear' (keyboard removed)."""
    markup, items = _checklist_markup()
    if not items:
        payload = {"chat_id": chat_id, "text": "🎉 No open commitments — all clear!"}
        method = "editMessageText" if message_id else "sendMessage"
        if message_id:
            payload["message_id"] = message_id
    else:
        payload = {"chat_id": chat_id,
                   "text": "Your open commitments — tap one to check it off:",
                   "reply_markup": markup}
        method = "editMessageText" if message_id else "sendMessage"
        if message_id:
            payload["message_id"] = message_id
    try:
        requests.post(f"{API}/{method}", json=payload, timeout=15)
    except Exception as e:
        print(f"[checklist] send failed: {e}")


def _answer_callback(cq_id, text=None):
    try:
        payload = {"callback_query_id": cq_id}
        if text:
            payload["text"] = text
        requests.post(f"{API}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"[checklist] answer failed: {e}")


def handle_callback(cq, allowed):
    """Handle a tapped checklist button: complete the commitment, ack, re-render in place."""
    cq_id = cq.get("id")
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")
    if allowed and chat_id not in allowed:      # same allowlist gate as messages
        _answer_callback(cq_id)
        return
    if data.startswith("done:"):
        try:
            from skills.commitment_manager import complete
            done = complete(int(data.split(":", 1)[1]))
            _answer_callback(cq_id, "✅ Done!" if done else "Already done")
        except Exception as e:
            print(f"[checklist] complete failed: {e}")
            _answer_callback(cq_id, "Couldn't update — try again")
        send_checklist(chat_id, message_id=message_id)
    else:
        _answer_callback(cq_id)


class TypingPulse:
    """Keep Telegram's chat action ('typing…' / 'recording voice…') alive while we
    work — a single sendChatAction expires after ~5s, leaving the user staring at
    radio silence during long agent runs.
    """

    def __init__(self, chat_id, action="typing"):
        self.chat_id, self.action = chat_id, action
        self._stop = threading.Event()

    def __enter__(self):
        def beat():
            while not self._stop.is_set():
                try:
                    requests.post(f"{API}/sendChatAction",
                                  json={"chat_id": self.chat_id, "action": self.action},
                                  timeout=10)
                except Exception:
                    pass
                self._stop.wait(4)
        threading.Thread(target=beat, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def synthesize_voice_note(text: str):
    """Render text to an OGG/Opus voice note. Returns the file path, or None.

    macOS `say` for synthesis + PyAV (bundled with faster-whisper) to transcode
    AIFF -> Opus, Telegram's required voice format. On the Pi this swaps to
    piper-tts — same interface.
    """
    import av
    aiff = ogg = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.aiff', delete=False) as f:
            aiff = f.name
        ogg = aiff.replace('.aiff', '.ogg')
        subprocess.run(['say', '-o', aiff, text[:3000]], check=True, timeout=60)

        inp = av.open(aiff)
        out = av.open(ogg, 'w', format='ogg')
        stream = out.add_stream('libopus', rate=48000)
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        for frame in inp.decode(audio=0):
            for rf in resampler.resample(frame):
                for packet in stream.encode(rf):
                    out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
        out.close()
        inp.close()
        return ogg
    except Exception as e:
        print(f"[tts] synthesis failed: {e}")
        if ogg and os.path.exists(ogg):
            os.unlink(ogg)
        return None
    finally:
        if aiff and os.path.exists(aiff):
            os.unlink(aiff)


def send_voice_note(chat_id, ogg_path, caption=None):
    """Send a voice note; caption carries the text when it fits (≤1024 chars)."""
    try:
        with open(ogg_path, 'rb') as f:
            requests.post(f"{API}/sendVoice",
                          data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                          files={"voice": f}, timeout=60)
        return True
    except Exception as e:
        print(f"[tts] sendVoice failed: {e}")
        return False


def repair_thread(agent, chat_id) -> int:
    """Neutralize dangling tool calls (a tool_use with no saved tool_result) in a chat's
    thread. These happen when the process is killed mid-turn (e.g. a restart between the
    model requesting a tool and the result being saved), and would otherwise 400 every
    future message in that thread. Replaces each dangling AI message (by id) with a plain
    text one. Returns how many were repaired."""
    cfg = thread_config(f"telegram-{chat_id}")
    try:
        msgs = agent.get_state(cfg).values.get("messages", [])
    except Exception:
        return 0
    from langchain_core.messages import ToolMessage
    repairs = []
    for i, m in enumerate(msgs):
        tcs = getattr(m, 'tool_calls', None) or []
        if not tcs:
            continue
        satisfied = set()
        for nxt in msgs[i + 1:]:
            if isinstance(nxt, ToolMessage):
                satisfied.add(nxt.tool_call_id)
            else:
                break
        if any(tc['id'] not in satisfied for tc in tcs):
            text = m.content if isinstance(m.content, str) else ""
            repairs.append(AIMessage(id=m.id, content=text or "(a tool action here was interrupted)"))
    if repairs:
        try:
            agent.update_state(cfg, {"messages": repairs})
        except Exception as e:
            print(f"[repair] failed for {chat_id}: {e}")
            return 0
    return len(repairs)


def run_agent_streaming(agent, chat_id, text) -> str:
    """Run the agent, sending a brief progress note as each new tool family is used,
    and return the final reply. Streaming the LangGraph steps means the user sees
    'Researching… / Working with your calendar…' instead of long radio silence."""
    cfg = thread_config(f"telegram-{chat_id}")
    last_note = None
    reply = None
    for chunk in agent.stream({"messages": [HumanMessage(content=text)]},
                              config=cfg, stream_mode="updates"):
        for _node, update in (chunk or {}).items():
            if not isinstance(update, dict):
                continue
            for m in update.get("messages", []) or []:
                tool_calls = getattr(m, "tool_calls", None) or []
                if tool_calls:
                    note = _friendly_action(tool_calls[0].get("name", ""))
                    if note != last_note:          # coalesce consecutive same-family steps
                        send_message(chat_id, note)
                        last_note = note
                elif isinstance(m, AIMessage):
                    txt = extract_text(m.content)
                    if txt and txt.strip():
                        reply = txt                 # last text-only AI message = the answer
    if reply is None:
        state = agent.get_state(cfg)
        reply = extract_text(state.values["messages"][-1].content)
    return reply


def transcribe_voice_message(file_id: str) -> str:
    """Download a Telegram voice note and transcribe it (Gemini via the router)."""
    info = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=15).json()
    file_path = info["result"]["file_path"]
    audio = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
                         timeout=60).content
    from llm_router import transcribe_audio
    return transcribe_audio(audio, mime_type="audio/ogg")


def _extract_photo(message):
    """(file_id, mime) for a photo in a Telegram message, or (None, None).
    Handles both compressed photos and images sent as documents."""
    if message.get("photo"):
        return message["photo"][-1]["file_id"], "image/jpeg"   # last entry = largest size
    doc = message.get("document") or {}
    if (doc.get("mime_type") or "").startswith("image/"):
        return doc["file_id"], doc["mime_type"]
    return None, None


def see_photo(file_id: str, mime: str, caption: str) -> str:
    """Download a photo the user sent and turn it into text the brain can act on: the
    vision model answers the caption (or describes the image), and the composed message
    flows through the NORMAL agent path so memory/commitments/tools all work on it."""
    info = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=15).json()
    file_path = info["result"]["file_path"]
    image = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
                         timeout=60).content
    from llm_router import describe_image
    seen = describe_image(image, mime, question=caption or None)
    return (f"[I sent you a photo. What it shows: {seen}]"
            + (f"\n\n{caption}" if caption else ""))


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

    # Startup self-check: log status, and telegram the user if anything is broken
    # on boot (a restart into a broken state should be loud, not silent).
    try:
        from healthcheck import run_all, summary, worst, FAIL
        results = run_all()
        print(summary(results))
        if worst(results) == FAIL and allowed:
            from notify import send_telegram
            send_telegram("⚠️ I just (re)started and something's wrong:\n\n" + summary(results))
    except Exception as e:
        print(f"[startup] health check failed to run: {e}")

    # Defensive: repair any thread left with a dangling tool call by a mid-turn restart,
    # so the user's first message after a deploy doesn't 400.
    for chat_id in allowed:
        n = repair_thread(agent, chat_id)
        if n:
            print(f"[startup] repaired {n} dangling tool-call(s) in chat {chat_id}")

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
            if update.get("callback_query"):     # a tapped checklist button
                handle_callback(update["callback_query"], allowed)
                continue
            message = update.get("message") or update.get("edited_message")
            if not message or not ("text" in message or "voice" in message
                                   or "photo" in message or "document" in message):
                continue

            chat_id = str(message["chat"]["id"])
            text = (message.get("text") or "").strip()

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

            # Voice notes: transcribe AFTER the allowlist gate, then treat as text.
            was_voice = False
            if not text and "voice" in message:
                if message["voice"].get("duration", 0) > 300:
                    send_message(chat_id, "That voice note is over 5 minutes — could you "
                                          "send a shorter one (or type it)?")
                    continue
                try:
                    with TypingPulse(chat_id):
                        text = transcribe_voice_message(message["voice"]["file_id"])
                except Exception as e:
                    print(f"[voice] transcription failed: {e}")
                    send_message(chat_id, "Sorry, I couldn't make out that voice note — "
                                          "mind trying again or typing it?")
                    continue
                if not text:
                    send_message(chat_id, "I couldn't hear anything in that voice note.")
                    continue
                was_voice = True
                send_message(chat_id, f"🎙️ Heard: “{text}”")

            # Photos: see them AFTER the allowlist gate, then treat as text — the vision
            # model answers the caption / describes the image, and the composed message
            # flows through the normal agent path (memory, commitments, tools).
            if not text:
                file_id, mime = _extract_photo(message)
                if file_id:
                    try:
                        with TypingPulse(chat_id):
                            text = see_photo(file_id, mime,
                                             (message.get("caption") or "").strip())
                    except Exception as e:
                        print(f"[photo] failed: {e}")
                        send_message(chat_id, "I couldn't open that photo — mind sending "
                                              "it again?")
                        continue
                elif "document" in message:
                    send_message(chat_id, "I can see photos now, but not that kind of "
                                          "file yet — send it as an image?")
                    continue

            if not text:
                continue

            if text.lower() in ("/start", "/help"):
                send_message(chat_id, "Hi, I'm Aria. Just talk to me normally — I can manage "
                                      "your email, news, reminders, and more.\n\n"
                                      "Tip: /tasks shows your commitments as a checklist you "
                                      "can tap to check off.")
                continue

            if text.lower() in ("/tasks", "/todo", "/todos", "/commitments", "/checklist"):
                send_checklist(chat_id)
                continue

            try:
                # Pulse keeps "typing…" alive; streaming sends brief progress notes.
                with TypingPulse(chat_id):
                    # Fast path first: a quick general-knowledge answer skips the full
                    # agent. None means "needs tools/data/current info" → full agent.
                    reply = quick_answer(agent, f"telegram-{chat_id}", text)
                    if reply is None:
                        reply = run_agent_streaming(agent, chat_id, text)
            except Exception as e:
                # A thread corrupted by a mid-turn interruption 400s on tool_use/result —
                # repair it and retry once, rather than surfacing a scary error.
                if "tool_use" in str(e) and "tool_result" in str(e):
                    n = repair_thread(agent, chat_id)
                    print(f"[bot] repaired {n} dangling tool-call(s), retrying")
                    try:
                        with TypingPulse(chat_id):
                            reply = run_agent_streaming(agent, chat_id, text)
                    except Exception as e2:
                        reply = "Hmm, that didn't go through on my end — mind trying again in a sec?"
                        print(f"[agent error after repair] {e2}")
                else:
                    reply = "Hmm, that didn't go through on my end — mind trying again in a sec?"
                    print(f"[agent error] {e}")

            # Reply in kind: spoken question gets a spoken answer (text rides along).
            delivered = False
            if was_voice:
                with TypingPulse(chat_id, action="record_voice"):
                    ogg = synthesize_voice_note(reply)
                if ogg:
                    caption = reply if len(reply) <= 1024 else None
                    delivered = send_voice_note(chat_id, ogg, caption=caption)
                    if delivered and caption is None:
                        send_message(chat_id, reply)  # full text when too long for caption
                    os.unlink(ogg)
            if not delivered:
                send_message(chat_id, reply)
            print(f"[{chat_id}] {'🎙️ ' if was_voice else ''}{text[:60]!r} -> {str(reply)[:80]!r}")


if __name__ == "__main__":
    main()

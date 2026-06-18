"""iMessage chat interface — talk to Aria from your phone, by text.

The iMessage counterpart to telegram_bot.py, for running Aria on a Mac. Zero infra: it
polls the macOS Messages database for new inbound messages (imessage_reader) and replies
through Messages.app via AppleScript (imessage_send). It reuses the same agent from
agent_core.py; conversations are checkpointed to SQLite (thread id = "imessage-<handle>")
and survive restarts, exactly like the Telegram interface.

Setup (full walkthrough in docs/imessage.md):
  1. Sign Messages.app into the iMessage account Aria answers from. Best practice is a
     DEDICATED Apple ID, so messages from your personal number arrive as inbound
     (is_from_me = 0) rather than syncing as your own sent messages.
  2. Grant Full Disk Access (to read chat.db) and Automation → Messages (to send) to
     whatever runs this — Terminal/iTerm or your Python host.
  3. Run it. While no allowlist is set it's in SETUP MODE: it texts back the handle of
     whoever messages it so you can allowlist yourself. Put that in .env and restart:
         IMESSAGE_ALLOWED_HANDLES=+15551234567        # comma-separate for multiple
  4. After that, only allowlisted handles are served; everyone else is ignored.

The allowlist is REQUIRED in normal use — Aria has access to your email and calendar, so
anyone who can text the account must not be able to drive the agent.
Run: `python3 imessage_bot.py`.
"""
import os
import time

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

from agent_core import (build_agent, open_checkpointer, thread_config, extract_text,
                        quick_answer)
from imessage_send import send_imessage
from imessage_reader import latest_rowid, fetch_new

load_dotenv()

POLL_SECONDS = float(os.getenv("IMESSAGE_POLL_SECONDS", "2"))
# Progress-note verbosity: "first" (one note per turn, default), "all" (one per tool
# family, like Telegram), or "off". iMessage bubbles each fire a phone notification, so
# "first" is the quiet default.
PROGRESS_NOTES = (os.getenv("IMESSAGE_PROGRESS_NOTES", "first") or "first").strip().lower()

# Brief, non-spammy "what I'm doing" notes by tool family (same set as telegram_bot).
_ACTIONS = [
    (('web_search', 'fetch_webpage', 'browse_and_report'), '🔍 Researching…'),
    (('create_calendar_event', 'get_calendar_events', 'list_my_calendars',
      'configure_shared_calendar', 'update_calendar_event', 'delete_calendar_event'),
     '📅 Working with your calendar…'),
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
    (('play_music', 'playback_control', 'now_playing', 'create_playlist'), '🎵 On your music…'),
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


def allowed_handles() -> set:
    raw = os.getenv("IMESSAGE_ALLOWED_HANDLES", "")
    return {h.strip() for h in raw.split(",") if h.strip()}


def _thread_id(handle: str) -> str:
    return f"imessage-{handle}"


def repair_thread(agent, handle) -> int:
    """Neutralize dangling tool calls (a tool_use with no saved tool_result) in a handle's
    thread — they happen when the process is killed mid-turn and would otherwise 400 every
    future message in that thread. Mirrors telegram_bot.repair_thread."""
    cfg = thread_config(_thread_id(handle))
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
            print(f"[repair] failed for {handle}: {e}")
            return 0
    return len(repairs)




def run_agent_streaming(agent, handle, text) -> str:
    """Run the agent, sending a brief progress note as tools are used (governed by
    PROGRESS_NOTES), and return the final reply."""
    cfg = thread_config(_thread_id(handle))
    last_note = None
    notes_sent = 0
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
                    want = (PROGRESS_NOTES == "all" and note != last_note) or \
                           (PROGRESS_NOTES == "first" and notes_sent == 0)
                    if want:
                        send_imessage(handle, note)
                        notes_sent += 1
                    last_note = note
                elif isinstance(m, AIMessage):
                    txt = extract_text(m.content)
                    if txt and txt.strip():
                        reply = txt
    if reply is None:
        state = agent.get_state(cfg)
        reply = extract_text(state.values["messages"][-1].content)
    return reply


def main():
    allowed = allowed_handles()
    if not allowed:
        print("⚠️  IMESSAGE_ALLOWED_HANDLES not set — running in SETUP MODE: the bot texts "
              "back the handle of anyone who messages it so you can allowlist yourself, but "
              "will NOT answer questions. Set the env var and restart for normal use.")

    try:
        agent = build_agent(checkpointer=open_checkpointer())
    except Exception as e:
        print(f"Failed to initialize Aria's agent: {e}")
        return

    # Proactivity engine: its notify path both iMessages the user AND appends the message to
    # each handle's thread, so the agent's history matches what the user saw (mirrors how
    # telegram_bot wires the engine).
    if allowed and not os.getenv("ARIA_ENGINE_DISABLED"):
        from engine import start_engine_thread

        def engine_notify(text: str) -> bool:
            ok = False
            for handle in allowed:
                if send_imessage(handle, text):
                    ok = True
                try:
                    agent.update_state(thread_config(_thread_id(handle)),
                                       {"messages": [AIMessage(content=text)]})
                except Exception as e:
                    print(f"[bot] couldn't append engine message to thread {handle}: {e}")
            return ok

        start_engine_thread(notify_fn=engine_notify)

    # Startup self-check: log status, and message the user if anything is broken on boot.
    try:
        from healthcheck import run_all, summary, worst, FAIL
        results = run_all()
        print(summary(results))
        if worst(results) == FAIL and allowed:
            for handle in allowed:
                send_imessage(handle, "⚠️ I just (re)started and something's wrong:\n\n"
                              + summary(results))
    except Exception as e:
        print(f"[startup] health check failed to run: {e}")

    # Repair any thread left with a dangling tool call by a mid-turn restart.
    for handle in allowed:
        n = repair_thread(agent, handle)
        if n:
            print(f"[startup] repaired {n} dangling tool-call(s) in thread {handle}")

    cursor = latest_rowid()
    print(f"💬 Aria is live on iMessage (from ROWID {cursor}). Ctrl+C to stop.")

    while True:
        try:
            new_messages = fetch_new(cursor)
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(POLL_SECONDS)
            continue

        for msg in new_messages:
            cursor = msg["rowid"]
            handle, text = msg["handle"], msg["text"].strip()
            if not text:
                continue

            # Setup mode: help the user discover the handle to allowlist.
            if not allowed:
                send_imessage(handle,
                              "Setup: add this line to your .env and restart me —\n"
                              f"IMESSAGE_ALLOWED_HANDLES={handle}")
                print(f"[setup] message from handle={handle}: {text[:60]!r}")
                continue

            # Normal mode: silently ignore anyone not on the allowlist.
            if handle not in allowed:
                print(f"⛔ Ignored unauthorized handle={handle}: {text[:60]!r}")
                continue

            if text.lower() in ("/start", "/help"):
                send_imessage(handle, "Hi, I'm Aria. Just talk to me normally — I can manage "
                                      "your email, news, reminders, and more.")
                continue

            try:
                reply = quick_answer(agent, _thread_id(handle), text)
                if reply is None:
                    reply = run_agent_streaming(agent, handle, text)
            except Exception as e:
                # A thread corrupted by a mid-turn interruption 400s on tool_use/result —
                # repair it and retry once rather than surfacing a scary error.
                if "tool_use" in str(e) and "tool_result" in str(e):
                    n = repair_thread(agent, handle)
                    print(f"[bot] repaired {n} dangling tool-call(s), retrying")
                    try:
                        reply = run_agent_streaming(agent, handle, text)
                    except Exception as e2:
                        reply = f"Sorry, something went wrong: {e2}"
                        print(f"[agent error after repair] {e2}")
                else:
                    reply = f"Sorry, something went wrong: {e}"
                    print(f"[agent error] {e}")

            send_imessage(handle, reply)
            print(f"[{handle}] {text[:60]!r} -> {str(reply)[:80]!r}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

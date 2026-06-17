"""Shared Aria agent construction — reused by every interface.

Both the local REPL (interact.py) and the Telegram bot (telegram_bot.py) build the
same agent, tools, and system prompt from here, so interfaces stay thin and there is
exactly one definition of "who Aria is and what she can do".

Conversation state is handled by a LangGraph SQLite checkpointer (aria_checkpoints.db),
so conversations survive process restarts. Interfaces pass ONLY the new user message
plus a stable thread id:

    agent = build_agent(checkpointer=open_checkpointer())
    result = agent.invoke({"messages": [HumanMessage(content=text)]},
                          config=thread_config("some-stable-id"))

The system prompt (with current date/time and profile) is injected fresh on every
model call via dynamic_prompt middleware — never stored in checkpointed state.
"""
import os
import sqlite3
from datetime import datetime

from langchain_core.tools import tool
from langchain_core.messages import SystemMessage
from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt, ModelRequest
from langgraph.checkpoint.sqlite import SqliteSaver

from llm_router import get_llm
from memory import add_memory, search_memory, read_cold_storage, load_profile
from tenant import is_guest, get_current_name
from skills.email_manager import (run_email_summary, draft_email_reply, update_memory,
                                  read_email_thread)
from people import remember_person, get_person, list_people
from skills.weather_manager import get_weather
from skills.research_manager import web_search, fetch_webpage
from skills.notes_manager import create_note, append_to_note, search_notes, read_note
from skills.grocery_manager import (add_to_grocery_list, view_grocery_list,
                                    remove_from_grocery_list, clear_grocery_list)
from skills.package_manager import check_packages
from skills.browser_manager import browse_and_report
from skills.home_assistant import list_lights, control_light
from skills.spotify_manager import (play_music, playback_control, now_playing,
                                    create_playlist)
from healthcheck import run_all as _health_run_all, summary as _health_summary
from langchain_core.tools import tool as _tool


@_tool
def get_system_status() -> str:
    """Report Aria's own health — config, credentials, engine, briefing, disk.
    Use when the user asks if everything's working / if she's healthy / what's wrong."""
    return _health_summary(_health_run_all())
from skills.news_manager import generate_news_brief
from skills.netflix_manager import update_netflix_household
from skills.commitment_manager import (add_commitment, list_commitments,
                                       complete_commitment, drop_commitment,
                                       analyze_commitments)
from skills.google_calendar import (create_calendar_event, get_calendar_events,
                                    list_my_calendars, configure_shared_calendar,
                                    update_calendar_event, delete_calendar_event)
from instructions import (render_for_prompt, add_standing_instruction,
                          update_standing_instruction, remove_standing_instruction)

CHECKPOINT_DB_PATH = os.path.join(os.path.dirname(__file__), "aria_checkpoints.db")


@tool
def read_and_summarize_emails() -> str:
    """Use this tool to read the user's recent emails, categorize them, and delete junk."""
    classifications, raw_emails = run_email_summary()
    if not classifications:
        return "No new emails to process or an error occurred."
    return f"Processed {len(raw_emails)} emails. Summary generated."


@tool
def generate_morning_news() -> str:
    """Use this tool to fetch and summarize the daily news and newsletters."""
    briefing = generate_news_brief()
    if not briefing:
        return "No news found or an error occurred."
    return "News briefing generated successfully."


def build_tools(guest=False):
    """The tool array Aria can call. Add new skills here (see CONTRIBUTING.md).

    guest=True returns the restricted, account-free subset for friend/guest sessions:
    isolated per-user memory + stateless general tools ONLY. Nothing that touches the
    owner's accounts or shared data (no email, calendar, contacts, notes, reminders,
    music, smart-home, browser, or system access)."""
    if guest:
        # Per-user, isolated tools only — memory, their own commitments, and stateless
        # lookups. Nothing touching the owner's accounts/data.
        return [add_memory, search_memory, add_commitment, list_commitments,
                complete_commitment, drop_commitment, get_weather, web_search, fetch_webpage]
    return [
        add_memory,
        search_memory,
        read_cold_storage,
        read_and_summarize_emails,
        read_email_thread,
        generate_morning_news,
        update_netflix_household,
        add_commitment,
        list_commitments,
        complete_commitment,
        drop_commitment,
        analyze_commitments,
        create_calendar_event,
        get_calendar_events,
        update_calendar_event,
        delete_calendar_event,
        list_my_calendars,
        configure_shared_calendar,
        add_standing_instruction,
        update_standing_instruction,
        remove_standing_instruction,
        remember_person,
        get_person,
        list_people,
        draft_email_reply,
        get_weather,
        web_search,
        fetch_webpage,
        update_memory,  # static profile keys, e.g. location (used by weather)
        create_note,
        append_to_note,
        search_notes,
        read_note,
        add_to_grocery_list,
        view_grocery_list,
        remove_from_grocery_list,
        clear_grocery_list,
        check_packages,
        browse_and_report,
        list_lights,
        control_light,
        play_music,
        playback_control,
        now_playing,
        create_playlist,
        get_system_status,
    ]


def _stable_prompt() -> str:
    """The cacheable system prompt: identity + tool guidance + standing instructions +
    profile. Deliberately holds NOTHING that changes per turn (the live timestamp lives
    in a separate uncached block; the people roster is gone — Aria uses list_people /
    get_person on demand). Stable across turns = Anthropic prompt-cache hits on the big
    tools+system prefix, the major chat-cost lever. Changes only when the user edits a
    standing rule or profile (rare), which is the right time to rebuild the cache.
    """
    profile_data = load_profile()
    standing_instructions = "" if is_guest() else render_for_prompt()

    return f"""You are Aria (Aria Responds Intelligently Always), a highly intelligent, proactive, and friendly personal AI assistant.
Your goal is to help your user manage their life, emails, and news.

YOUR CORE DUTY is making sure nothing the user commits to ever slips. Telling you IS their
system of record. When they mention — even in passing — a promise to someone, a deadline,
a renewal, someone's birthday, or a reply they owe, capture it with `add_commitment`
(confirm briefly after; offer first only if you're unsure they want it tracked).
Use `list_commitments` when they ask what they owe or what's pending; `complete_commitment`
when they say something's done; `drop_commitment` when they no longer intend to do it.
Use kind='people_date' with recurrence='yearly' for birthdays, and recurrence
('daily'/'weekly'/'monthly'/'every_N_days') for anything they want REPEATED on a
schedule ("remind me weekly to..."). Use due_time ONLY when they name a time of day.
You track FLAT commitments (with recurrence) — there is NO nested "subcommitment"/subtask
feature. If asked for subcommitments or subtasks, track it as ONE commitment and say so;
NEVER reply that you can't create or track commitments — that is your core capability and is
always available. (Same rule everywhere: don't deny a capability you have a tool for; if a
specific variation isn't supported, say what you CAN do instead of refusing outright.)
Use `analyze_commitments` when the user asks how they're doing, what keeps slipping, or
for a review — it surfaces patterns over time (what's overdue, who/what it concentrates on).

CALENDAR: for appointments and events with a date (dinners, flights, meetings), use
`create_calendar_event`. When telling the user what's ON their calendar, or cross-
referencing dates, report ONLY events that `get_calendar_events` actually returned this
turn — call it first, and list nothing it didn't return. NEVER invent, infer, or carry
over an "event" from earlier conversation (something the user merely mentioned wanting to
do is NOT on their calendar). If the tool returns nothing for a date, say that date is
clear. To change one, use `update_calendar_event` (rename/reschedule);
to remove one, `delete_calendar_event` — both find the event by searching and update
every copy (personal + shared), so you can now fix a wrong event yourself instead of
asking the user to delete it. Events = calendar; promises/tasks = commitments; something
can be both. Use `get_calendar_events` when asked about the schedule.

STANDING INSTRUCTIONS — the user's persistent rules. They are ALWAYS in force and
override tool defaults. When the user gives you a lasting rule ("from now on...",
"always...", "by default..."), SAVE it with `add_standing_instruction` — don't just
acknowledge it. When they change or revoke one, use update/remove. One-off requests
("just this once", "only my personal calendar this time") are NOT standing
instructions — honor them directly via tool parameters.
KEEP THE REGISTRY CURATED: before adding, check the list below — if a new rule
overlaps or refines an existing one, UPDATE that one instead of adding a near-
duplicate. Write rules tersely. This list is read on every turn; it must stay
a constitution, not a junk drawer. Current instructions:
{standing_instructions}

User Core Profile (Static):
{profile_data}

You also run an autonomous background engine that acts on the user's behalf without being
asked: it screens incoming email into an evening digest (tracking replies they owe as
commitments), pings timed commitments at their moment, and handles Netflix
household-update emails (browser automation). When it acts, the notification it sends
appears in this conversation as one of YOUR messages — own those actions; they were yours.
Records of past actions also live in your memory: if asked about something you may have
done that you don't see in this conversation, use `search_memory` BEFORE answering. Never
deny having done something without checking.

You have access to a semantic memory database and several active skills.
- Use `search_memory` when the user asks about their past preferences, or when you need context about a person or topic mentioned in conversation.
- Use `add_memory` when the user tells you a NEW fact, preference, or event about themselves. BE PROACTIVE in saving new preferences so you don't forget them!
- Use `read_and_summarize_emails` when the user asks you to check their inbox or summarize their mail.
- Use `read_email_thread` when the user asks about ONE specific thread ("what's the X email about?", "what did Rohan say?") — search for it and read it back. You DO have email access; never tell the user you can't access their Gmail.
- Use `generate_morning_news` to get the latest news.
- RESEARCH is one of your duties: use `web_search` + `fetch_webpage` for anything
  needing current information, lookups, comparisons, or recommendations — don't answer
  from stale knowledge when a quick search would do better. Use `get_weather` for weather.
- GROUNDING — CRITICAL: never state a SPECIFIC fact about the user's accounts (calendar
  events, emails, commitments, balances, package status, what's on a list) unless it came
  from a tool result THIS turn. Don't reconstruct it from earlier in the conversation or
  from memory and present it as currently true, and never invent it to seem complete. If
  you haven't fetched it, call the tool — or say you'll check — rather than guessing.
- LINKS — CRITICAL: NEVER write a URL from your own memory. You WILL get it wrong (a
  bare search page, a guessed path, a dead link), which wastes the user's time and
  breaks trust. Only ever share a link that was actually returned to you by `web_search`,
  `fetch_webpage`, or `browse_and_report`. When recommending specific products to buy,
  `web_search` each item by name and share the REAL result URL (the actual product or
  retailer page) — and if you do this you are also pulling live prices/availability
  rather than guessing. If you can't find a real link for something, give the product
  name and tell the user to search for it; do NOT invent a link to fill the gap.
- NOTES: you are the user's notes system (his Apple Notes archive is imported). Use
  `create_note`/`append_to_note` for lists, plans, and reference info he wants kept;
  `search_notes`/`read_note` when he asks about anything he noted down. Actionable
  todos are commitments, not notes — split them out when both appear together.
- GROCERIES & RECIPES: maintain a running grocery list with `add_to_grocery_list` /
  `view_grocery_list` / `remove_from_grocery_list` / `clear_grocery_list` (clear it
  after a shopping trip). When he names a dish or recipe ("add stuff for butter
  chicken"), work out the ingredients yourself — from recipe text, a URL via
  fetch_webpage, or your own knowledge — and add them all at once. Mention if a few
  pantry staples (salt, oil) are probably already on hand rather than padding the list.
- PACKAGES: `check_packages` finds shipping/delivery emails — summarize what's in
  transit vs delivered when he asks about a package.
- BROWSER TASKS: `browse_and_report` drives a real browser to explore a web flow
  (e.g. an airline meal pre-order link) and report back. Pass it the URL, what to do,
  and any facts he's given (last name, confirmation #). It EXPLORES and hands off —
  it never pays, orders, or enters passwords, and stops at that boundary with a link.
  Relay what it found and the link; confirm with him before any step that commits money.
  AVAILABILITY/BOOKING SEARCHES (lodging, flights, tickets, restaurants): CONSTRAIN THE
  SEARCH UP FRONT with the user's dates and party size — the most reliable way is to
  build the start_url with date/guest parameters already applied (e.g. an Airbnb URL
  with checkin/checkout/adults) so results are pre-filtered to what's actually
  available. NEVER recommend an option you haven't confirmed is available for his
  dates; if availability can't be verified, say so rather than presenting it. Pass the
  dates/party size to browse_and_report in `facts` and put them in the task too.
- SMART HOME: `control_light` (on/off, brightness, color) and `list_lights` operate his
  Matter lights via Home Assistant. "turn off the lights" → control_light("all", "off").
- MUSIC: `play_music` (search + play), `playback_control` (pause/resume/next/previous),
  `now_playing`, and `create_playlist` (for "make me a playlist for X", pick the tracks
  yourself and pass them). Spotify; needs an active device for playback.

HUMAN TOUCH — pay attention to the PEOPLE in the user's life. Use `list_people` to see
who you already know and `get_person` to recall details about one of them.
- When they mention someone you don't recognize — by name or role ("my girlfriend",
  "my boss") — check with `get_person`/`list_people` first; if still unknown, ask who
  they are naturally (ONE question) and save them with `remember_person` (relation,
  alias like "girlfriend", birthday if given). Lasting facts about a person go in their
  record (note=...), not loose memory.
- Check `get_person` before asking anything you might already know. NEVER re-ask a
  known fact — that is the cardinal sin of fake-human assistants.
- Saving a birthday auto-tracks it yearly; you never need a separate reminder for it.
- When something you're saving is missing a key detail (a commitment with no date, a
  person with no name, an event with no time), ask at most ONE natural follow-up
  question instead of silently storing a vague entry.
- Notice things worth circling back on ("how did the interview go?") and save them as
  memories so future-you can ask.
- When asked to draft/answer an email, use `draft_email_reply` — it only creates a
  Gmail draft for his review; nothing sends.

BE CONCISE. Default to a sentence or two — lead with the answer or result, then stop.
Skip preamble, don't restate the question, don't explain your steps or list caveats
unless asked. For options or lists, keep each line tight. Satvik prefers brief, direct
replies over thorough ones; a short answer he can act on beats a long one he has to skim.
"""


def build_system_message() -> SystemMessage:
    """System message as two blocks: a cache_control-marked STABLE block (cached across
    turns with the tool schemas — the big cost saver) and a tiny VOLATILE block holding
    the live timestamp after the cache breakpoint, so the clock can't bust the cache."""
    now = datetime.now().strftime('%A, %Y-%m-%d %H:%M:%S')
    # 1h cache TTL (vs the 5-min default) survives the gaps between sparse, bursty
    # assistant interactions — the dominant cause of cache misses for personal use.
    # Reads stay ~0.1x; the write premium (2x vs 1.25x) pays off after one re-hit/hour.
    ttl = os.getenv("ARIA_CACHE_TTL", "1h")
    stable = _stable_prompt()
    if is_guest():
        # Guest sessions get a restricted toolset; tell the model so it sets expectations
        # instead of pretending to have email/calendar. (Profile + standing rules are
        # already empty for guests, so this banner is the only owner-vs-guest prompt delta.)
        who = get_current_name()
        greeting = f"You're talking with {who}. " if who else ""
        stable = (f"GUEST MODE — {greeting}you are a personal demo of Aria for a guest user. "
                  "You have ONLY: your memory of THIS guest (add_memory / search_memory), their "
                  "own reminders/commitments (add/list/complete/drop_commitment), web search, "
                  "and weather. You do NOT have email, calendar, contacts, notes, music, smart-"
                  "home, or anyone's accounts — if asked for those, say you can't in guest mode. "
                  "Be warm, use their name, and remember what they tell you.\n\n") + stable
    return SystemMessage(content=[
        {"type": "text", "text": stable,
         "cache_control": {"type": "ephemeral", "ttl": ttl}},
        {"type": "text",
         "text": f"The current date and time is: {now}. Use this for any date math "
                 f"(e.g. computing a commitment's date from 'tomorrow' or 'Friday')."},
    ])


@dynamic_prompt
def _fresh_system_prompt(request: ModelRequest) -> SystemMessage:
    return build_system_message()


def open_checkpointer() -> SqliteSaver:
    """Open the shared SQLite conversation store (one per process).

    check_same_thread=False because the agent graph may touch the connection from
    worker threads; SqliteSaver serializes access internally.
    """
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    return SqliteSaver(conn)


def thread_config(thread_id: str) -> dict:
    """Invoke config selecting which persistent conversation thread to continue."""
    return {"configurable": {"thread_id": thread_id}}


def build_agent(checkpointer=None, guest=False):
    """Construct the LangGraph ReAct agent. Raises if no LLM can be initialized.

    Pass a checkpointer (open_checkpointer()) to get durable conversations; omit it
    for stateless one-shot use. guest=True builds the restricted guest agent (account-free
    toolset); invoke it only with a tenant context set (see tenant.set_current_user).
    """
    llm = get_llm(temperature=0)
    return create_agent(
        llm,
        build_tools(guest=guest),
        middleware=[_fresh_system_prompt],
        checkpointer=checkpointer,
    )


def extract_text(content) -> str:
    """Normalize an AI message's content to a string.

    Anthropic sometimes returns a list of content blocks instead of a plain string.
    """
    if isinstance(content, list):
        return next(
            (block['text'] for block in content
             if isinstance(block, dict) and block.get('type') == 'text'),
            str(content),
        )
    return content

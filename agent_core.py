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
from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt, ModelRequest
from langgraph.checkpoint.sqlite import SqliteSaver

from llm_router import get_llm
from memory import add_memory, search_memory, read_cold_storage, load_profile
from skills.email_manager import run_email_summary, draft_email_reply, update_memory
from people import roster_for_prompt, remember_person, get_person, list_people
from skills.weather_manager import get_weather
from skills.research_manager import web_search, fetch_webpage
from skills.notes_manager import create_note, append_to_note, search_notes, read_note
from skills.grocery_manager import (add_to_grocery_list, view_grocery_list,
                                    remove_from_grocery_list, clear_grocery_list)
from skills.package_manager import check_packages
from skills.browser_manager import browse_and_report
from skills.home_assistant import list_lights, control_light
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
                                       complete_commitment, drop_commitment)
from skills.google_calendar import (create_calendar_event, get_calendar_events,
                                    list_my_calendars, configure_shared_calendar)
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


def build_tools():
    """The full tool array Aria can call. Add new skills here (see CONTRIBUTING.md)."""
    return [
        add_memory,
        search_memory,
        read_cold_storage,
        read_and_summarize_emails,
        generate_morning_news,
        update_netflix_household,
        add_commitment,
        list_commitments,
        complete_commitment,
        drop_commitment,
        create_calendar_event,
        get_calendar_events,
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
        get_system_status,
    ]


def build_system_prompt() -> str:
    """Build the system prompt with the current time and profile injected fresh.

    Keep this identity + behavior only — no mutable state. Engine actions reach the
    agent as messages appended to the conversation thread (see telegram_bot.py), and
    older ones via search_memory; injecting them here would bloat every model call.
    """
    profile_data = load_profile()
    current_time = datetime.now().strftime('%A, %Y-%m-%d %H:%M:%S')
    standing_instructions = render_for_prompt()
    people_roster = roster_for_prompt()

    return f"""You are Aria (Aria Responds Intelligently Always), a highly intelligent, proactive, and friendly personal AI assistant.
Your goal is to help your user manage their life, emails, and news.
The current date and time is: {current_time}. Keep this in mind when computing dates for commitments.

YOUR CORE DUTY is making sure nothing the user commits to ever slips. Telling you IS their
system of record. When they mention — even in passing — a promise to someone, a deadline,
a renewal, someone's birthday, or a reply they owe, capture it with `add_commitment`
(confirm briefly after; offer first only if you're unsure they want it tracked).
Use `list_commitments` when they ask what they owe or what's pending; `complete_commitment`
when they say something's done; `drop_commitment` when they no longer intend to do it.
Use kind='people_date' with recurring_yearly=True for birthdays and anniversaries, and
due_time ONLY when they name a specific time of day.

CALENDAR: for appointments and events with a date (dinners, flights, meetings), use
`create_calendar_event`. Events = calendar; promises/tasks = commitments; something
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
- Use `generate_morning_news` to get the latest news.
- RESEARCH is one of your duties: use `web_search` + `fetch_webpage` for anything
  needing current information, lookups, comparisons, or recommendations — don't answer
  from stale knowledge when a quick search would do better. Use `get_weather` for weather.
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
- SMART HOME: `control_light` (on/off, brightness, color) and `list_lights` operate his
  Matter lights via Home Assistant. "turn off the lights" → control_light("all", "off").

HUMAN TOUCH — pay attention to the PEOPLE in the user's life. You know:
{people_roster}
- When they mention someone NOT on that roster — by name or by role ("my girlfriend",
  "my boss") — ask who they are naturally (ONE question) and save them with
  `remember_person` (relation, alias like "girlfriend", birthday if given). Lasting
  facts about a person go in their record (note=...), not loose memory.
- Use `get_person` before asking anything you might already know. NEVER re-ask a
  known fact — that is the cardinal sin of fake-human assistants.
- Saving a birthday auto-tracks it yearly; you never need a separate reminder for it.
- When something you're saving is missing a key detail (a commitment with no date, a
  person with no name, an event with no time), ask at most ONE natural follow-up
  question instead of silently storing a vague entry.
- Notice things worth circling back on ("how did the interview go?") and save them as
  memories so future-you can ask.
- When asked to draft/answer an email, use `draft_email_reply` — it only creates a
  Gmail draft for his review; nothing sends.

Be conversational, concise, and helpful. You do not need to explain the steps you are taking unless asked.
"""


@dynamic_prompt
def _fresh_system_prompt(request: ModelRequest) -> str:
    return build_system_prompt()


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


def build_agent(checkpointer=None):
    """Construct the LangGraph ReAct agent. Raises if no LLM can be initialized.

    Pass a checkpointer (open_checkpointer()) to get durable conversations; omit it
    for stateless one-shot use.
    """
    llm = get_llm(temperature=0)
    return create_agent(
        llm,
        build_tools(),
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

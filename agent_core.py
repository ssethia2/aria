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
from skills.email_manager import run_email_summary
from skills.news_manager import generate_news_brief
from skills.netflix_manager import update_netflix_household
from skills.commitment_manager import (add_commitment, list_commitments,
                                       complete_commitment, drop_commitment)
from skills.google_calendar import (create_calendar_event, get_calendar_events,
                                    list_my_calendars, configure_shared_calendar)

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
    ]


def build_system_prompt() -> str:
    """Build the system prompt with the current time and profile injected fresh.

    Keep this identity + behavior only — no mutable state. Engine actions reach the
    agent as messages appended to the conversation thread (see telegram_bot.py), and
    older ones via search_memory; injecting them here would bloat every model call.
    """
    profile_data = load_profile()
    current_time = datetime.now().strftime('%A, %Y-%m-%d %H:%M:%S')

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
`create_calendar_event` — it automatically follows the user's standing rule: the event
lands on BOTH his personal calendar and the one shared with his girlfriend (yellow).
Don't ask which calendar; the tool handles it. Events = calendar; promises/tasks =
commitments; something can be both. Use `get_calendar_events` when asked about the
schedule.

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

HUMAN TOUCH — pay attention to the PEOPLE in the user's life:
- When they mention someone by role ("my girlfriend", "my boss", "my roommate") and you
  don't know who that is, check `search_memory` first; if still unknown, ask their name
  naturally as part of your reply, then save it with `add_memory`. Once you know a name,
  use it. NEVER re-ask something memory already knows — check before asking.
- When something you're saving is missing a key detail (a commitment with no date, a
  person with no name, an event with no time), ask at most ONE natural follow-up question
  instead of silently storing a vague entry. One question, not an interrogation.
- Notice things worth circling back on ("how did the interview go?") and save them as
  memories so future-you can ask.

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

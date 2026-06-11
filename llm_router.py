"""Unified LLM factory with a built-in fallback chain (see docs/adr/0001).

All modules obtain their chat model via get_llm() rather than calling a provider
SDK directly. Two tiers:
  - standard: Claude Opus 4.8 -> Claude Sonnet 4.6 -> Gemini Flash (agent, briefing)
  - light:    Claude Haiku 4.5 -> Gemini Flash (high-frequency background screening)

When a chain link fails at invoke time the user is told via Telegram (once per model
per day) — silent fallback masked the claude-3 retirements for months in 2026.

Model lineup current as of 2026-06 (claude-3-opus / claude-3-5-sonnet were retired
by Anthropic in early 2026 and now 404). Note: Opus 4.8 removed sampling params —
do NOT pass temperature to it (400 error); it is still applied to the fallbacks.
"""
import json
import os
from datetime import date

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

ALERT_STATE_PATH = os.path.join(os.path.dirname(__file__), "router_alerts.json")


def _notify_fallback_once(model_name: str, error: str):
    """Telegram the user that a chain link is failing — at most once per model per day."""
    today = date.today().isoformat()
    try:
        with open(ALERT_STATE_PATH) as f:
            state = json.load(f)
    except Exception:
        state = {}

    if state.get(model_name) == today:
        return
    state[model_name] = today
    try:
        with open(ALERT_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[router] couldn't persist alert state: {e}")

    from notify import send_telegram
    send_telegram(
        f"⚠️ Heads up: my model '{model_name}' is failing and I've switched to a fallback.\n\n"
        f"Error: {error}\n\n"
        f"I'll keep working, but if this persists the lineup in llm_router.py may need "
        f"updating (e.g. a retired model ID). I'll only mention this once today."
    )


def _with_failure_alert(model, model_name: str):
    """Attach an on-error listener so invoke-time failures surface to the user."""
    def on_error(run, *args, **kwargs):
        try:
            _notify_fallback_once(model_name, str(getattr(run, "error", "unknown"))[:300])
        except Exception as e:
            print(f"[router] fallback-alert hook failed: {e}")

    return model.with_listeners(on_error=on_error)


def _build_chain(specs):
    """Init each (name, factory) spec, drop init failures, return anchor.with_fallbacks(rest).

    Every link except the last gets a failure-alert listener — if the LAST link fails
    the whole chain raises, and the caller's own fail-loud path reports it.
    """
    models = []
    for name, build in specs:
        try:
            models.append((name, build()))
        except Exception as e:
            print(f"Warning: LLM init failed for {name}: {e}")

    if not models:
        raise ValueError("CRITICAL ERROR: No LLMs could be initialized. Check your API Keys in .env")

    wrapped = [
        _with_failure_alert(model, name) if i < len(models) - 1 else model
        for i, (name, model) in enumerate(models)
    ]
    anchor, fallbacks = wrapped[0], wrapped[1:]
    return anchor.with_fallbacks(fallbacks) if fallbacks else anchor


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe a voice recording via Gemini (the configured multimodal provider).

    Lives here so provider-SDK access stays centralized (ADR 0001's spirit) even
    though transcription isn't a chat-model call.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a speech-to-text engine. Output the verbatim transcript of "
                "the audio and NOTHING else — no preamble, no commentary, no quotes, "
                "no repetition. If the audio contains no speech, output nothing."),
            temperature=0),
        contents=[types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)])
    return (response.text or "").strip()


def get_llm(temperature=0, tier="standard"):
    """
    Returns a unified LangChain ChatModel with built-in fallbacks.

    tier="standard": Opus 4.8 -> Sonnet 4.6 -> Gemini Flash (most capable first)
    tier="light":    Haiku 4.5 -> Gemini Flash (cheap + fast, for frequent background calls)
    """
    if tier == "light":
        return _build_chain([
            ("claude-haiku-4-5", lambda: ChatAnthropic(model="claude-haiku-4-5", temperature=temperature)),
            ("gemini-2.5-flash", lambda: ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=temperature)),
        ])

    return _build_chain([
        # Opus 4.8 rejects sampling params (temperature would 400) — omit it.
        ("claude-opus-4-8", lambda: ChatAnthropic(model="claude-opus-4-8")),
        ("claude-sonnet-4-6", lambda: ChatAnthropic(model="claude-sonnet-4-6", temperature=temperature)),
        ("gemini-2.5-flash", lambda: ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=temperature)),
    ])

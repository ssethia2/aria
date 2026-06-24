"""Unified LLM factory with tiered, provider-fallback model chains (see docs/adr/0001).

Every module gets its model via get_llm(tier=...) — never a provider SDK directly. Three
tiers, each an ordered chain that falls through on error/rate-limit (LangChain
.with_fallbacks), so a quota wall or outage degrades instead of breaking:

  heavy    : Opus 4.8 -> Sonnet 4.6 -> Gemini   (agentic multi-step: browser, complex)
  standard : Sonnet 4.6 -> Opus 4.8 -> Gemini   (chat agent, insight, news, compaction)
  light    : Haiku 4.5 -> Gemini                (high-frequency screening + the chat
             fast-path: cheap, RELIABLE Haiku first. Gemini's free tier is only a
             fallback — its 20-requests/day cap is far too small to lead with, and
             doing so silently starved the agent down to free-tier scraps.)

Adding a provider (Groq/OpenRouter free models, etc.) = one more (name, factory) entry
in a chain; the fallback machinery is provider-agnostic.

When a chain link fails at invoke time the user is told via Telegram (once per model per
day) — EXCEPT the light tier, whose fall-through to free-tier Gemini is low-stakes and
stays silent. Model lineup current as of 2026-06.
"""
import json
import os
from datetime import date

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

ALERT_STATE_PATH = os.path.join(os.path.dirname(__file__), "router_alerts.json")


# --- model factories (Opus 4.8 rejects sampling params, so it ignores temperature) ---
def _opus(t):
    return ChatAnthropic(model="claude-opus-4-8")


def _sonnet(t):
    return ChatAnthropic(model="claude-sonnet-4-6", temperature=t)


def _haiku(t):
    return ChatAnthropic(model="claude-haiku-4-5", temperature=t)


def _gemini(t):
    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=t)


TIERS = {
    "heavy":    [("claude-opus-4-8", _opus), ("claude-sonnet-4-6", _sonnet),
                 ("gemini-2.5-flash", _gemini)],
    "standard": [("claude-sonnet-4-6", _sonnet), ("claude-opus-4-8", _opus),
                 ("gemini-2.5-flash", _gemini)],
    "light":    [("claude-haiku-4-5", _haiku), ("gemini-2.5-flash", _gemini)],
}


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
        f"updating (e.g. a retired model ID). I'll only mention this once today.")


def _with_failure_alert(model, model_name: str):
    def on_error(run, *args, **kwargs):
        try:
            _notify_fallback_once(model_name, str(getattr(run, "error", "unknown"))[:300])
        except Exception as e:
            print(f"[router] fallback-alert hook failed: {e}")
    return model.with_listeners(on_error=on_error)


def _build_chain(specs, alert=True):
    """Init each (name, factory) spec, drop init failures, return anchor.with_fallbacks(rest).
    Non-last links get a failure-alert listener when alert=True (off for the light tier,
    whose free-primary is expected to fall through daily)."""
    models = []
    for name, build in specs:
        try:
            models.append((name, build()))
        except Exception as e:
            print(f"Warning: LLM init failed for {name}: {e}")
    if not models:
        raise ValueError("CRITICAL ERROR: No LLMs could be initialized. Check your API Keys in .env")
    wrapped = [
        _with_failure_alert(m, name) if (alert and i < len(models) - 1) else m
        for i, (name, m) in enumerate(models)
    ]
    anchor, fallbacks = wrapped[0], wrapped[1:]
    return anchor.with_fallbacks(fallbacks) if fallbacks else anchor


_whisper_model = None


def _get_whisper():
    """Lazy-load the local Whisper model (ARIA_WHISPER_MODEL: 'base' default, 'tiny' for
    slower CPUs). Downloads once, then cached."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        size = os.getenv("ARIA_WHISPER_MODEL", "base")
        _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper_model


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe a voice recording. Local Whisper first (no quota, offline, private);
    Gemini multimodal as fallback. Lives here so provider access stays centralized."""
    import io
    try:
        segments, _info = _get_whisper().transcribe(io.BytesIO(audio_bytes))
        text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            return text
        print("[stt] local whisper heard nothing; trying Gemini")
    except Exception as e:
        print(f"[stt] local whisper failed ({e}); falling back to Gemini")

    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a speech-to-text engine. Output the verbatim transcript of "
                "the audio and NOTHING else — no preamble, no commentary, no quotes."),
            temperature=0),
        contents=[types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)])
    return (response.text or "").strip()


def get_llm(temperature=0, tier="standard"):
    """Return a tiered ChatModel with built-in fallbacks. See module docstring for tiers.
    Carries a cost-tracking callback so every call's token usage is recorded (cost_tracker)."""
    specs = TIERS.get(tier, TIERS["standard"])
    chain = []
    for name, factory in specs:
        chain.append((name, lambda t=temperature, f=factory: f(t)))
    built = _build_chain(chain, alert=(tier != "light"))
    try:
        from cost_tracker import CostTrackingHandler
        return built.with_config(callbacks=[CostTrackingHandler()])
    except Exception as e:
        print(f"[router] cost tracking not attached: {e}")
        return built

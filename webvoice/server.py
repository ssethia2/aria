"""Multi-user GUEST voice backend — friends trying Aria out.

Each friend gets an invite link carrying a token (`?t=...`). The token maps to a user_id,
and the backend serves them an ISOLATED guest Aria: their own memory, an account-free
toolset (no email/calendar/contacts/etc.), their own conversation thread. Friends can't
see your data or each other's. The owner uses their own interfaces (telegram/voice_live)
for full access — this endpoint is guest-only and invite-gated.

Endpoints:
  GET  /              -> the web voice client (friends open it as `…/?t=<token>`)
  GET  /live-token    -> (token-gated) mints a short-lived Gemini Live token
  POST /agent         -> (token-gated) runs the guest brain for one escalated request

Add a friend:  python3 webvoice/add_friend.py <name>   (prints the invite link)
Run:  uvicorn webvoice.server:app --host 0.0.0.0 --port 8800   (+ ngrok/cloudflared)
Needs GEMINI_API_KEY (Live + token) and ANTHROPIC_API_KEY (the brain) in .env.
"""
import os
import json
import base64
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Body, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage

from google import genai
from google.genai import types

import tenant
from webvoice import usage
from webvoice import push
from agent_core import (build_agent, open_checkpointer, thread_config, extract_text,
                        quick_answer)

load_dotenv()

HERE = Path(__file__).resolve().parent
FRIENDS_PATH = HERE / "friends.json"     # gitignored: { "<token>": "<user_id>", ... }
MODEL = os.getenv("ARIA_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
# OWNER MODE (off unless set): a secret token that unlocks the FULL agent (your real toolset
# + memory, no guest sandbox) so you can test the PWA as yourself. Only set this where your
# credentials live (your Mac) — NEVER on the public guest box.
OWNER_TOKEN = os.getenv("ARIA_OWNER_TOKEN")

# Guest front-of-house prompt: only the capabilities a guest actually has (personalized
# with their name when we know it).
def _system(name=None):
    who = f" with {name}" if name else ""
    use_name = f" Use {name}'s name naturally." if name else ""
    return ("You are Aria, talking" + who + " out loud — keep replies short, warm, and "
            "natural, never an essay or markdown." + use_name + " You can remember things "
            "about THIS person, track their reminders/commitments, and look things up. For "
            "anything needing your memory of them, their reminders, the web, or the weather, "
            "call escalate_to_aria and speak back the result. You do NOT have email, calendar, "
            "or any personal accounts — if asked, say you can't in this demo. Remember what "
            "they tell you so it feels personal.")


ESCALATE = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="escalate_to_aria",
    description="Hand a request to Aria's brain: the guest's own memory, web search, or weather.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"request": types.Schema(
            type=types.Type.STRING, description="the user's request in their own words")},
        required=["request"]))])
def _live_config(name=None):
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=_system(name))]),
        tools=[ESCALATE],
        input_audio_transcription={},
        output_audio_transcription={})


# OWNER front-of-house prompt: full capabilities (the real Aria reached via escalate).
def _owner_system():
    return ("You are Aria, talking with Satvik out loud — keep replies short, warm, and "
            "natural, never an essay or markdown. You have FULL access to his life via "
            "escalate_to_aria: email, calendar, reminders/commitments, notes, memory, the web, "
            "weather, music, and smart home. For anything beyond casual chat, call "
            "escalate_to_aria with his request and speak back the result.")


def _owner_live_config():
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=_owner_system())]),
        tools=[ESCALATE],
        input_audio_transcription={},
        output_audio_transcription={})

app = FastAPI()
_genai = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
_guest_agent = None      # one shared guest agent; per-request tenant context picks the user
_owner_agent = None      # full-toolset agent for OWNER MODE (only when ARIA_OWNER_TOKEN is set)


def _agent_instance():
    """Lazily build the guest agent (account-free toolset) on first use."""
    global _guest_agent
    if _guest_agent is None:
        _guest_agent = build_agent(checkpointer=open_checkpointer(), guest=True)
    return _guest_agent


def _owner_agent_instance():
    """Lazily build the FULL agent for owner mode (your real toolset + memory)."""
    global _owner_agent
    if _owner_agent is None:
        _owner_agent = build_agent(checkpointer=open_checkpointer(), guest=False)
    return _owner_agent


def _is_owner(token: str) -> bool:
    """True only when owner mode is enabled AND this token matches the owner secret."""
    return bool(OWNER_TOKEN) and (token or "").strip() == OWNER_TOKEN


def _friends() -> dict:
    try:
        return json.loads(FRIENDS_PATH.read_text())
    except Exception:
        return {}


def _resolve(token: str):
    """(user_id, display_name) for an invite token, or 403. Accepts the new
    {token: {"id","name"}} record and the legacy {token: "user_id"} string."""
    v = _friends().get((token or "").strip())
    if not v:
        raise HTTPException(status_code=403, detail="Invalid or missing invite link.")
    if isinstance(v, dict):
        return (v.get("id") or v.get("user_id")), v.get("name")
    return v, None


def _user_for(token: str) -> str:
    """Resolve an invite token to a user_id, or 403 (invite-only so strangers can't run up
    your Gemini/Claude bill)."""
    return _resolve(token)[0]


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/capture-worklet.js")
def worklet():
    return FileResponse(HERE / "capture-worklet.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse(HERE / "manifest.json", media_type="application/manifest+json")


@app.get("/icon.png")
def icon():
    return FileResponse(HERE / "icon.png", media_type="image/png")


@app.get("/sw.js")
def service_worker():
    return FileResponse(HERE / "sw.js", media_type="application/javascript")


@app.on_event("startup")
def _start_push():
    push.start_scheduler()      # no-op unless VAPID keys are configured


@app.get("/push/key")
def push_key():
    """The VAPID public key the browser needs to subscribe (empty if push isn't configured)."""
    return {"key": push.public_key()}


class PushSub(BaseModel):
    token: str = ""
    subscription: dict


@app.post("/push/subscribe")
def push_subscribe(req: PushSub):
    """Register a device's push subscription against its token's user (owner or guest)."""
    uid = "owner" if _is_owner(req.token) else _resolve(req.token)[0]
    push.add_subscription(uid, req.subscription)
    return {"ok": True}


@app.get("/live-token")
def live_token(t: str = ""):
    """Mint a single-use ephemeral Live token — invite-gated so only friends can connect.
    Owner token (if set) gets the full-capability voice config and skips the caps."""
    if _is_owner(t):
        tok = _genai.auth_tokens.create(config=types.CreateAuthTokenConfig(
            uses=1,
            live_connect_constraints=types.LiveConnectConstraints(
                model=MODEL, config=_owner_live_config()),
            http_options=types.HttpOptions(api_version="v1alpha")))
        return {"token": tok.name, "model": MODEL}
    uid, name = _resolve(t)
    if not usage.check_and_increment(uid, "tokens"):
        raise HTTPException(status_code=429, detail="Daily voice limit reached — try again tomorrow.")
    tok = _genai.auth_tokens.create(config=types.CreateAuthTokenConfig(
        uses=1,
        live_connect_constraints=types.LiveConnectConstraints(model=MODEL, config=_live_config(name)),
        http_options=types.HttpOptions(api_version="v1alpha")))
    return {"token": tok.name, "model": MODEL}


class AgentReq(BaseModel):
    request: str
    token: str = ""


@app.post("/agent")
def agent(req: AgentReq):
    """Answer one guest request — isolated to this friend's memory. Triages like the owner's
    text path: a cheap light-tier model handles pure general-knowledge directly, and the full
    (expensive) guest brain is reserved for anything touching their data/tools/the live world."""
    # OWNER MODE: full agent, your real toolset + memory, no guest sandbox, no caps.
    if _is_owner(req.token):
        try:
            result = _owner_agent_instance().invoke(
                {"messages": [HumanMessage(content=req.request)]},
                config=thread_config("owner-web"))
            return {"result": extract_text(result["messages"][-1].content)}
        except Exception as e:
            return {"result": f"Sorry, I hit an error. ({e})"}

    uid, name = _resolve(req.token)
    thread = f"guest-{tenant.safe_id(uid)}"
    brain = _agent_instance()

    # 1. Light-tier fast path (cheap). Pure — no tools/memory/profile — so it can't leak; it
    #    just reads this friend's own thread for context and answers, or returns None.
    if usage.allow(uid, "quick"):
        ans = quick_answer(brain, thread, req.request)
        if ans is not None:
            usage.record(uid, "quick")
            return {"result": ans}

    # 2. Full guest brain (escalation) — gated on the agent count cap AND the $/day ceiling.
    if not usage.allow(uid, "agent"):
        return {"result": "You've hit today's limit with me — let's pick this up tomorrow."}
    usage.record(uid, "agent")
    ctx = tenant.set_current_user(uid, name)  # contextvar: drives guest-mode prompt + name
    try:
        # The run config is the RELIABLE tenant carrier — data tools read it (not the
        # contextvar) and fail closed, so a friend's data can't leak even if context
        # propagation ever breaks across a thread boundary.
        cfg = thread_config(thread)
        cfg["configurable"].update(tenant=uid, guest=True)
        result = brain.invoke(
            {"messages": [HumanMessage(content=req.request)]}, config=cfg)
        return {"result": extract_text(result["messages"][-1].content)}
    except Exception as e:
        return {"result": f"Sorry, I hit an error. ({e})"}
    finally:
        tenant.reset_current_user(ctx)


# --- Push-to-talk: the FREE voice path (no paid audio APIs) ---------------------------
# Audio in -> local Whisper STT (on the Mac) -> the brain (subscription for the owner)
# -> local `say` TTS out as AAC/M4A (iOS-native). Gemini Live stays available behind the
# client's mode toggle for when realtime barge-in is worth cents-per-minute.

def _tts_m4a(text: str):
    """Text -> M4A/AAC bytes via macOS `say` + `afconvert`. None if synthesis unavailable
    (client then just shows the text reply)."""
    import re
    # Strip markdown the model writes for chat — asterisks/backticks/headers aren't speech.
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # [label](url) -> label
    aiff = m4a = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.aiff', delete=False) as f:
            aiff = f.name
        m4a = aiff.replace('.aiff', '.m4a')
        voice = os.getenv("ARIA_TTS_VOICE")
        cmd = ['say', '-o', aiff] + (['-v', voice] if voice else []) + [text[:3000]]
        subprocess.run(cmd, check=True, timeout=60)
        subprocess.run(['afconvert', '-f', 'm4af', '-d', 'aac', aiff, m4a],
                       check=True, timeout=60)
        with open(m4a, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"[voice] tts failed: {e}")
        return None
    finally:
        for p in (aiff, m4a):
            if p and os.path.exists(p):
                os.unlink(p)


@app.post("/voice-turn")
def voice_turn(t: str = "", body: bytes = Body(...),
               content_type: str = Header("audio/mp4")):
    """One push-to-talk turn: recorded clip in, transcript + reply + spoken audio back.
    Same auth as /agent (owner token or invite); token is checked BEFORE any CPU is spent."""
    if not _is_owner(t):
        _resolve(t)                      # 403 for strangers
    if not body or len(body) < 200:
        raise HTTPException(status_code=400, detail="No audio received.")

    from llm_router import transcribe_audio
    heard = (transcribe_audio(bytes(body), content_type or "audio/mp4") or "").strip()
    if not heard:
        return {"you": "", "reply": "I couldn't make that out — try again?",
                "audio_b64": None, "audio_mime": None}

    reply = agent(AgentReq(request=heard, token=t))["result"]
    audio = _tts_m4a(reply)
    return {"you": heard, "reply": reply,
            "audio_b64": base64.b64encode(audio).decode() if audio else None,
            "audio_mime": "audio/mp4" if audio else None}

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
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage

from google import genai
from google.genai import types

import tenant
from agent_core import build_agent, open_checkpointer, thread_config, extract_text

load_dotenv()

HERE = Path(__file__).resolve().parent
FRIENDS_PATH = HERE / "friends.json"     # gitignored: { "<token>": "<user_id>", ... }
MODEL = os.getenv("ARIA_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")

# Guest front-of-house prompt: only the capabilities a guest actually has.
SYSTEM = ("You are Aria, talking with a guest out loud — keep replies short, warm, and "
          "natural, never an essay or markdown. You can remember things about THIS person "
          "and look things up. For anything needing your memory of them, the web, or the "
          "weather, call escalate_to_aria with their request and speak back the result. You "
          "do NOT have email, calendar, contacts, or any personal accounts — if asked, say "
          "you can't in this demo. Remember what they tell you so it feels personal.")
ESCALATE = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="escalate_to_aria",
    description="Hand a request to Aria's brain: the guest's own memory, web search, or weather.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"request": types.Schema(
            type=types.Type.STRING, description="the user's request in their own words")},
        required=["request"]))])
LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=types.Content(parts=[types.Part(text=SYSTEM)]),
    tools=[ESCALATE],
    input_audio_transcription={},
    output_audio_transcription={})

app = FastAPI()
_genai = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
_guest_agent = None      # one shared guest agent; per-request tenant context picks the user


def _agent_instance():
    """Lazily build the guest agent (account-free toolset) on first use."""
    global _guest_agent
    if _guest_agent is None:
        _guest_agent = build_agent(checkpointer=open_checkpointer(), guest=True)
    return _guest_agent


def _friends() -> dict:
    try:
        return json.loads(FRIENDS_PATH.read_text())
    except Exception:
        return {}


def _user_for(token: str) -> str:
    """Resolve an invite token to a user_id, or 403 — keeps it invite-only so strangers
    can't run up your Gemini/Claude bill."""
    uid = _friends().get((token or "").strip())
    if not uid:
        raise HTTPException(status_code=403, detail="Invalid or missing invite link.")
    return uid


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


@app.get("/live-token")
def live_token(t: str = ""):
    """Mint a single-use ephemeral Live token — invite-gated so only friends can connect."""
    _user_for(t)
    tok = _genai.auth_tokens.create(config=types.CreateAuthTokenConfig(
        uses=1,
        live_connect_constraints=types.LiveConnectConstraints(model=MODEL, config=LIVE_CONFIG),
        http_options=types.HttpOptions(api_version="v1alpha")))
    return {"token": tok.name, "model": MODEL}


class AgentReq(BaseModel):
    request: str
    token: str = ""


@app.post("/agent")
def agent(req: AgentReq):
    """Run the guest brain for one escalated request — isolated to this friend's memory."""
    uid = _user_for(req.token)
    ctx = tenant.set_current_user(uid)       # routes memory + guest mode for this request
    try:
        result = _agent_instance().invoke(
            {"messages": [HumanMessage(content=req.request)]},
            config=thread_config(f"guest-{tenant.safe_id(uid)}"))
        return {"result": extract_text(result["messages"][-1].content)}
    except Exception as e:
        return {"result": f"Sorry, I hit an error. ({e})"}
    finally:
        tenant.reset_current_user(ctx)

"""Spike backend for the web voice client (the phone-direct architecture).

Endpoints:
  GET  /              -> the web voice client (index.html)
  GET  /live-token    -> mints a SHORT-LIVED Gemini Live token; the API key never
                         leaves this server, the browser connects to Google with the token.
  POST /agent         -> the escalate_to_aria handoff: runs the full Aria brain
                         (agent_core + memory + tools) for one request, returns text.

Run from the repo root:
    source venv/bin/activate
    uvicorn webvoice.server:app --host 0.0.0.0 --port 8800

iOS needs HTTPS for mic access, so expose it over a tunnel and open THAT url on the phone:
    cloudflared tunnel --url http://localhost:8800      # or: ngrok http 8800

Needs GEMINI_API_KEY (Live + token) and ANTHROPIC_API_KEY (the brain) in .env.
This is a spike — single shared conversation thread, no auth. Not for production.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage

from google import genai
from google.genai import types

from agent_core import build_agent, open_checkpointer, thread_config, extract_text

load_dotenv()

HERE = Path(__file__).resolve().parent
MODEL = os.getenv("ARIA_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
THREAD = "web-voice"

# The session config (system prompt + the escalate tool) is baked into the ephemeral token
# server-side, so the model is guaranteed to have the tool — a client-only config can be
# dropped/overridden by the token's constraints.
SYSTEM = ("You are Aria, talking with Satvik out loud. Keep replies short, warm, and natural "
          "— never an essay, never markdown. Handle casual chat yourself. For ANYTHING that "
          "needs his memory, email, calendar, reminders, notes, music, the web, or any "
          "multi-step task, call escalate_to_aria with his request, then speak the result.")
ESCALATE = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="escalate_to_aria",
    description=("Hand a request to Aria's full brain: memory, email, calendar, reminders, "
                 "notes, music, web search, or multi-step tasks."),
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
_agent = build_agent(checkpointer=open_checkpointer())


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/capture-worklet.js")
def worklet():
    return FileResponse(HERE / "capture-worklet.js", media_type="application/javascript")


@app.get("/live-token")
def live_token():
    """Mint a single-use ephemeral token so the browser can open Live without the API key."""
    tok = _genai.auth_tokens.create(config=types.CreateAuthTokenConfig(
        uses=1,
        live_connect_constraints=types.LiveConnectConstraints(
            model=MODEL,
            config=LIVE_CONFIG,
        ),
        http_options=types.HttpOptions(api_version="v1alpha"),
    ))
    return {"token": tok.name, "model": MODEL}


class AgentReq(BaseModel):
    request: str


@app.post("/agent")
def agent(req: AgentReq):
    """Run the full brain for one escalated request (the escalate_to_aria handoff)."""
    try:
        result = _agent.invoke({"messages": [HumanMessage(content=req.request)]},
                               config=thread_config(THREAD))
        return {"result": extract_text(result["messages"][-1].content)}
    except Exception as e:
        return {"result": f"Sorry, I hit an error reaching my tools. ({e})"}

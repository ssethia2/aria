"""Conversational voice mode — Gemini Live front-end with barge-in, backed by Aria's brain.

A realtime speech-to-speech session (Gemini Live) handles the natural back-and-forth:
it listens while it talks, so you can interrupt her mid-sentence. She answers casual
chat herself; for anything real — memory, email, calendar, reminders, notes, music,
web, multi-step work — she calls ONE tool, `escalate_to_aria`, which runs the existing
Claude agent (agent_core) and speaks back the result. Fast front-of-house, real brain
on demand.

  python3 voice_live.py     talk naturally; interrupt any time; Ctrl-C to quit.

Needs GEMINI_API_KEY. Model is overridable via ARIA_LIVE_MODEL (default: the 2.5
native-audio preview; set to a stable live model if the preview is unavailable on
your key). Shares the 'local-voice-live' conversation thread with the brain.
"""
import os
import sys
import queue
import asyncio
import threading

from dotenv import load_dotenv
from google import genai
from google.genai import types
from langchain_core.messages import HumanMessage

from core.agent_core import build_agent, open_checkpointer, thread_config, extract_text

load_dotenv()

MODEL = os.getenv("ARIA_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
IN_RATE = 16000      # Gemini Live expects 16 kHz PCM in
OUT_RATE = 24000     # and returns 24 kHz PCM out
CHUNK = 1024
THREAD_ID = "local-voice-live"

SYSTEM = (
    "You are Aria, talking with Satvik out loud. This is a spoken conversation: keep "
    "replies short, warm, and natural — never an essay, never markdown, never read URLs "
    "aloud. Handle casual chat and quick questions yourself. For anything that needs his "
    "memory, email, calendar, reminders, notes, music, the web, or any multi-step task, "
    "call escalate_to_aria with his request in his own words, then speak back what it "
    "returns as one natural sentence."
)

ESCALATE = types.FunctionDeclaration(
    name="escalate_to_aria",
    description=("Hand a request to Aria's full brain. Use for anything beyond casual "
                 "conversation: memory, email, calendar, reminders, notes, music, web "
                 "search, or multi-step tasks."),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"request": types.Schema(
            type=types.Type.STRING,
            description="The user's request, in their own words.")},
        required=["request"],
    ),
)


def run_brain(agent, request: str) -> str:
    """Run the full Claude agent for one request and return its spoken-back text."""
    result = agent.invoke({"messages": [HumanMessage(content=request)]},
                          config=thread_config(THREAD_ID))
    return extract_text(result["messages"][-1].content)


async def main():
    if not os.getenv("GEMINI_API_KEY"):
        print("Set GEMINI_API_KEY in .env first.")
        return

    print("Initializing Aria's brain…")
    agent = build_agent(checkpointer=open_checkpointer())
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    loop = asyncio.get_running_loop()

    try:
        import sounddevice as sd
    except Exception as e:
        print(f"Audio backend unavailable: {e}\nIn the venv: pip install sounddevice")
        return

    # --- speaker: a thread draining a queue so barge-in can flush it instantly ---
    play_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def player():
        with sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16") as out:
            while not stop.is_set():
                try:
                    chunk = play_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk:
                    out.write(chunk)

    threading.Thread(target=player, daemon=True).start()

    def flush_playback():
        try:
            while True:
                play_q.get_nowait()
        except queue.Empty:
            pass

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM)]),
        tools=[types.Tool(function_declarations=[ESCALATE])],
        input_audio_transcription={},
        output_audio_transcription={},
    )

    print(f"Connecting to {MODEL}…")
    try:
        cm = client.aio.live.connect(model=MODEL, config=config)
        session = await cm.__aenter__()
    except Exception as e:
        print(f"Couldn't open Live session: {e}\n"
              "If it's a model error, set ARIA_LIVE_MODEL to a live model your key supports "
              "(e.g. gemini-2.0-flash-live-001).")
        return

    print("🎙️  Live voice. Talk naturally — interrupt her any time. Ctrl-C to quit.")

    # --- mic: callback (audio thread) hands bytes to the asyncio loop ---
    in_q: asyncio.Queue = asyncio.Queue()

    def mic_cb(indata, frames, t, status):
        loop.call_soon_threadsafe(in_q.put_nowait, bytes(indata))

    mic = sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=mic_cb)
    mic.start()

    async def send_mic():
        while True:
            data = await in_q.get()
            await session.send_realtime_input(
                audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}"))

    async def receive():
        async for msg in session.receive():
            sc = msg.server_content
            if sc:
                if sc.interrupted:                       # user barged in
                    flush_playback()
                if sc.input_transcription and sc.input_transcription.text:
                    print(f"\nYou: {sc.input_transcription.text}")
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            play_q.put(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    print(sc.output_transcription.text, end="", flush=True)
                if sc.turn_complete:
                    print()
            if msg.tool_call:
                responses = []
                for fc in msg.tool_call.function_calls:
                    if fc.name == "escalate_to_aria":
                        req = (fc.args or {}).get("request", "")
                        print(f"\n[→ brain] {req}")
                        answer = await loop.run_in_executor(None, run_brain, agent, req)
                        print(f"[brain] {answer[:100]}")
                        responses.append(types.FunctionResponse(
                            id=fc.id, name=fc.name, response={"result": answer}))
                if responses:
                    await session.send_tool_response(function_responses=responses)

    try:
        await asyncio.gather(send_mic(), receive())
    finally:
        mic.stop()
        stop.set()
        await cm.__aexit__(None, None, None)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting live voice.")

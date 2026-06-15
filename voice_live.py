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
import time
import queue
import asyncio
import threading
import traceback
from collections import deque

import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from langchain_core.messages import HumanMessage

import voice_aec

from agent_core import build_agent, open_checkpointer, thread_config, extract_text

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
    duplex = "--duplex" in sys.argv          # headphones: raw mic, full barge-in
    no_aec = "--no-aec" in sys.argv          # force the half-duplex gate fallback
    aec_on = not duplex and not no_aec and voice_aec.available()
    canceller = voice_aec.make_canceller() if aec_on else None
    render_q = deque(maxlen=200)             # 16 kHz reference frames of what we're playing
    HANGOVER = 0.6                            # gate fallback: mute mic this long after she talks
    state = {"last_audio": 0.0}               # monotonic time of the last audio chunk *played*

    def player():
        ref_rem = np.zeros(0, dtype=np.int16)
        with sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16") as out:
            while not stop.is_set():
                try:
                    chunk = play_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not chunk:
                    continue
                out.write(chunk)
                state["last_audio"] = time.monotonic()
                if aec_on:                    # stash a 16 kHz reference of what we just played
                    ref16 = voice_aec.resample_to_16k(np.frombuffer(chunk, np.int16), OUT_RATE)
                    frames_, ref_rem = voice_aec.chunk_frames(np.concatenate([ref_rem, ref16]))
                    for f in frames_:
                        render_q.append(f.tobytes())

    player_thread = threading.Thread(target=player, daemon=True)
    player_thread.start()

    def flush_playback():
        try:
            while True:
                play_q.get_nowait()
        except queue.Empty:
            pass
        render_q.clear()                      # drop stale echo references on barge-in

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

    mode = ("Headphones/duplex — interrupt any time." if duplex
            else "Speaker barge-in (AEC) — talk over her." if aec_on
            else "Speaker mode — mic mutes while she talks (--duplex for barge-in).")
    print(f"🎙️  Live voice. {mode}  Ctrl-C to quit.")

    # --- mic: callback (audio thread) hands bytes to the asyncio loop ---
    in_q: asyncio.Queue = asyncio.Queue()

    def mic_cb(indata, frames, t, status):
        loop.call_soon_threadsafe(in_q.put_nowait, bytes(indata))

    mic = sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=mic_cb)
    mic.start()

    async def send_mic():
        mic_rem = np.zeros(0, dtype=np.int16)
        while True:
            data = await in_q.get()
            try:
                if aec_on:
                    # Echo-cancel each 10 ms mic frame against the matching played
                    # reference, then send the cleaned audio. The mic stays open, so you
                    # can talk over her (barge-in) without her hearing herself.
                    samples = np.frombuffer(data, dtype=np.int16)
                    frames_, mic_rem = voice_aec.chunk_frames(np.concatenate([mic_rem, samples]))
                    cleaned = []
                    for mf in frames_:
                        ref = render_q.popleft() if render_q else voice_aec.SILENCE
                        cleaned.append(canceller.process(mf.tobytes(), ref))
                    if cleaned:
                        await session.send_realtime_input(audio=types.Blob(
                            data=b"".join(cleaned), mime_type=f"audio/pcm;rate={IN_RATE}"))
                else:
                    # Gate fallback: drop mic while she's speaking (no barge-in). --duplex
                    # (headphones) skips the gate entirely.
                    if not duplex and (not play_q.empty()
                                       or time.monotonic() - state["last_audio"] < HANGOVER):
                        continue
                    await session.send_realtime_input(
                        audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}"))
            except Exception:
                print("\n   [mic send failed — session likely closed]")
                traceback.print_exc()
                return

    async def _handle(msg):
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
        if msg.go_away:                               # server about to disconnect
            print(f"\n[server ending session: {msg.go_away}]")
        if msg.tool_call:
            # Always return a response per call — if run_brain raises and we send
            # nothing, the model hangs forever waiting for the tool result.
            responses = []
            for fc in msg.tool_call.function_calls:
                req = (fc.args or {}).get("request", "")
                print(f"\n[→ brain] {req}")
                try:
                    answer = await loop.run_in_executor(None, run_brain, agent, req)
                except Exception:
                    print("   [brain error]")
                    traceback.print_exc()
                    answer = "Sorry, I hit an error reaching my tools."
                print(f"[brain] {answer[:100]}")
                responses.append(types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"result": answer}))
            if responses:
                await session.send_tool_response(function_responses=responses)

    async def receive():
        # session.receive() yields ONE model turn then completes; loop so the
        # conversation keeps going across turns instead of ending after the first reply.
        try:
            while True:
                got = False
                async for msg in session.receive():
                    got = True
                    try:
                        await _handle(msg)
                    except Exception:
                        print("\n   [message-handling error — continuing]")
                        traceback.print_exc()
                if not got:
                    await asyncio.sleep(0.05)         # avoid a tight spin on empty turns
        except genai_errors.APIError as e:
            if getattr(e, "code", None) != 1000:      # 1000 = normal close (Ctrl+C / end)
                print("\n   [receive stream error]")
                traceback.print_exc()
        except Exception:
            print("\n   [receive stream error]")
            traceback.print_exc()
        finally:
            print("\n[session closed]")

    try:
        # Exit as soon as either side ends (e.g. the session closes) instead of hanging.
        tasks = [asyncio.create_task(send_mic()), asyncio.create_task(receive())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
    finally:
        # Orderly teardown. Stop the mic first (halts callbacks into the loop), then let
        # the player thread close its PortAudio stream *in its own thread* before exit.
        # Without the join the daemon thread is killed mid-PortAudio at interpreter
        # shutdown, which double-frees → "malloc: pointer being freed was not allocated".
        try:
            mic.stop()
            mic.close()
        except Exception:
            pass
        stop.set()
        player_thread.join(timeout=2)
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting live voice.")

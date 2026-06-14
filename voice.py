"""Local voice interface — talk to Aria through your Mac's mic and speakers.

Hands-free by default: it waits for you to speak, records until you stop, transcribes
locally (Whisper, via llm_router — no cloud, private), runs the shared agent, and
speaks the reply aloud. Speech I/O is on-device; only the LLM call leaves the machine.

  python3 voice.py          hands-free (auto-detects speech start/end)
  python3 voice.py --ptt     push-to-talk (Enter to start, Enter to stop)

Say or type 'exit' (or Ctrl-C) to quit. Conversation is checkpointed under the
'local-voice' thread, so it survives restarts.

TTS uses macOS `say`; on Linux/Pi it falls back to piper-tts (set PIPER_MODEL) —
that's the only thing that changes when this moves to the Pi.
"""
import io
import os
import re
import sys
import wave
import queue
import shutil
import subprocess

import numpy as np
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from agent_core import build_agent, open_checkpointer, thread_config, extract_text
from llm_router import transcribe_audio

load_dotenv()

SAMPLE_RATE = 16000   # Whisper's native rate
BLOCK = 480           # 30 ms frames
SILENCE_HANG = 0.8    # seconds of trailing quiet that ends a turn
MAX_TURN = 30         # hard cap on one utterance (seconds)
EXIT_WORDS = {"exit", "quit", "goodbye", "bye", "stop"}


def _wav_bytes(samples: np.ndarray) -> bytes:
    """int16 mono WAV bytes from a float32 [-1, 1] array — what transcribe_audio decodes."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


def _for_speech(text: str) -> str:
    """Strip markdown so `say` doesn't read '*' / '#' / URLs aloud."""
    t = re.sub(r"`{1,3}", "", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)   # [label](url) -> label
    t = re.sub(r"https?://\S+", "link", t)
    t = re.sub(r"[*_#>]", "", t)
    return t.strip()


def speak(text: str):
    text = _for_speech(text)
    if not text:
        return
    if shutil.which("say"):                                  # macOS
        subprocess.run(["say", text[:3000]], timeout=180)
    elif shutil.which("piper") and shutil.which("aplay"):    # Linux/Pi
        model = os.getenv("PIPER_MODEL", "en_US-amy-medium")
        try:
            wav = subprocess.run(["piper", "--model", model, "--output_file", "-"],
                                 input=text.encode(), capture_output=True, timeout=180)
            subprocess.run(["aplay", "-q"], input=wav.stdout, timeout=180)
        except Exception as e:
            print(f"[tts] piper failed: {e}")
    # else: no TTS backend — the text is already printed, so we just stay silent.


def record_until_silence(sd) -> np.ndarray:
    """Wait for speech to start, then record until ~SILENCE_HANG of quiet."""
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(indata[:, 0].copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=cb):
        ambient = [_rms(q.get()) for _ in range(int(0.4 * SAMPLE_RATE / BLOCK))]
        floor = max(np.mean(ambient) * 3.0, 0.015)           # adaptive threshold

        frames, speaking, silent_for, elapsed = [], False, 0.0, 0.0
        per_block = BLOCK / SAMPLE_RATE
        while True:
            block = q.get()
            level = _rms(block)
            if not speaking:
                if level > floor:
                    speaking = True
                    frames.append(block)
                continue
            frames.append(block)
            elapsed += per_block
            silent_for = silent_for + per_block if level < floor else 0.0
            if silent_for >= SILENCE_HANG or elapsed >= MAX_TURN:
                break
    return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")


def record_ptt(sd) -> np.ndarray:
    """Push-to-talk: record between two Enter presses."""
    input("  [Enter] to start…")
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(indata[:, 0].copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=cb):
        input("  🔴 recording… [Enter] to stop")
    frames = []
    while not q.empty():
        frames.append(q.get())
    return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")


def main():
    ptt = "--ptt" in sys.argv
    try:
        import sounddevice as sd
    except ImportError:
        print("Missing dependency. In the venv: pip install sounddevice")
        return
    except OSError as e:
        print(f"Audio backend unavailable: {e}\n"
              "macOS usually bundles PortAudio with the wheel; if not: brew install portaudio")
        return

    print("Initializing Aria…")
    agent = build_agent(checkpointer=open_checkpointer())
    thread_id = "local-voice"
    print("🎙️  Voice mode. "
          + ("Press Enter to talk." if ptt else "Just start talking.")
          + "  Say 'exit' or hit Ctrl-C to quit.")

    while True:
        try:
            samples = record_ptt(sd) if ptt else record_until_silence(sd)
            if samples.size < SAMPLE_RATE * 0.3:             # <0.3 s — nothing useful
                continue
            text = transcribe_audio(_wav_bytes(samples), mime_type="audio/wav").strip()
            if not text:
                continue
            print(f"\nYou: {text}")
            if re.sub(r"[^a-z]", "", text.lower()) in EXIT_WORDS:
                speak("Goodbye.")
                break
            result = agent.invoke({"messages": [HumanMessage(content=text)]},
                                  config=thread_config(thread_id))
            reply = extract_text(result["messages"][-1].content)
            print(f"Aria: {reply}")
            speak(reply)
        except KeyboardInterrupt:
            print("\nExiting voice mode.")
            break
        except Exception as e:
            print(f"[voice error] {e}")


if __name__ == "__main__":
    main()

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
import threading
import subprocess
from collections import deque

import numpy as np
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from core.agent_core import build_agent, open_checkpointer, thread_config, extract_text
from core.llm_router import transcribe_audio

load_dotenv()

SAMPLE_RATE = 16000   # Whisper's native rate
BLOCK = 480           # 30 ms frames
SILENCE_HANG = float(os.getenv("ARIA_VOICE_HANG", "1.2"))       # trailing quiet that ends a turn (s)
MIN_SPEECH = float(os.getenv("ARIA_VOICE_MIN_SPEECH", "1.0"))   # min voiced audio before a turn can end (s)
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


def _drain(q: queue.Queue):
    """Discard everything captured so far — called before each turn so audio the mic
    heard while Aria was speaking (her own voice) doesn't poison the next utterance."""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def listen_hands_free(q: queue.Queue) -> np.ndarray:
    """Read from the live mic queue: wait for speech, then record until a sustained pause.
    A turn ends only after MIN_SPEECH of voiced audio AND SILENCE_HANG of trailing quiet,
    so it won't cut off a couple words in. Returns empty if no speech for ~20 s."""
    per_block = BLOCK / SAMPLE_RATE
    ambient = [_rms(q.get()) for _ in range(int(0.4 * SAMPLE_RATE / BLOCK))]
    # Sensitive enough not to clip soft speech, capped so a noisy calibration can't
    # make us deaf and end turns early.
    floor = min(max(np.mean(ambient) * 1.8, 0.01), 0.05)

    preroll = deque(maxlen=6)            # ~0.18 s kept before onset so word 1 isn't clipped
    recent = deque(maxlen=3)             # smooth ~90 ms so mid-word dips don't read as silence
    frames, speaking = [], False
    silent_for, voiced_for, elapsed, waited = 0.0, 0.0, 0.0, 0.0
    while True:
        try:
            block = q.get(timeout=1.0)
        except queue.Empty:
            return np.zeros(0, dtype="float32")   # stream stalled — let the loop re-prompt
        recent.append(_rms(block))
        level = max(recent)
        if not speaking:
            preroll.append(block)
            waited += per_block
            if level > floor:
                speaking = True
                frames.extend(preroll)
            elif waited >= 20.0:
                return np.zeros(0, dtype="float32")
            continue
        frames.append(block)
        elapsed += per_block
        if level >= floor:
            voiced_for += per_block
            silent_for = 0.0
        else:
            silent_for += per_block
        if (silent_for >= SILENCE_HANG and voiced_for >= MIN_SPEECH) or elapsed >= MAX_TURN:
            break
    return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")


def listen_ptt(q: queue.Queue) -> np.ndarray:
    """Push-to-talk over the live mic queue: capture between two Enter presses."""
    input("  [Enter] to start…")
    _drain(q)
    stop = threading.Event()
    threading.Thread(target=lambda: (input("  🔴 recording… [Enter] to stop"),
                                     stop.set()), daemon=True).start()
    frames = []
    while not stop.is_set():
        try:
            frames.append(q.get(timeout=0.1))
        except queue.Empty:
            pass
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

    # One persistent mic stream for the whole session — reopening per turn is flaky on
    # macOS and drops audio. We flush it (_drain) before each turn instead.
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(indata[:, 0].copy())

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=BLOCK, callback=cb)
    stream.start()
    print("🎙️  Voice mode. "
          + ("Press Enter to talk." if ptt else "Just start talking.")
          + "  Say 'exit' or hit Ctrl-C to quit.  (half-duplex: she can't hear you while she talks)")

    try:
        while True:
            _drain(q)                                        # drop her own voice / stale audio
            if not ptt:
                print("🎧 listening…")
            samples = listen_ptt(q) if ptt else listen_hands_free(q)
            if samples.size < SAMPLE_RATE * 0.3:             # <0.3 s — nothing useful
                if not ptt:
                    print("   (didn't catch that)")
                continue
            text = transcribe_audio(_wav_bytes(samples), mime_type="audio/wav").strip()
            if not text:
                print("   (couldn't make that out)")
                continue
            print(f"You: {text}")
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
    except Exception as e:
        print(f"[voice error] {e}")
    finally:
        stream.stop()
        stream.close()


if __name__ == "__main__":
    main()

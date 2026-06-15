"""Acoustic echo cancellation for the live voice mode (speexdsp).

Lets the mic stay open while Aria is speaking — so you can interrupt her (barge-in) on
speakers — by subtracting her own voice (the speaker echo) from the mic signal before it's
sent to Gemini. Without this, her voice loops back in and she answers herself.

speexdsp is an OPTIONAL dependency (needs a system lib: `brew install speexdsp` on macOS,
`apt install libspeexdsp-dev` on Linux/Pi). If it isn't importable, `available()` returns
False and voice_live falls back to half-duplex mic gating.

The canceller works on 10 ms (160-sample) int16 mono frames at 16 kHz. Callers feed it the
mic frame plus the matching reference frame (what was just played, resampled to 16 kHz);
its adaptive filter (length AEC_TAIL) absorbs the speaker→mic delay.
"""
import os

import numpy as np

AEC_RATE = 16000     # canceller runs at Gemini's input rate
AEC_FRAME = 160      # 10 ms
# Echo-tail length (samples). ~128 ms by default; raise via ARIA_AEC_TAIL if echo leaks
# through (i.e. she still hears herself) — speaker→mic latency exceeds the filter.
AEC_TAIL = int(os.getenv("ARIA_AEC_TAIL", "2048"))
SILENCE = np.zeros(AEC_FRAME, dtype=np.int16).tobytes()


def available() -> bool:
    """True if the speexdsp echo canceller can be imported."""
    try:
        import speexdsp  # noqa: F401
        return True
    except Exception:
        return False


def make_canceller():
    """Create a speexdsp EchoCanceller for 10 ms / 16 kHz frames."""
    from speexdsp import EchoCanceller
    return EchoCanceller.create(AEC_FRAME, AEC_TAIL, AEC_RATE)


def resample_to_16k(samples: np.ndarray, in_rate: int) -> np.ndarray:
    """Linear-resample int16 mono audio to 16 kHz. Used for the AEC reference signal, where
    interpolation quality is non-critical (the adaptive filter tolerates it)."""
    if samples.size == 0:
        return np.zeros(0, dtype=np.int16)
    if in_rate == AEC_RATE:
        return samples.astype(np.int16)
    n_out = int(round(samples.size * AEC_RATE / in_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    xi = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(xi, x, samples.astype(np.float32)).astype(np.int16)


def chunk_frames(buf: np.ndarray, size: int = AEC_FRAME):
    """Split buf into complete `size`-sample frames. Returns (frames, leftover) so the
    caller can carry the partial tail into the next block."""
    n = (buf.size // size) * size
    frames = [buf[i:i + size] for i in range(0, n, size)]
    return frames, buf[n:].copy()

"""Tests for the local voice interface's pure helpers (no audio hardware needed).

Run: python3 -m unittest discover tests
"""
import io
import unittest
import wave

import numpy as np

import voice


class TestVoiceHelpers(unittest.TestCase):
    def test_wav_bytes_roundtrip(self):
        samples = (np.sin(np.linspace(0, 50, 1600)) * 0.5).astype("float32")
        data = voice._wav_bytes(samples)
        with wave.open(io.BytesIO(data), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), voice.SAMPLE_RATE)
            self.assertEqual(w.getnframes(), len(samples))

    def test_rms_silence_vs_signal(self):
        self.assertEqual(voice._rms(np.zeros(0, dtype="float32")), 0.0)
        self.assertLess(voice._rms(np.zeros(480, dtype="float32")),
                        voice._rms(np.full(480, 0.5, dtype="float32")))

    def test_for_speech_strips_markdown_and_links(self):
        out = voice._for_speech("**Bold** see [docs](https://x.io) and `code` https://y.io")
        self.assertNotIn("*", out)
        self.assertNotIn("`", out)
        self.assertIn("docs", out)
        self.assertNotIn("https://x.io", out)
        self.assertIn("link", out)            # bare URL replaced

    def test_exit_word_normalization(self):
        import re
        for phrase in ("Exit.", "goodbye!", "  BYE  "):
            self.assertIn(re.sub(r"[^a-z]", "", phrase.lower()), voice.EXIT_WORDS)


if __name__ == "__main__":
    unittest.main()

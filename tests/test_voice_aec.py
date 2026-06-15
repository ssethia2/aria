"""Tests for the AEC helpers (pure; the canceller itself needs real audio to judge).

Run: python3 -m unittest discover tests
"""
import unittest

import numpy as np

import voice_aec


class TestVoiceAEC(unittest.TestCase):
    def test_resample_24k_to_16k_ratio(self):
        src = np.zeros(2400, dtype=np.int16)            # 100 ms @ 24 kHz
        out = voice_aec.resample_to_16k(src, 24000)
        self.assertEqual(out.dtype, np.int16)
        self.assertAlmostEqual(out.size, 1600, delta=2)  # 100 ms @ 16 kHz

    def test_resample_passthrough_when_already_16k(self):
        src = (np.arange(320) % 100).astype(np.int16)
        out = voice_aec.resample_to_16k(src, 16000)
        self.assertEqual(out.size, src.size)

    def test_resample_empty(self):
        self.assertEqual(voice_aec.resample_to_16k(np.zeros(0, dtype=np.int16), 24000).size, 0)

    def test_chunk_frames_splits_and_keeps_remainder(self):
        buf = np.arange(160 * 3 + 50, dtype=np.int16)
        frames, rem = voice_aec.chunk_frames(buf, 160)
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(f.size == 160 for f in frames))
        self.assertEqual(rem.size, 50)

    def test_silence_frame_is_one_10ms_frame(self):
        self.assertEqual(len(voice_aec.SILENCE), voice_aec.AEC_FRAME * 2)  # int16 bytes

    def test_available_is_bool(self):
        self.assertIsInstance(voice_aec.available(), bool)


if __name__ == "__main__":
    unittest.main()

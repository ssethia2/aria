"""Tests for the iMessage transport: attributedBody decoding and the send wrapper.
Offline — subprocess is mocked.
Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import patch, MagicMock

import imessage_reader
import imessage_send


def _attributed(text: str) -> bytes:
    """Build a minimal Messages attributedBody blob around `text`, matching the
    length-prefix scheme decode_attributed_body expects."""
    preamble = b"\x01\x94\x84\x01+"  # the 5 bytes skipped after the NSString marker
    raw = text.encode("utf-8")
    if len(raw) > 127:
        prefix = b"\x81" + len(raw).to_bytes(2, "little")
    else:
        prefix = bytes([len(raw)])
    return b"\x04\x0bNSString" + preamble + prefix + raw


class TestAttributedBody(unittest.TestCase):
    def test_short_string(self):
        self.assertEqual(
            imessage_reader.decode_attributed_body(_attributed("hello there")),
            "hello there")

    def test_long_string_uses_two_byte_length(self):
        s = "x" * 500
        self.assertEqual(imessage_reader.decode_attributed_body(_attributed(s)), s)

    def test_unicode_and_emoji(self):
        s = "café ☕ déjà"
        self.assertEqual(imessage_reader.decode_attributed_body(_attributed(s)), s)

    def test_empty_or_unrecognized(self):
        self.assertEqual(imessage_reader.decode_attributed_body(None), "")
        self.assertEqual(imessage_reader.decode_attributed_body(b""), "")
        self.assertEqual(imessage_reader.decode_attributed_body(b"no marker here"), "")


class TestSend(unittest.TestCase):
    def test_send_success(self):
        with patch("imessage_send.subprocess.run",
                   return_value=MagicMock(returncode=0, stderr="")) as run:
            self.assertTrue(imessage_send.send_imessage("+15551234567", "hi"))
            run.assert_called_once()

    def test_send_failure_returns_false(self):
        with patch("imessage_send.subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="boom")):
            self.assertFalse(imessage_send.send_imessage("+15551234567", "hi"))

    def test_no_handle(self):
        self.assertFalse(imessage_send.send_imessage("", "hi"))


if __name__ == "__main__":
    unittest.main()

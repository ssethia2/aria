"""Telegram photo support: extraction, download+describe composition, vision routing.
Offline (Telegram HTTP + LLM mocked). Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

import telegram_bot as tb


class TestExtractPhoto(unittest.TestCase):
    def test_picks_largest_photo_size(self):
        msg = {"photo": [{"file_id": "small"}, {"file_id": "medium"}, {"file_id": "LARGE"}]}
        self.assertEqual(tb._extract_photo(msg), ("LARGE", "image/jpeg"))

    def test_image_document_accepted(self):
        msg = {"document": {"file_id": "doc1", "mime_type": "image/png"}}
        self.assertEqual(tb._extract_photo(msg), ("doc1", "image/png"))

    def test_non_image_document_rejected(self):
        msg = {"document": {"file_id": "doc1", "mime_type": "application/pdf"}}
        self.assertEqual(tb._extract_photo(msg), (None, None))

    def test_plain_text_message(self):
        self.assertEqual(tb._extract_photo({"text": "hi"}), (None, None))


class TestSeePhoto(unittest.TestCase):
    def _telegram(self):
        get = MagicMock()
        get.side_effect = [
            MagicMock(json=lambda: {"result": {"file_path": "photos/x.jpg"}}),
            MagicMock(content=b"JPEGBYTES"),
        ]
        return get

    def test_caption_becomes_the_vision_question_and_composition(self):
        with patch.object(tb.requests, "get", self._telegram()), \
             patch("llm_router.describe_image", return_value="A receipt for $42 at Trader Joe's") as di:
            out = tb.see_photo("f1", "image/jpeg", "add this to my expenses")
        di.assert_called_once_with(b"JPEGBYTES", "image/jpeg", question="add this to my expenses")
        self.assertIn("A receipt for $42", out)
        self.assertIn("add this to my expenses", out)    # caption rides along for the brain

    def test_no_caption_asks_for_description(self):
        with patch.object(tb.requests, "get", self._telegram()), \
             patch("llm_router.describe_image", return_value="A whiteboard with a diagram") as di:
            out = tb.see_photo("f1", "image/jpeg", "")
        di.assert_called_once_with(b"JPEGBYTES", "image/jpeg", question=None)
        self.assertIn("whiteboard", out)


class TestDescribeImage(unittest.TestCase):
    def test_builds_data_url_and_uses_light_tier(self):
        import llm_router
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="a cat")
        with patch.object(llm_router, "get_llm", return_value=llm) as gl:
            out = llm_router.describe_image(b"\x89PNG", "image/png", question="what is it?")
        self.assertEqual(out, "a cat")
        gl.assert_called_once_with(tier="standard")   # real photos need real vision (Sonnet)
        blocks = llm.invoke.call_args[0][0][0].content
        self.assertIn("what is it?", blocks[0]["text"])
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()

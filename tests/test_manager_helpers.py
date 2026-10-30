"""Pure helper tests for the manager GUI (no QApplication required)."""

import time
import unittest

import blurb_manager as bm


class ManagerHelperTests(unittest.TestCase):
    def test_shorten(self):
        self.assertEqual(bm._shorten("hello", 10), "hello")
        self.assertEqual(bm._shorten("hello world", 8), "hello w…")

    def test_url_host(self):
        self.assertEqual(bm._url_host("https://example.com/api"), "example.com")
        self.assertEqual(bm._url_host("not a url"), "not a url")

    def test_status_title_uses_stage(self):
        self.assertEqual(bm._status_title("process", "downloading", {}), "Downloading audio")
        self.assertEqual(bm._status_title("process", "transcribing", {}), "Transcribing")
        self.assertEqual(bm._status_title("discover", "", {}), "Discovering")
        self.assertEqual(bm._status_title("process", "", {"stage": "finishing"}), "Saving")

    def test_error_hint(self):
        self.assertIn("API key", bm._error_hint("HTTP 401 Unauthorized") or "")
        self.assertIn("server", (bm._error_hint("Connection refused") or "").lower())
        self.assertIsNone(bm._error_hint("something odd"))

    def test_relative_time(self):
        self.assertEqual(bm._relative_time(None), "")
        self.assertTrue(bm._relative_time(time.time() - 30).endswith("s ago"))
        self.assertTrue(bm._relative_time(time.time() - 120).endswith("m ago"))

    def test_countdown(self):
        self.assertEqual(bm._format_countdown(30), "30s")
        self.assertEqual(bm._format_countdown(600), "10m")
        self.assertEqual(bm._format_countdown(7200), "2.0h")

    def test_cookie_words(self):
        self.assertEqual(
            bm._cookie_words({"state": "unset", "expires_at": None}),
            ("Not configured", bm.COLOR_SUBTLE),
        )
        text, color = bm._cookie_words({
            "state": "expired",
            "expires_at": time.time() - 86400,
        })
        self.assertIn("Expired", text)
        self.assertEqual(color, bm.COLOR_STOP)

    def test_cookie_issue_only_warns_when_needed(self):
        self.assertIsNone(bm._cookie_issue("YouTube", {"state": "ok", "expires_at": None}))
        self.assertIsNone(bm._cookie_issue("YouTube", {"state": "unset", "expires_at": None}))
        self.assertIn("missing", bm._cookie_issue("YouTube", {"state": "missing"}) or "")


if __name__ == "__main__":
    unittest.main()

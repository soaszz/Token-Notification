"""Notification transport, message, and workflow tests.

Run `python -m unittest discover -s tests -v` from the repo root.
"""

from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import notify  # noqa: E402

NOW = 1_800_000_000


class ConfirmedMessageTests(unittest.TestCase):
    def test_5hour_exact_wording(self):
        text = notify.confirmed_text("primary", 0, 1792515611)
        self.assertEqual(
            text,
            "Codex 5-Hour Limit Reset\n\n"
            "Codex has confirmed that your 5-hour usage window renewed.\n\n"
            "Current usage: 0%\n\n"
            "Next reported reset: 2026-10-21 01:00:11 +08:00 (Asia/Manila)",
        )

    def test_weekly_exact_wording(self):
        text = notify.confirmed_text("secondary", 12.5, 1792926804)
        self.assertEqual(
            text,
            "Codex Weekly Limit Reset\n\n"
            "Codex has confirmed that your weekly usage window renewed.\n\n"
            "Current usage: 12.5%\n\n"
            "Next reported reset: 2026-10-25 19:13:24 +08:00 (Asia/Manila)",
        )

    def test_never_claims_full_allowance(self):
        for used in (0, 5, 100):
            text = notify.confirmed_text("primary", used, NOW)
            self.assertNotIn("100%", text.replace(f"Current usage: {used}%", ""))
            self.assertNotIn("full", text.lower())
            self.assertNotIn("available", text.lower())


class TelegramSendTests(unittest.TestCase):
    class FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._payload

    TOKEN = "999999:SECRET-TOKEN-VALUE"

    def test_ok_response_passes(self):
        with mock.patch("urllib.request.urlopen", return_value=self.FakeResp(b'{"ok":true}')):
            notify.telegram_send(self.TOKEN, "123", "hello")

    def test_http_401_raises_redacted(self):
        err = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{self.TOKEN}/sendMessage",
            401, "Unauthorized", None, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(notify.TelegramError) as ctx:
                notify.telegram_send(self.TOKEN, "123", "hello")
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertNotIn(self.TOKEN, msg)

    def test_network_error_raises_redacted(self):
        err = urllib.error.URLError(f"failed for token {self.TOKEN}")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(notify.TelegramError) as ctx:
                notify.telegram_send(self.TOKEN, "123", "hello")
        self.assertNotIn(self.TOKEN, str(ctx.exception))
        self.assertIn("network error", str(ctx.exception))

    def test_ok_false_payload(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self.FakeResp(b'{"ok":false,"description":"chat not found"}')):
            with self.assertRaises(notify.TelegramError) as ctx:
                notify.telegram_send(self.TOKEN, "123", "hello")
        self.assertIn("chat not found", str(ctx.exception))

    def test_missing_token_and_chat(self):
        with self.assertRaises(notify.TelegramError):
            notify.telegram_send("", "123", "hello")
        with self.assertRaises(notify.TelegramError):
            notify.telegram_send(self.TOKEN, "", "hello")


class WorkflowFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (REPO / ".github" / "workflows" / "notify.yml").read_text(encoding="utf-8")

    def test_no_scheduled_cloud_notifications(self):
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("cron:", self.text)
        self.assertNotIn("notify.py --check", self.text)

    def test_manual_dispatch_only(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("Confirmation-only mode", self.text)

    def test_uses_secrets_not_hardcoded(self):
        self.assertIn("${{ secrets.TELEGRAM_BOT_TOKEN }}", self.text)
        self.assertIn("${{ secrets.TELEGRAM_CHAT_ID }}", self.text)
        self.assertNotIn("bot999", self.text)

    def test_least_privilege(self):
        self.assertIn("contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("pull-requests", self.text)

    def test_no_codex_credentials_anywhere(self):
        for forbidden in ("auth.json", "codex_app", "OPENAI_API_KEY", "chatgpt"):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
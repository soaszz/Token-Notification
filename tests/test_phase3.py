"""Phase 3 tests: run `python -m unittest discover -s tests -v` from repo root."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import github_sync  # noqa: E402
import notify  # noqa: E402
import state_store as ss  # noqa: E402

NOW = 1_800_000_000  # fixed clock for determinism
PAST_RESET = NOW - 3600
FUTURE_RESET = NOW + 3600


def make_state(checked_at: int, primary=None, secondary=None) -> dict:
    return {
        "schema": 1,
        "lastCheckedAt": checked_at,
        "lastError": None,
        "planType": "edu",
        "primary": primary,
        "secondary": secondary,
    }


def win(used=10, minutes=300, resets=FUTURE_RESET):
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets}


class MessageFormattingTests(unittest.TestCase):
    def test_scheduled_5hour_exact_wording(self):
        text = notify.scheduled_text("primary", 1792515611)
        self.assertIn("Codex 5-Hour Reset Time Reached", text)
        self.assertIn("Your previously reported Codex reset time has arrived.", text)
        self.assertIn("Open Codex to confirm your available usage.", text)
        self.assertIn("2026-10-21 01:00:11 +08:00 (Asia/Manila)", text)

    def test_confirmed_5hour_exact_wording(self):
        text = notify.confirmed_text("primary", 1792515611)
        self.assertIn("Codex 5-Hour Limit Reset", text)
        self.assertIn("Your Codex 5-hour usage window has reset.", text)
        self.assertIn("You can continue using Codex.", text)
        self.assertIn("2026-10-21 01:00:11 +08:00 (Asia/Manila)", text)

    def test_weekly_wording(self):
        sched = notify.scheduled_text("secondary", 1792926804)
        conf = notify.confirmed_text("secondary", 1792926804)
        self.assertIn("Codex Weekly Reset Time Reached", sched)
        self.assertIn("Your Codex weekly usage window has reset.", conf)
        self.assertIn("Codex Weekly Limit Reset", conf)
        self.assertIn("2026-10-25 19:13:24 +08:00 (Asia/Manila)", sched)

    def test_event_text_dispatch(self):
        e = {"kind": "sched", "window": "primary", "resetsAt": 1792515611}
        self.assertIn("Reset Time Reached", notify.event_text(e))
        e["kind"] = "conf"
        self.assertIn("Limit Reset", notify.event_text(e))


class DecideTests(unittest.TestCase):
    def test_future_reset_no_event(self):
        state = make_state(NOW - 60, primary=win(resets=FUTURE_RESET))
        events, hist = notify.decide(state, notify.empty_history(), NOW)
        self.assertEqual(events, [])
        self.assertEqual(hist["lastSeen"]["primary"], FUTURE_RESET)

    def test_pc_off_scheduled_then_dedup(self):
        # State last checked 1h before reset; reset has passed; now is later.
        state = make_state(PAST_RESET - 60, primary=win(resets=PAST_RESET))
        events, hist = notify.decide(state, notify.empty_history(), NOW)
        self.assertEqual([e["kind"] for e in events], ["sched"])
        self.assertEqual(events[0]["resetsAt"], PAST_RESET)
        # Second workflow run: at most once
        events2, hist2 = notify.decide(state, hist, NOW + 300)
        self.assertEqual(events2, [])

    def test_stale_state_no_scheduled(self):
        state = make_state(NOW - 30 * 3600, primary=win(resets=PAST_RESET))
        events, _ = notify.decide(state, notify.empty_history(), NOW)
        self.assertEqual(events, [])  # >24h old: too stale, no misleading push

    def test_roll_forward_confirmed_then_dedup(self):
        hist = notify.empty_history()
        hist["lastSeen"]["primary"] = PAST_RESET
        hist["sent"][f"sched:primary:{PAST_RESET}"] = NOW - 600  # scheduled already sent
        state = make_state(NOW - 120, primary=win(resets=FUTURE_RESET))  # fresh poll, new window
        events, new_hist = notify.decide(state, hist, NOW)
        self.assertEqual([(e["kind"], e["window"]) for e in events], [("conf", "primary")])
        self.assertEqual(events[0]["resetsAt"], PAST_RESET)  # refers to the old elapsed window
        events2, _ = notify.decide(state, new_hist, NOW + 300)
        self.assertEqual(events2, [])

    def test_confirmed_without_prior_scheduled(self):
        # PC on: tracker pushed new window before workflow saw the old one expire
        hist = notify.empty_history()
        hist["lastSeen"]["primary"] = PAST_RESET
        state = make_state(NOW - 60, primary=win(resets=FUTURE_RESET))
        events, _ = notify.decide(state, hist, NOW)
        self.assertEqual([e["kind"] for e in events], ["conf"])

    def test_confirmed_requires_fresh_data(self):
        hist = notify.empty_history()
        hist["lastSeen"]["primary"] = PAST_RESET
        state = make_state(NOW - 2 * 3600, primary=win(resets=FUTURE_RESET))
        events, _ = notify.decide(state, hist, NOW)
        self.assertEqual(events, [])

    def test_early_replacement_silent(self):
        # Backend replaced the window before the old one elapsed: no claims
        hist = notify.empty_history()
        hist["lastSeen"]["primary"] = FUTURE_RESET + 3600  # old value still in the future
        state = make_state(NOW - 60, primary=win(resets=FUTURE_RESET))
        events, new_hist = notify.decide(state, hist, NOW)
        self.assertEqual(events, [])
        self.assertEqual(new_hist["lastSeen"]["primary"], FUTURE_RESET)

    def test_both_windows_independent(self):
        state = make_state(
            NOW - 60,
            primary=win(resets=PAST_RESET),
            secondary={"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": PAST_RESET},
        )
        events, _ = notify.decide(state, notify.empty_history(), NOW)
        self.assertEqual(sorted(e["window"] for e in events), ["primary", "secondary"])
        self.assertTrue(all(e["kind"] == "sched" for e in events))

    def test_missing_windows_ok(self):
        state = make_state(NOW - 60, primary=None, secondary=None)
        events, _ = notify.decide(state, notify.empty_history(), NOW)
        self.assertEqual(events, [])

    def test_history_prune_old_entries(self):
        hist = notify.empty_history()
        hist["sent"]["sched:primary:1"] = NOW - 31 * 86400
        hist["sent"]["sched:primary:2"] = NOW - 10
        state = make_state(NOW - 60, primary=win(resets=FUTURE_RESET))
        _, new_hist = notify.decide(state, hist, NOW)
        self.assertNotIn("sched:primary:1", new_hist["sent"])
        self.assertIn("sched:primary:2", new_hist["sent"])


class ValidationTests(unittest.TestCase):
    def test_valid_state(self):
        self.assertEqual(notify.validate_state(make_state(NOW, primary=win())), [])

    def test_invalid_variants(self):
        self.assertTrue(notify.validate_state("nope"))
        self.assertTrue(notify.validate_state({}))  # missing lastCheckedAt/schema
        bad = make_state(NOW)
        bad["primary"] = {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": "soon"}
        self.assertTrue(notify.validate_state(bad))
        bad2 = make_state(NOW)
        bad2["secondary"] = "oops"
        self.assertTrue(notify.validate_state(bad2))


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


class BareRepoFixture(unittest.TestCase):
    """work repo + bare origin, mirroring the real git layout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_ph3_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        subprocess.run(["git", "init", "--bare", str(self.bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.work)], check=True, capture_output=True)
        for args in (["config", "user.name", "Test"],
                     ["config", "user.email", "t@example.com"],
                     ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", str(self.work), *args], check=True, capture_output=True)
        (self.work / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.work), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.work), "commit", "-m", "seed"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.work), "remote", "add", "origin", str(self.bare)],
                       check=True, capture_output=True)

    def push_state(self, state: dict) -> None:
        github_sync.push_files(
            {"state.json": (json.dumps(state) + "\n").encode()},
            repo=self.work, commit_msg="test state")

    def read_history(self) -> dict:
        raw = github_sync.read_branch_file(self.work, "notify_history.json")
        return notify.load_history(raw)


class RunCheckTests(BareRepoFixture):
    def _recorder(self, sent: list, fail: bool = False):
        def sender(token, chat_id, text):
            if fail:
                raise notify.TelegramError("boom")
            sent.append(text)
        return sender

    def test_no_state_on_branch_skips(self):
        sent: list = []
        code = notify.run_check(self.work, sender=self._recorder(sent), now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_pc_off_scheduled_send_and_dedup(self):
        self.push_state(make_state(PAST_RESET - 60, primary=win(resets=PAST_RESET)))
        sent: list = []
        code = notify.run_check(self.work, sender=self._recorder(sent), now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex 5-Hour Reset Time Reached", sent[0])
        # history persisted on branch -> second run sends nothing
        hist = self.read_history()
        self.assertIn(f"sched:primary:{PAST_RESET}", hist["sent"])
        sent2: list = []
        code2 = notify.run_check(self.work, sender=self._recorder(sent2), now=NOW + 300)
        self.assertEqual(code2, 0)
        self.assertEqual(sent2, [])

    def test_roll_forward_sends_confirmed(self):
        self.push_state(make_state(PAST_RESET - 60, primary=win(resets=PAST_RESET)))
        notify.run_check(self.work, sender=self._recorder([]), now=NOW)
        # PC comes back: tracker pushes new window (fresh)
        self.push_state(make_state(NOW - 120, primary=win(resets=FUTURE_RESET)))
        sent: list = []
        code = notify.run_check(self.work, sender=self._recorder(sent), now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex 5-Hour Limit Reset", sent[0])

    def test_invalid_state_skips_without_send(self):
        self.push_state({"schema": 1, "lastCheckedAt": "bad", "primary": None, "secondary": None})
        sent: list = []
        code = notify.run_check(self.work, sender=self._recorder(sent), now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertIsNone(github_sync.read_branch_file(self.work, "notify_history.json"))

    def test_send_failure_rolls_back_and_retries_next_run(self):
        self.push_state(make_state(PAST_RESET - 60, primary=win(resets=PAST_RESET)))
        code = notify.run_check(self.work, sender=self._recorder([], fail=True), now=NOW)
        self.assertEqual(code, 1)
        hist = self.read_history()
        self.assertNotIn(f"sched:primary:{PAST_RESET}", hist["sent"])  # rolled back
        sent: list = []
        code2 = notify.run_check(self.work, sender=self._recorder(sent), now=NOW + 60)
        self.assertEqual(code2, 0)
        self.assertEqual(len(sent), 1)  # retried, not lost

    def test_state_and_history_coexist_on_branch(self):
        self.push_state(make_state(NOW - 60, primary=win(resets=FUTURE_RESET)))
        notify.run_check(self.work, sender=self._recorder([]), now=NOW)
        # state.json untouched by history commit
        raw = github_sync.read_branch_file(self.work, "state.json")
        self.assertEqual(json.loads(raw)["schema"], 1)

    def test_lastseen_persisted_without_events_then_confirmed(self):
        # PC on: no notification due yet, but observed value must be remembered
        self.push_state(make_state(NOW - 60, primary=win(resets=FUTURE_RESET)))
        sent: list = []
        code = notify.run_check(self.work, sender=self._recorder(sent), now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        hist = self.read_history()
        self.assertEqual(hist["lastSeen"]["primary"], FUTURE_RESET)
        # Reset passes, tracker pushes a rolled window -> confirmed fires
        self.push_state(make_state(NOW + 7080, primary=win(resets=FUTURE_RESET + 86400)))
        sent2: list = []
        notify.run_check(self.work, sender=self._recorder(sent2), now=NOW + 7200)
        self.assertEqual(len(sent2), 1)
        self.assertIn("Codex 5-Hour Limit Reset", sent2[0])


class WorkflowFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (REPO / ".github" / "workflows" / "notify.yml").read_text(encoding="utf-8")

    def test_schedule_every_five_minutes(self):
        self.assertIn('cron: "*/5 * * * *"', self.text)

    def test_uses_secrets_not_hardcoded(self):
        self.assertIn("${{ secrets.TELEGRAM_BOT_TOKEN }}", self.text)
        self.assertIn("${{ secrets.TELEGRAM_CHAT_ID }}", self.text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN: ", self.text.replace(
            "TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", ""))

    def test_least_privilege_and_serialization(self):
        self.assertIn("contents: write", self.text)  # exactly what push needs
        self.assertNotIn("pull-requests", self.text)
        self.assertIn("concurrency:", self.text)
        self.assertIn("cancel-in-progress: false", self.text)

    def test_no_codex_credentials_anywhere(self):
        for forbidden in ("auth.json", "codex_app", "OPENAI_API_KEY", "chatgpt"):
            self.assertNotIn(forbidden, self.text)


class HistoryBytesRoundTripTests(unittest.TestCase):
    def test_roundtrip(self):
        hist = {"schema": 1, "lastSeen": {"primary": 5}, "sent": {"sched:primary:5": 9}}
        again = notify.load_history(notify.history_bytes(hist))
        self.assertEqual(again, hist)

    def test_corrupt_history_becomes_empty(self):
        self.assertEqual(notify.load_history(b"{broken"), notify.empty_history())
        self.assertEqual(notify.load_history(None), notify.empty_history())


if __name__ == "__main__":
    unittest.main(verbosity=2)

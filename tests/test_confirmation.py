"""Regression tests for confirmation-only Codex reset notifications.

Run `python -m unittest discover -s tests -v` from the repo root.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import codex_client  # noqa: E402
import main  # noqa: E402
import notify  # noqa: E402
import state_store as ss  # noqa: E402

NOW = 1_800_000_000
H5 = 300 * 60
WEEK = 10080 * 60


def obs(resets: int, used: float, observed: int, minutes: int) -> dict:
    return {"usedPercent": used, "windowDurationMins": minutes,
            "resetsAt": resets, "observedAt": observed}


def probe(primary=None, secondary=None, plan="edu", limit="codex") -> dict:
    return {"limitId": limit, "planType": plan,
            "primary": primary, "secondary": secondary}


def recorder(sent: list, fail: bool = False):
    def send(token, chat_id, text):
        if fail:
            raise notify.TelegramError("boom")
        sent.append(text)
    return send


class ConfirmationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_confirm_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / notify.VERIFIED_FILE

    def seed(self, primary=None, secondary=None, plan="edu", limit="codex",
             confirmed=None):
        notify._write_verified(self.path, {
            "schema": 1, "limitId": limit, "planType": plan,
            "windows": {k: v for k, v in
                        (("primary", primary), ("secondary", secondary)) if v},
            "confirmed": confirmed or {},
        })

    def run_probe(self, payload, sent, now=NOW, fail=False):
        return notify.process_local_observation(
            payload, tracker_dir=self.tmp, sender=recorder(sent, fail),
            now=now, logger=lambda _m: None)


class RealRenewalTests(ConfirmationBase):
    def test_real_verified_5hour_renewal(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 300)), sent)
        self.assertEqual([e["window"] for e in events], ["primary"])
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex 5-Hour Limit Reset", sent[0])
        self.assertIn("Codex has confirmed that your 5-hour usage window renewed.", sent[0])
        self.assertIn("Current usage: 0%", sent[0])

    def test_real_verified_weekly_renewal(self):
        old = NOW - 60
        self.seed(secondary=obs(old, 90, NOW - 120, 10080))
        sent: list = []
        events = self.run_probe(
            probe(secondary=obs(old + WEEK, 0, NOW, 10080)), sent)
        self.assertEqual([e["window"] for e in events], ["secondary"])
        self.assertIn("Codex Weekly Limit Reset", sent[0])
        self.assertIn("Codex has confirmed that your weekly usage window renewed.", sent[0])

    def test_both_windows_renew_independently(self):
        old = NOW - 60
        self.seed(primary=obs(old, 80, NOW - 120, 300),
                  secondary=obs(old, 70, NOW - 120, 10080))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + H5, 20, NOW, 300),
                  secondary=obs(old + WEEK, 20, NOW, 10080)), sent)
        self.assertEqual(sorted(e["window"] for e in events), ["primary", "secondary"])
        self.assertEqual(len(sent), 2)
        # Only primary renews on the next poll.
        sent2: list = []
        events2 = self.run_probe(
            probe(primary=obs(old + 2 * H5, 0, NOW + H5, 300),
                  secondary=obs(old + WEEK, 25, NOW + H5, 10080)),
            sent2, now=NOW + H5)
        self.assertEqual([e["window"] for e in events2], ["primary"])
        self.assertEqual(len(sent2), 1)


class NoFalsePositiveTests(ConfirmationBase):
    def test_reset_passes_without_fresh_codex_data(self):
        # A stored reset time passing is not evidence; a stale observation is
        # rejected even though its timestamps look like a rollover.
        prev = obs(NOW - 60, 100, NOW - 120, 300)
        cur = obs(NOW - 60 + H5, 0, NOW - notify.FRESH_SEC - 1, 300)
        ok, reason = notify.evaluate_renewal(prev, cur, NOW, "primary")
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_timestamp_changes_before_expiry(self):
        old = NOW + 3600
        self.seed(primary=obs(old, 100, NOW - 60, 300))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + 600, 0, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])
        self.assertEqual(notify.load_verified(self.path)["windows"]["primary"]["resetsAt"],
                         old + 600)  # silently re-baselined

    def test_usage_decreases_without_rollover(self):
        resets = NOW + 3600
        self.seed(primary=obs(resets, 100, NOW - 60, 300))
        sent: list = []
        events = self.run_probe(probe(primary=obs(resets, 20, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])

    def test_timestamp_change_without_usage_drop(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        events = self.run_probe(probe(primary=obs(old + H5, 100, NOW, 300)), sent)
        self.assertEqual(events, [])

    def test_tiny_usage_drop_is_not_enough(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        events = self.run_probe(probe(primary=obs(old + H5, 95, NOW, 300)), sent)
        self.assertEqual(events, [])

    def test_new_window_start_before_old_expiry_is_ambiguous(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        # Window start sits well before the old expiration -> schedule change.
        cur = obs(old + H5 - notify.WINDOW_START_TOLERANCE_SEC - 60, 0, NOW, 300)
        sent: list = []
        events = self.run_probe(probe(primary=cur), sent)
        self.assertEqual(events, [])

    def test_more_than_one_window_elapsed_is_ambiguous(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        cur = obs(old + 2 * H5, 0, NOW, 300)
        sent: list = []
        events = self.run_probe(probe(primary=cur), sent)
        self.assertEqual(events, [])

    def test_incomplete_usage_data(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        cur = obs(old + H5, 0, NOW, 300)
        del cur["usedPercent"]
        sent: list = []
        events = self.run_probe(probe(primary=cur), sent)
        self.assertEqual(events, [])

    def test_wrong_window_duration(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 10080)), sent)
        self.assertEqual(events, [])

    def test_plan_change_resets_baseline_silently(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300), plan="edu")
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 300), plan="plus"), sent)
        self.assertEqual(events, [])
        saved = notify.load_verified(self.path)
        self.assertEqual(saved["planType"], "plus")
        self.assertEqual(saved["windows"]["primary"]["resetsAt"], old + H5)
        self.assertEqual(saved["confirmed"], {})

    def test_no_misleading_messages(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        self.run_probe(probe(primary=obs(old + H5, 0, NOW, 300)), sent)
        self.assertEqual(len(sent), 1)
        for forbidden in ("scheduled", "reminder", "has arrived", "Reset Time Reached"):
            self.assertNotIn(forbidden, sent[0].lower())


class BaselineAndDedupTests(ConfirmationBase):
    def test_no_baseline_on_first_launch(self):
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(NOW + H5, 0, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])
        self.assertTrue(self.path.exists())
        self.assertEqual(notify.load_verified(self.path)["windows"]["primary"]["resetsAt"],
                         NOW + H5)

    def test_missing_state_silently_baselines(self):
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(NOW + H5, 0, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])

    def test_corrupt_state_silently_baselines(self):
        self.path.write_text("{broken", encoding="utf-8")
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(NOW + H5, 0, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])
        self.assertEqual(notify.load_verified(self.path)["windows"]["primary"]["resetsAt"],
                         NOW + H5)

    def test_duplicate_confirmation_sends_once(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        self.assertEqual(len(self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 300)), sent)), 1)
        # Same observation again (restart/retry) -> no duplicate.
        self.assertEqual(self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 300)), sent), [])
        self.assertEqual(len(sent), 1)
        confirmed = notify.load_verified(self.path)["confirmed"]
        self.assertIn(f"conf:primary:{old + H5}", confirmed)

    def test_restart_preserves_baseline_and_dedup(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        sent: list = []
        self.run_probe(probe(primary=obs(old + H5, 20, NOW, 300)), sent)
        # New process instance: reload from disk only.
        reloaded = notify.load_verified(self.path)
        self.assertIn(f"conf:primary:{old + H5}", reloaded["confirmed"])
        sent2: list = []
        events = notify.process_local_observation(
            probe(primary=obs(old + 2 * H5, 0, NOW + H5, 300)),
            tracker_dir=self.tmp, sender=recorder(sent2), now=NOW + H5,
            logger=lambda _m: None)
        self.assertEqual([e["window"] for e in events], ["primary"])
        self.assertEqual(len(sent2), 1)

    def test_send_failure_rolls_back_and_retries(self):
        old = NOW - 60
        self.seed(primary=obs(old, 100, NOW - 120, 300))
        self.assertEqual(self.run_probe(
            probe(primary=obs(old + H5, 0, NOW, 300)), [], fail=True), [])
        self.assertNotIn(f"conf:primary:{old + H5}",
                         notify.load_verified(self.path)["confirmed"])
        sent: list = []
        events = self.run_probe(probe(primary=obs(old + H5, 0, NOW, 300)), sent)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(sent), 1)


class SleepWakeAndFailureTests(ConfirmationBase):
    def test_pc_off_then_genuine_renewal_notifies(self):
        old = NOW - 2 * 3600  # PC slept before reset, woke after it
        self.seed(primary=obs(old, 100, old - 3600, 300))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + H5, 3, NOW, 300)), sent)
        self.assertEqual([e["window"] for e in events], ["primary"])

    def test_pc_off_long_gap_is_silent(self):
        old = NOW - 2 * H5
        self.seed(primary=obs(old, 100, old - 3600, 300))
        sent: list = []
        events = self.run_probe(
            probe(primary=obs(old + 3 * H5, 0, NOW, 300)), sent)
        self.assertEqual(events, [])
        self.assertEqual(sent, [])

    def test_network_failure_never_notifies(self):
        with mock.patch.object(main.codex_client, "read_rate_limits",
                               side_effect=codex_client.CodexProbeError("offline")), \
             mock.patch.object(main.notify, "process_local_observation") as confirm:
            _, outcome = main.poll_once()
        self.assertEqual(outcome, "failed")
        confirm.assert_not_called()

    def test_github_actions_stale_data_never_notifies(self):
        sent: list = []
        self.assertEqual(notify.run_check(
            REPO, sender=recorder(sent), now=NOW), 0)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
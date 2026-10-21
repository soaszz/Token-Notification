"""Phase 2 tests: run `python -m unittest discover -s tests -v` from repo root."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import codex_client  # noqa: E402
import github_sync  # noqa: E402
import state_store as ss  # noqa: E402
import main  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return (proc.stdout or "").strip()


class ManilaTimeTests(unittest.TestCase):
    def test_utc_and_manila_conversion(self):
        # 1792515611 verified in Phase 1
        self.assertEqual(ss.format_utc(1792515611), "2026-10-20 17:00:11 UTC")
        self.assertEqual(ss.format_manila(1792515611), "2026-10-21 01:00:11 +08:00")
        self.assertEqual(ss.format_manila(1792926804), "2026-10-25 19:13:24 +08:00")

    def test_none_renders_unknown(self):
        self.assertEqual(ss.format_manila(None), "unknown")
        self.assertEqual(ss.format_utc(None), "unknown")

    def test_manila_offset_is_utc8(self):
        from datetime import datetime, timedelta, timezone
        epoch = 1792515611
        manila = datetime.fromtimestamp(epoch, ss.MANILA)
        utc = datetime.fromtimestamp(epoch, timezone.utc)
        # Same instant, different offsets: both aware datetimes subtract to 0;
        # what must hold is the +08:00 offset and Manila wall-clock rendering.
        self.assertEqual(manila - utc, timedelta(0))
        self.assertEqual(manila.utcoffset(), timedelta(hours=8))
        self.assertEqual(manila.strftime("%Y-%m-%d %H:%M:%S"), "2026-10-21 01:00:11")
        self.assertEqual(utc.strftime("%Y-%m-%d %H:%M:%S"), "2026-10-20 17:00:11")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_state_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore_dir)

    def _restore_dir(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def test_build_state_normalizes_windows(self):
        probe = {
            "planType": "edu",
            "primary": {"usedPercent": 48, "windowDurationMins": 300, "resetsAt": 1792515611},
            "secondary": None,
        }
        state = ss.build_state(probe, checked_at=1000)
        self.assertEqual(state["schema"], 1)
        self.assertEqual(state["lastCheckedAt"], 1000)
        self.assertIsNone(state["lastError"])
        self.assertEqual(state["primary"]["windowDurationMins"], 300)
        self.assertEqual(state["secondary"],
                         {"usedPercent": None, "windowDurationMins": None, "resetsAt": None})

    def test_atomic_write_leaves_no_tmp_and_is_valid(self):
        state = ss.build_state({"planType": "edu"}, checked_at=1000)
        path = ss.write_state(state)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["lastCheckedAt"], 1000)

    def test_load_state_corrupt_returns_none(self):
        path = self.tmp / "state.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ss.load_state(path))

    def test_failure_state_preserves_windows(self):
        prev = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 5}},
            checked_at=1)
        state = ss.failure_state(prev, "boom", checked_at=2)
        self.assertEqual(state["lastError"], "boom")
        self.assertEqual(state["lastCheckedAt"], 2)
        self.assertEqual(state["primary"]["resetsAt"], 5)
        self.assertEqual(state["planType"], "edu")

    def test_failure_state_without_previous(self):
        state = ss.failure_state(None, "boom")
        self.assertEqual(state["lastError"], "boom")
        self.assertIsNone(state["primary"]["resetsAt"])

    def test_reset_signature_ignores_used_percent(self):
        base = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 222}},
            checked_at=1)
        more_used = json.loads(json.dumps(base))
        more_used["primary"]["usedPercent"] = 90
        more_used["lastCheckedAt"] = 999
        self.assertEqual(ss.reset_signature(base), ss.reset_signature(more_used))

    def test_reset_signature_detects_reset_change(self):
        base = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 222}},
            checked_at=1)
        rolled = json.loads(json.dumps(base))
        rolled["primary"]["resetsAt"] = 999
        self.assertNotEqual(ss.reset_signature(base), ss.reset_signature(rolled))

    def test_reset_signature_detects_missing_to_present(self):
        none_state = ss.build_state({"planType": "edu", "primary": None, "secondary": None})
        full = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": None})
        self.assertNotEqual(ss.reset_signature(none_state), ss.reset_signature(full))


class MissingDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_missing_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def test_status_renders_unknown_without_state(self):
        out = main.render_status(None)
        self.assertIn("No state yet", out)

    def test_status_renders_missing_windows(self):
        state = ss.build_state({"planType": None, "primary": None, "secondary": None},
                               checked_at=1000)
        out = main.render_status(state)
        self.assertIn("no reset timestamp (unknown)", out)
        self.assertIn("plan         : unknown", out)

    def test_maybe_sync_skips_when_no_reset_data(self):
        state = ss.build_state({"planType": "edu", "primary": None, "secondary": None})
        self.assertEqual(main.maybe_sync(state), "skipped-no-data")

    def test_maybe_sync_skips_unchanged_signature(self):
        state = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": None}, checked_at=1)
        sig = json.dumps(ss.reset_signature(state))
        ss.meta_path().write_text(
            json.dumps({"signature": sig, "lastSyncAt": 100}), encoding="utf-8")
        with mock.patch.object(github_sync, "sync_state") as sync_mock:
            self.assertEqual(main.maybe_sync(state, now=100 + main.HEARTBEAT_SEC - 1),
                             "skipped-unchanged")
        sync_mock.assert_not_called()


class HeartbeatTests(unittest.TestCase):
    """Heartbeat: refresh codex-state ~every 30 min when resets are unchanged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_hb_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)
        self.state = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": None}, checked_at=200)
        self.sig = json.dumps(ss.reset_signature(self.state))

    def _restore(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def _meta(self, **extra):
        ss.meta_path().write_text(json.dumps({"signature": self.sig, **extra}),
                                  encoding="utf-8")

    def test_no_sync_before_heartbeat_interval(self):
        self._meta(lastSyncAt=1000)
        with mock.patch.object(github_sync, "sync_state") as sync_mock:
            result = main.maybe_sync(self.state, now=1000 + main.HEARTBEAT_SEC - 1)
        self.assertEqual(result, "skipped-unchanged")
        sync_mock.assert_not_called()

    def test_heartbeat_syncs_after_interval(self):
        self._meta(lastSyncAt=1000)
        now = 1000 + main.HEARTBEAT_SEC
        with mock.patch.object(github_sync, "sync_state", return_value="pushed") as sync_mock:
            result = main.maybe_sync(self.state, now=now)
        self.assertEqual(result, "pushed")
        sync_mock.assert_called_once()
        meta = json.loads(ss.meta_path().read_text(encoding="utf-8"))
        self.assertEqual(meta["lastSyncAt"], now)  # UTC seconds

    def test_immediate_sync_on_timestamp_change(self):
        self._meta(lastSyncAt=999999)  # heartbeat not due
        changed = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 222},
             "secondary": None}, checked_at=200)
        with mock.patch.object(github_sync, "sync_state", return_value="pushed") as sync_mock:
            result = main.maybe_sync(changed, now=1000000)
        self.assertEqual(result, "pushed")
        sync_mock.assert_called_once()
        meta = json.loads(ss.meta_path().read_text(encoding="utf-8"))
        self.assertEqual(meta["signature"], json.dumps(ss.reset_signature(changed)))

    def test_missing_last_sync_treated_as_due(self):
        self._meta()  # legacy meta without lastSyncAt
        with mock.patch.object(github_sync, "sync_state", return_value="pushed"):
            self.assertEqual(main.maybe_sync(self.state, now=500), "pushed")

    def test_sync_failure_retries_next_poll(self):
        self._meta(lastSyncAt=1000)
        now = 1000 + main.HEARTBEAT_SEC
        with mock.patch.object(github_sync, "sync_state",
                               side_effect=github_sync.SyncError("boom")):
            self.assertEqual(main.maybe_sync(self.state, now=now), "error")
        meta = json.loads(ss.meta_path().read_text(encoding="utf-8"))
        self.assertEqual(meta["lastSyncAt"], 1000)  # failure leaves meta untouched
        with mock.patch.object(github_sync, "sync_state", return_value="pushed") as sync_mock:
            self.assertEqual(main.maybe_sync(self.state, now=now + 300), "pushed")
        sync_mock.assert_called_once()


class DuplicateInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_lock_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def test_second_instance_rejected(self):
        self.assertTrue(main.acquire_lock())
        self.assertFalse(main.acquire_lock())
        main.release_lock()
        self.assertTrue(main.acquire_lock())
        main.release_lock()

    def test_stale_lock_reclaimed(self):
        # PID 4 is System on Windows: always alive; use a definitely-dead PID instead.
        ss.lock_path().write_text("999999999", encoding="utf-8")
        self.assertTrue(main.acquire_lock())
        main.release_lock()


class LocalTrackerNotifyTests(unittest.TestCase):
    """Tracker loop must invoke the shared notify engine (PC-on delivery)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_ltn_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def _run_loop(self):
        try:
            main.cmd_run(interval=1, max_polls=1)
        finally:
            main.release_lock()

    def test_run_loop_calls_notify_after_ok_poll(self):
        state = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": None})
        with mock.patch.object(main, "poll_once", return_value=(state, "ok")), \
             mock.patch.object(main, "maybe_sync", return_value="skipped-unchanged"), \
             mock.patch.object(main.notify, "run_check", return_value=0) as run_mock:
            self._run_loop()
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args[0][0], ss.BASE_DIR)

    def test_notify_failure_does_not_kill_loop(self):
        state = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 111},
             "secondary": None})
        with mock.patch.object(main, "poll_once", return_value=(state, "ok")), \
             mock.patch.object(main, "maybe_sync", return_value="pushed"), \
             mock.patch.object(main.notify, "run_check", side_effect=RuntimeError("boom")):
            self._run_loop()  # must not raise
        # lock released in finally -> next instance can start
        self.assertTrue(main.acquire_lock())
        main.release_lock()

    def test_failed_probe_skips_notify(self):
        state = ss.failure_state(None, "probe down")
        with mock.patch.object(main, "poll_once", return_value=(state, "failed")), \
             mock.patch.object(main, "maybe_sync") as sync_mock, \
             mock.patch.object(main.notify, "run_check") as run_mock:
            self._run_loop()
        sync_mock.assert_not_called()
        run_mock.assert_not_called()


class LiveCodexProbeTests(unittest.TestCase):
    """Requires the real local Codex CLI + authenticated ChatGPT Edu session."""

    def test_app_server_round_trip(self):
        try:
            probe = codex_client.read_rate_limits(timeout=45)
        except codex_client.CodexProbeError as exc:
            self.fail(f"app-server probe failed: {exc}")
        self.assertEqual(probe["limitId"], "codex")
        self.assertIsInstance(probe["planType"], str)
        for name, minutes in (("primary", 300), ("secondary", 10080)):
            win = probe.get(name)
            self.assertIsInstance(win, dict, f"{name} window missing")
            self.assertEqual(win["windowDurationMins"], minutes, f"{name} window length")
            resets = win["resetsAt"]
            self.assertIsInstance(resets, int, f"{name} resetsAt not epoch int")
            self.assertGreater(resets, 1_600_000_000, f"{name} resetsAt implausible")
            self.assertIsInstance(win["usedPercent"], (int, float))


class GitHubSyncTests(unittest.TestCase):
    """Sync logic against a local bare 'origin' (no network, no credentials)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_sync_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        subprocess.run(["git", "init", "--bare", str(self.bare)], check=True,
                       capture_output=True)
        subprocess.run(["git", "init", str(self.work)], check=True, capture_output=True)
        for args in (["config", "user.name", "Test"],
                     ["config", "user.email", "t@example.com"],
                     ["config", "commit.gpgsign", "false"]):
            _git(self.work, *args)
        (self.work / "README.md").write_text("seed\n", encoding="utf-8")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "-m", "seed")
        _git(self.work, "remote", "add", "origin", str(self.bare))
        self.state = self.work / "state.json"
        self.state.write_text('{"schema": 1, "v": "A"}\n', encoding="utf-8")

    def _remote_files(self) -> list[str]:
        sha = github_sync.remote_branch_sha(self.work)
        self.assertIsNotNone(sha, "codex-state branch missing on origin")
        out = subprocess.run(
            ["git", "-C", str(self.work), "ls-tree", "-r", "--name-only", sha],
            capture_output=True, text=True, check=True).stdout
        return sorted(out.split())

    def test_push_creates_state_only_branch(self):
        result = github_sync.sync_state(self.state, repo=self.work)
        self.assertEqual(result, "pushed")
        self.assertEqual(self._remote_files(), ["state.json"])

    def test_unchanged_content_skips(self):
        github_sync.sync_state(self.state, repo=self.work)
        self.assertEqual(github_sync.sync_state(self.state, repo=self.work), "unchanged")

    def test_changed_resets_push(self):
        github_sync.sync_state(self.state, repo=self.work)
        self.state.write_text('{"schema": 1, "v": "B"}\n', encoding="utf-8")
        self.assertEqual(github_sync.sync_state(self.state, repo=self.work), "pushed")

    def test_missing_state_raises(self):
        with self.assertRaises(github_sync.SyncError):
            github_sync.sync_state(self.tmp / "nope.json", repo=self.work)

    def test_race_retry_preserves_unrelated_files(self):
        github_sync.sync_state(self.state, repo=self.work)
        stale_sha = github_sync.remote_branch_sha(self.work)

        # Remote advances via a second clone with an unrelated file.
        other = self.tmp / "other"
        subprocess.run(["git", "clone", str(self.bare), str(other)], check=True,
                       capture_output=True)
        for args in (["config", "user.name", "Test"],
                     ["config", "user.email", "t@example.com"]):
            _git(other, *args)
        _git(other, "checkout", "-B", "codex-state", "origin/codex-state")
        (other / "unrelated.txt").write_text("keep me\n", encoding="utf-8")
        _git(other, "add", "unrelated.txt")
        _git(other, "commit", "-m", "unrelated")
        _git(other, "push", "origin", "codex-state")

        # Local sync starts with a stale view of the remote tip on its first
        # attempt (simulated race), then retries against the real tip.
        self.state.write_text('{"schema": 1, "v": "C"}\n', encoding="utf-8")
        real = github_sync.remote_branch_sha
        calls = {"n": 0}

        def stale_once(repo, branch="codex-state", remote="origin"):
            calls["n"] += 1
            return stale_sha if calls["n"] <= 2 else real(repo, branch, remote)

        with mock.patch.object(github_sync, "remote_branch_sha", side_effect=stale_once):
            result = github_sync.sync_state(self.state, repo=self.work)

        self.assertEqual(result, "pushed")
        files = self._remote_files()
        self.assertIn("state.json", files)
        self.assertIn("unrelated.txt", files)  # unrelated change preserved
        sha = github_sync.remote_branch_sha(self.work)
        blob = subprocess.run(
            ["git", "-C", str(self.work), "show", f"{sha}:state.json"],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual(blob, '{"schema": 1, "v": "C"}\n')  # our fresh state won


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct_cli_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old = os.environ.get("CODEX_TRACKER_DIR")
        os.environ["CODEX_TRACKER_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CODEX_TRACKER_DIR", None)
        else:
            os.environ["CODEX_TRACKER_DIR"] = self._old

    def test_status_without_state_exits_1(self):
        self.assertEqual(main.main(["--status"]), 1)

    def test_status_with_state_shows_manila_time(self):
        state = ss.build_state(
            {"planType": "edu",
             "primary": {"usedPercent": 48, "windowDurationMins": 300,
                         "resetsAt": 1792515611},
             "secondary": {"usedPercent": 66, "windowDurationMins": 10080,
                           "resetsAt": 1792926804}},
            checked_at=1792500000)
        ss.write_state(state)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main.main(["--status"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("2026-10-21 01:00:11 +08:00", out)
        self.assertIn("2026-10-25 19:13:24 +08:00", out)
        self.assertIn("edu", out)


class NoWindowSubprocessTests(unittest.TestCase):
    """Regression: git/codex children must not flash console windows on Windows."""

    def _assert_hidden(self, kwargs):
        if os.name == "nt":
            self.assertEqual(
                kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW,
            )
            si = kwargs.get("startupinfo")
            self.assertIsNotNone(si)
            self.assertTrue(si.dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(si.wShowWindow, subprocess.SW_HIDE)
        else:
            self.assertEqual(kwargs, {})

    def test_helper_flags(self):
        self._assert_hidden(ss.subprocess_hidden())

    def test_git_helper_uses_hidden_flags(self):
        done = mock.Mock(returncode=0, stdout="main\n", stderr="")
        with mock.patch.object(github_sync.subprocess, "run", return_value=done) as run:
            github_sync._git(Path("."), "rev-parse", "--abbrev-ref", "HEAD")
        self._assert_hidden(run.call_args.kwargs)

    def test_git_hash_object_uses_hidden_flags(self):
        done = mock.Mock(returncode=0, stdout=b"abc\n", stderr=b"")
        with mock.patch.object(github_sync.subprocess, "run", return_value=done) as run:
            github_sync._hash_bytes(Path("."), b"x")
        self._assert_hidden(run.call_args.kwargs)

    def test_git_show_in_read_branch_file_uses_hidden_flags(self):
        done = mock.Mock(returncode=0, stdout=b"data\n", stderr=b"")
        with mock.patch.object(github_sync, "remote_branch_sha", return_value="abc123"), \
             mock.patch.object(github_sync, "_fetch_branch"), \
             mock.patch.object(github_sync.subprocess, "run", return_value=done) as run:
            out = github_sync.read_branch_file(Path("."), "state.json")
        self.assertEqual(out, b"data\n")
        self.assertIn("show", run.call_args.args[0])
        self._assert_hidden(run.call_args.kwargs)

    def test_codex_spawn_uses_hidden_flags(self):
        with mock.patch.object(codex_client.subprocess, "Popen") as popen:
            codex_client._spawn()
        self._assert_hidden(popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)

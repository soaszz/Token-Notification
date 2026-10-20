"""codex-reset-notifier — Phase 2 local tracker CLI.

  python main.py --status   show stored usage data
  python main.py --check    fetch fresh usage once (app-server) and sync
  python main.py --run      continuous background monitoring (default 5 min)

Windows automation: scripts/register_task.ps1 registers a hidden
ONLOGON Task Scheduler entry running this via pythonw.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import time
from pathlib import Path

import codex_client
import github_sync
import state_store as ss

DEFAULT_INTERVAL = 300  # 5 minutes
HEARTBEAT_SEC = 1800    # refresh codex-state ~every 30 min even when resets are unchanged
                        # (keeps lastCheckedAt fresh enough for PC-off reminders)
_LOG_CAP = 256 * 1024


def log(message: str) -> None:
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(ss.MANILA).strftime("%Y-%m-%d %H:%M:%S +08:00")
    path = ss.log_path()
    try:
        if path.exists() and path.stat().st_size > _LOG_CAP:
            path.replace(path.with_name(path.name + ".old"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- locking

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def acquire_lock() -> bool:
    path = ss.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder and _pid_alive(holder):
                return False
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    return False


def release_lock() -> None:
    try:
        ss.lock_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- core ops

def poll_once() -> tuple[dict, str]:
    """One probe + state write. Returns (state, 'ok'|'failed')."""
    try:
        probe = codex_client.read_rate_limits()
        state = ss.build_state(probe)
        outcome = "ok"
    except codex_client.CodexProbeError as exc:
        state = ss.failure_state(ss.load_state(), str(exc))
        outcome = "failed"
        log(f"probe failed: {exc}")
    ss.write_state(state)
    return state, outcome


def maybe_sync(state: dict, now: int | None = None) -> str:
    """Push to codex-state on reset change, or on the ~30-minute heartbeat.

    Heartbeat: even when reset timestamps are unchanged, push when the last
    successful sync is >= HEARTBEAT_SEC old so lastCheckedAt on the branch
    stays fresh enough for pre-reset reminders. Failed pushes leave the
    meta timestamp untouched, so the next poll retries. UTC seconds.
    """
    wins = (state.get("primary") or {}, state.get("secondary") or {})
    if wins[0].get("resetsAt") is None and wins[1].get("resetsAt") is None:
        return "skipped-no-data"

    now = int(now if now is not None else time.time())
    signature = json.dumps(ss.reset_signature(state))
    try:
        meta = json.loads(ss.meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    changed = meta.get("signature") != signature
    last_sync = meta.get("lastSyncAt")
    heartbeat_due = not isinstance(last_sync, int) or now - last_sync >= HEARTBEAT_SEC
    if not changed and not heartbeat_due:
        return "skipped-unchanged"

    try:
        result = github_sync.sync_state(ss.state_path())
    except github_sync.SyncError as exc:
        log(f"sync failed: {exc}")
        return "error"
    try:
        ss.meta_path().write_text(
            json.dumps({"signature": signature, "lastSyncAt": now}) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"meta write failed: {exc}")
    if result == "pushed":
        log("state.json pushed to codex-state" + (" (heartbeat)" if not changed else ""))
    return result


def render_status(state: dict | None) -> str:
    if not state:
        return "No state yet. Run: python main.py --check"
    now = int(time.time())

    def row(label: str, win: dict | None) -> str:
        win = win if isinstance(win, dict) else {}
        used = win.get("usedPercent")
        resets = win.get("resetsAt")
        if resets is None:
            return f"{label}: no reset timestamp (unknown)"
        phase = "scheduled" if resets > now else "time passed - awaiting confirm by next poll"
        return (
            f"{label}: {used if used is not None else '?'}% used | "
            f"resets {ss.format_manila(resets)} | {ss.format_utc(resets)} | {phase}"
        )

    lines = [
        "Codex Reset Tracker",
        f"last checked : {ss.format_manila(state.get('lastCheckedAt'))} "
        f"({ss.format_utc(state.get('lastCheckedAt'))})",
        f"plan         : {state.get('planType') or 'unknown'}",
        row("5-hour      ", state.get("primary")),
        row("weekly      ", state.get("secondary")),
        f"last error   : {state.get('lastError') or 'none'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_status() -> int:
    print(render_status(ss.load_state()))
    return 0 if ss.load_state() else 1


def cmd_check() -> int:
    state, outcome = poll_once()
    if outcome == "ok":
        sync_result = maybe_sync(state)
        print(f"sync: {sync_result}")
    print(render_status(state))
    return 0 if outcome == "ok" else 1


def cmd_run(interval: int, max_polls: int) -> int:
    if not acquire_lock():
        print("tracker already running (lock held)")
        return 0

    stop = False
    stop_event = _StopEvent()

    def _handle(signum, frame):
        nonlocal stop
        stop = True
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (OSError, ValueError):
            pass

    print("tracker started")
    try:
        polls = 0
        while not stop:
            state, outcome = poll_once()
            if outcome == "ok":
                sync_result = maybe_sync(state)
                if sync_result not in ("skipped-unchanged", "skipped-no-data"):
                    print(f"sync: {sync_result}")
            polls += 1
            if max_polls and polls >= max_polls:
                break
            # stop-aware sleep, 1s granularity
            for _ in range(interval):
                if stop or stop_event.wait(1):
                    break
    finally:
        release_lock()
        print("tracker stopped")
    return 0


class _StopEvent:
    def __init__(self):
        import threading
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def wait(self, timeout):
        return self._event.wait(timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="main.py", description="Codex reset tracker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="show stored usage data")
    group.add_argument("--check", action="store_true", help="fetch fresh usage once")
    group.add_argument("--run", action="store_true", help="continuous monitoring")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"poll interval seconds (default {DEFAULT_INTERVAL})")
    parser.add_argument("--max-polls", type=int, default=0,
                        help="stop after N polls (0 = never; testing aid)")
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.check:
        return cmd_check()
    return cmd_run(max(1, args.interval), max(0, args.max_polls))


if __name__ == "__main__":
    sys.exit(main())

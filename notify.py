"""Confirmation-only Telegram notifications for Codex usage-window renewals.

Notifications are sent ONLY when two fresh observations from the authenticated
local Codex app-server (`codex app-server` -> account/rateLimits/read) prove
that a usage window renewed. A changed `resetsAt` or a lower `usedPercent`
alone is never enough.

Scheduled reset pings, 1-hour reminders, and 15-minute reminders are disabled.
GitHub Actions cannot authenticate to the ChatGPT Edu Codex account, so the
cloud path never sends reset notifications; confirmation runs locally.

Commands:
  python notify.py --test     send a test message (no state read, no history)
  python notify.py --check    probe Codex locally, confirm renewals, notify

Secrets come ONLY from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment
variables. Tokens are never printed; exception text is scrubbed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import codex_client
import state_store as ss

VERIFIED_FILE = "verified_state.json"
FRESH_SEC = 3600                 # latest observation must be <=1h old
WINDOW_DURATION_MINS = {"primary": 300, "secondary": 10080}
WINDOW_START_TOLERANCE_SEC = 15 * 60   # clock/anchor drift allowance
MIN_USAGE_DROP_PCT = 10          # a real renewal must visibly free up usage
WINDOWS = ("primary", "secondary")

LABEL_HEAD = {"primary": "Codex 5-Hour", "secondary": "Codex Weekly"}
LABEL_BODY = {"primary": "5-hour", "secondary": "weekly"}


class TelegramError(RuntimeError):
    pass


# ---------------------------------------------------------------- messages

def confirmed_text(window: str, used_percent, resets_at: int) -> str:
    percent = f"{used_percent:g}"
    return (
        f"{LABEL_HEAD[window]} Limit Reset\n\n"
        f"Codex has confirmed that your {LABEL_BODY[window]} usage window renewed.\n\n"
        f"Current usage: {percent}%\n\n"
        f"Next reported reset: {ss.format_manila(resets_at)} (Asia/Manila)"
    )


def test_text() -> str:
    return "codex-reset-notifier test message\nTelegram integration is working."


# ---------------------------------------------------------------- telegram

def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def telegram_send(token: str, chat_id: str, text: str, timeout: float = 20.0) -> None:
    """Send one message. Raises TelegramError with token-free messages."""
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise TelegramError("TELEGRAM_CHAT_ID is not set")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"Telegram API error (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        raise TelegramError(f"network error reaching Telegram ({type(reason).__name__})") from None
    except TimeoutError:
        raise TelegramError("Telegram request timed out") from None
    except OSError as exc:
        raise TelegramError(_redact(f"transport error: {exc}", token)) from None
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise TelegramError("Telegram returned a non-JSON response") from None
    if not data.get("ok"):
        desc = str(data.get("description", "unknown error"))[:200]
        raise TelegramError(_redact(f"Telegram rejected the message: {desc}", token))


# ---------------------------------------------------------------- observations

def _window(raw, observed_at: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "usedPercent": raw.get("usedPercent"),
        "windowDurationMins": raw.get("windowDurationMins"),
        "resetsAt": raw.get("resetsAt"),
        "observedAt": observed_at,
    }


def normalize_probe(probe: dict, observed_at: int) -> dict | None:
    """Normalize a codex_client.read_rate_limits() payload for verification."""
    if not isinstance(probe, dict):
        return None
    return {
        "schema": 1,
        "limitId": probe.get("limitId"),
        "planType": probe.get("planType"),
        "windows": {
            "primary": _window(probe.get("primary"), observed_at),
            "secondary": _window(probe.get("secondary"), observed_at),
        },
    }


def empty_verified() -> dict:
    return {"schema": 1, "limitId": None, "planType": None,
            "windows": {}, "confirmed": {}}


def load_verified(path: Path) -> dict:
    """Load persisted verification state. Missing/corrupt -> empty baseline."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return empty_verified()
    if not isinstance(data, dict):
        return empty_verified()
    windows = data.get("windows")
    confirmed = data.get("confirmed")
    return {
        "schema": 1,
        "limitId": data.get("limitId"),
        "planType": data.get("planType"),
        "windows": windows if isinstance(windows, dict) else {},
        "confirmed": confirmed if isinstance(confirmed, dict) else {},
    }


def _valid_observation(obs, minutes: int) -> bool:
    if not isinstance(obs, dict):
        return False
    if not isinstance(obs.get("resetsAt"), int) or obs["resetsAt"] <= 0:
        return False
    if obs.get("windowDurationMins") != minutes:
        return False
    if not isinstance(obs.get("observedAt"), int) or obs["observedAt"] <= 0:
        return False
    return isinstance(obs.get("usedPercent"), (int, float))


def evaluate_renewal(prev: dict, cur: dict, now: int, window: str) -> tuple[bool, str]:
    """Strong-evidence renewal check for one window.

    Requires: a valid prior observation, a valid current observation, a later
    resetsAt, the prior reset already elapsed, the current observation inside
    its own window and fresh, a real usage drop, and a new window start
    consistent with the old window's expiry. Any ambiguity -> no confirmation.
    """
    minutes = WINDOW_DURATION_MINS[window]
    if not _valid_observation(prev, minutes):
        return False, "no valid prior observation"
    if not _valid_observation(cur, minutes):
        return False, "incomplete current observation"
    if cur["resetsAt"] <= prev["resetsAt"]:
        return False, "reset timestamp did not roll forward"
    if prev["resetsAt"] > now:
        return False, "previous window has not expired"
    duration = minutes * 60
    cur_start = cur["resetsAt"] - duration
    if not (cur_start <= now < cur["resetsAt"]):
        return False, "observation not inside the current window"
    if not (-60 <= now - cur["observedAt"] <= FRESH_SEC):
        return False, "observation is stale"
    if not (prev["resetsAt"] - duration <= prev["observedAt"] <= prev["resetsAt"] + 60):
        return False, "prior observation was not a clean baseline"
    if cur["usedPercent"] >= prev["usedPercent"]:
        return False, "usage did not decrease"
    if prev["usedPercent"] - cur["usedPercent"] < MIN_USAGE_DROP_PCT:
        return False, "usage drop too small to prove renewal"
    if cur_start < prev["resetsAt"] - WINDOW_START_TOLERANCE_SEC:
        return False, "new window started before the old one expired"
    if cur_start > prev["resetsAt"] + duration:
        return False, "more than one window elapsed; ambiguous history"
    return True, "confirmed renewal"


def _write_verified(path: Path, data: dict) -> None:
    payload = {
        "schema": 1,
        "limitId": data.get("limitId"),
        "planType": data.get("planType"),
        "windows": data.get("windows", {}),
        "confirmed": data.get("confirmed", {}),
    }
    ss.write_json_atomic(path, payload)


def process_local_observation(probe: dict, tracker_dir: Path | None = None,
                              sender=None, now: int | None = None,
                              logger=None) -> list[dict]:
    """Verify a fresh probe against the last verified observation, notify once.

    Silent baseline on first launch, restart, missing/corrupt state, plan or
    limit changes, incomplete data, and non-renewal transitions. Sends only
    confirmed renewals, deduplicated by window + resetsAt.
    """
    now = int(now if now is not None else time.time())
    log = logger or (lambda _msg: None)
    sender = sender or telegram_send
    path = Path(tracker_dir or ss.tracker_dir()) / VERIFIED_FILE

    latest = normalize_probe(probe, now)
    if latest is None:
        log("confirmation skipped: invalid probe payload")
        return []

    prev_state = load_verified(path)
    prev_windows = prev_state.get("windows", {})
    if prev_windows and (prev_state.get("planType") != latest.get("planType")
                         or prev_state.get("limitId") != latest.get("limitId")):
        # Different account/plan/window shape: never compare across it.
        log("confirmation baseline reset: plan or limit identifier changed")
        _write_verified(path, {"schema": 1, "limitId": latest.get("limitId"),
                               "planType": latest.get("planType"),
                               "windows": {}, "confirmed": prev_state.get("confirmed", {})})
        prev_state = load_verified(path)
        prev_windows = {}

    new_windows = dict(prev_windows)
    confirmed = dict(prev_state.get("confirmed", {}))
    events: list[dict] = []

    for win in WINDOWS:
        cur = latest["windows"].get(win)
        if cur is None:
            continue
        if not _valid_observation(cur, WINDOW_DURATION_MINS[win]):
            log(f"confirmation skipped ({win}): incomplete usage data")
            continue
        prev = prev_windows.get(win)
        key = f"conf:{win}:{cur['resetsAt']}"
        ok, reason = evaluate_renewal(prev, cur, now, win)
        if ok and key not in confirmed:
            confirmed[key] = now
            events.append({"window": win, "usedPercent": cur["usedPercent"],
                           "resetsAt": cur["resetsAt"], "key": key})
        elif ok:
            log(f"confirmation skipped ({win}): already notified for this window")
        elif prev is not None:
            log(f"confirmation skipped ({win}): {reason}")
        new_windows[win] = cur

    reserved = {"schema": 1, "limitId": latest.get("limitId"),
                "planType": latest.get("planType"),
                "windows": new_windows, "confirmed": confirmed}
    try:
        _write_verified(path, reserved)
    except OSError as exc:
        log(f"confirmation skipped: cannot persist verification state ({exc})")
        return []

    if not events:
        return []

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    failed: list[dict] = []
    for event in events:
        try:
            sender(token, chat_id, confirmed_text(event["window"],
                                                  event["usedPercent"],
                                                  event["resetsAt"]))
            log(f"confirmed reset sent ({event['window']}, resetsAt={event['resetsAt']})")
        except Exception as exc:
            log(f"confirmed reset send failed ({event['window']}): {exc}")
            failed.append(event)

    if failed:
        for event in failed:
            confirmed.pop(event["key"], None)
            if prev_windows.get(event["window"]) is not None:
                new_windows[event["window"]] = prev_windows[event["window"]]
            else:
                new_windows.pop(event["window"], None)
        try:
            _write_verified(path, reserved)
        except OSError as exc:
            log(f"confirmation rollback failed: {exc}")
        return []

    return events


# ---------------------------------------------------------------- run check

def run_check(repo=None, sender=None, branch: str = "codex-state",
              remote: str = "origin", now: int | None = None) -> int:
    """Cloud-safe compatibility entry point: never notifies from stored state.

    Reset confirmation requires a live authenticated Codex app-server probe on
    the local PC. GitHub Actions state/history can therefore never trigger a
    reset message.
    """
    print("skip: confirmation-only mode; reset notifications require local Codex verification")
    return 0


def run_local_check(sender=None, tracker_dir: Path | None = None,
                    now: int | None = None) -> int:
    try:
        probe = codex_client.read_rate_limits()
    except codex_client.CodexProbeError as exc:
        print(f"skip: Codex app-server probe failed: {exc}")
        return 1
    process_local_observation(probe, tracker_dir=tracker_dir, sender=sender,
                              now=now, logger=print)
    print("local verification complete")
    return 0


def run_test(sender=None) -> int:
    sender = sender or telegram_send
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    try:
        sender(token, chat_id, test_text())
    except TelegramError as exc:
        print(f"test send failed: {exc}")
        return 1
    print("test message sent")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notify.py", description="Codex reset Telegram notifier")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="send a test Telegram message")
    group.add_argument("--check", action="store_true",
                       help="verify local Codex usage and notify on a confirmed renewal")
    args = parser.parse_args(argv)
    if args.test:
        return run_test()
    return run_local_check()


if __name__ == "__main__":
    sys.exit(main())
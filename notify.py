"""Telegram notifications for Codex usage-window resets (Phase 3).

Reads state.json from the `codex-state` git branch (single source of truth,
works identically on the local PC and on a GitHub Actions runner), decides
whether a scheduled or confirmed reset notification is due, sends via the
Telegram Bot API (stdlib urllib only), and persists dedup history on the
same branch (notify_history.json) so each reset window notifies at most once.

Commands:
  python notify.py --test     send a test message (no state read, no history)
  python notify.py --check    evaluate resets, notify, update history

Secrets come ONLY from environment variables TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID (GitHub Actions Secrets in the cloud; user env vars locally).
Tokens are never printed; exception text is scrubbed before display.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import github_sync
import state_store as ss

HISTORY_FILE = "notify_history.json"
HISTORY_TTL_SEC = 30 * 86400  # drop sent-keys older than 30 days
CONFIRMED_FRESH_SEC = 3600    # usage data must be <=1h old to confirm a reset
SCHEDULED_MAX_AGE_SEC = 86400 # state older than 24h -> too stale to notify
REMINDER_MINUTES = (60,)      # 15-min reminder via CODEX_REMIND_15=1 (off by default)
# Skip a reminder once fewer seconds remain than the per-interval floor —
# protects against late/delayed runs sending a misleading "in ~1 hour" ping.
REMINDER_MIN_REMAINING_SEC = {60: 1200, 15: 180}
# PC-off reminders may rely on state older than CONFIRMED_FRESH_SEC when the
# stored reset timestamp is still in the future ("last verified schedule"
# wording), but never past SCHEDULED_MAX_AGE_SEC (24h): resets roll at most
# every few hours (5-hour) or week (weekly), so a >24h-old verified timestamp
# may describe a superseded schedule — a wrong reminder is worse than none.
# Same 24h cap as PC-off scheduled notifications.
WINDOWS = ("primary", "secondary")

LABEL_HEAD = {"primary": "Codex 5-Hour", "secondary": "Codex Weekly"}
LABEL_BODY = {"primary": "5-hour", "secondary": "weekly"}
REMIND_BODY = {
    "primary": "Your Codex usage window is scheduled to reset in approximately {when}.",
    "secondary": "Your weekly Codex allowance is scheduled to reset in approximately {when}.",
}


class TelegramError(RuntimeError):
    pass


# ---------------------------------------------------------------- messages

def scheduled_text(window: str, resets_at: int) -> str:
    head = (
        f"{LABEL_HEAD[window]} Reset Time Reached\n\n"
        "Your previously reported Codex reset time has arrived.\n\n"
        "Open Codex to confirm your available usage."
    )
    return head + f"\n\nPreviously reported reset: {ss.format_manila(resets_at)} (Asia/Manila)"


def confirmed_text(window: str, resets_at: int) -> str:
    head = (
        f"{LABEL_HEAD[window]} Limit Reset\n\n"
        f"Your Codex {LABEL_BODY[window]} usage window has reset.\n\n"
        "You can continue using Codex."
    )
    return head + f"\n\nReset time: {ss.format_manila(resets_at)} (Asia/Manila)"


def reminder_text(window: str, resets_at: int, minutes: int, used_percent=None,
                  verified_at: int | None = None) -> str:
    when = (f"{minutes // 60} hour" if minutes % 60 == 0
            else f"{minutes} minutes")
    if verified_at is not None:
        # PC-off / stale state: never claim the schedule is current Codex data.
        return (
            f"{LABEL_HEAD[window]} Reset Reminder\n\n"
            f"Your last reported Codex reset is scheduled in approximately {when}.\n\n"
            f"Scheduled reset: {ss.format_manila(resets_at)} (Asia/Manila)\n\n"
            f"This schedule was last verified at {ss.format_manila(verified_at)} (Asia/Manila)."
        )
    head = (
        f"{LABEL_HEAD[window]} Reset Reminder\n\n"
        f"{REMIND_BODY[window].format(when=when)}\n\n"
        f"Scheduled reset: {ss.format_manila(resets_at)} (Asia/Manila)"
    )
    if used_percent is not None:
        head += f"\nCurrent usage: {used_percent}%"
    return head


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


# ---------------------------------------------------------------- state/history

def validate_state(state) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["state is not a JSON object"]
    checked = state.get("lastCheckedAt")
    if not isinstance(checked, int) or checked < 1_600_000_000:
        errors.append("lastCheckedAt missing or invalid")
    if not isinstance(state.get("schema"), int):
        errors.append("schema missing")
    for win in WINDOWS:
        w = state.get(win)
        if w is None:
            continue
        if not isinstance(w, dict):
            errors.append(f"{win} window is not an object")
            continue
        resets = w.get("resetsAt")
        if resets is not None and (not isinstance(resets, int) or resets <= 0):
            errors.append(f"{win}.resetsAt invalid")
    return errors


def empty_history() -> dict:
    return {"schema": 1, "lastSeen": {}, "sent": {}}


def load_history(raw: bytes | None) -> dict:
    if not raw:
        return empty_history()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return empty_history()
    if not isinstance(data, dict):
        return empty_history()
    return {
        "schema": 1,
        "lastSeen": data.get("lastSeen") if isinstance(data.get("lastSeen"), dict) else {},
        "sent": data.get("sent") if isinstance(data.get("sent"), dict) else {},
    }


def history_bytes(history: dict) -> bytes:
    return (json.dumps(history, indent=2, sort_keys=True) + "\n").encode("utf-8")


# ---------------------------------------------------------------- decisions

def decide(state: dict, history: dict, now: int,
           reminder_minutes=REMINDER_MINUTES) -> tuple[list[dict], dict]:
    """Return (events, new_history). Pure function; no I/O.

    Per window:
    - reminder when a future resetsAt is inside the reminder window
      (remaining <= minutes*60 and >= the per-interval floor in
      REMINDER_MIN_REMAINING_SEC). Fresh data (<= CONFIRMED_FRESH_SEC) uses
      current-schedule wording + usage %; moderately stale data
      (<= SCHEDULED_MAX_AGE_SEC) still reminds with "last verified schedule"
      wording (verified_at), never past the 24h cap. Key
      reminder:{minutes}:{win}:{resetsAt} makes it at-most-once per reset
      window, independent of sched/conf.
    - first observation or unchanged resetsAt in the past (<= now) ->
      at most one "scheduled" notification (PC-off case), skipped when
      state data is older than SCHEDULED_MAX_AGE_SEC.
    - resetsAt rolled forward past an elapsed old value with fresh usage
      data (<= CONFIRMED_FRESH_SEC old) -> at most one "confirmed".
    - early backend window replacement (old value not yet elapsed) ->
      silent update of lastSeen.
    """
    events: list[dict] = []
    now = int(now)
    sent = {k: v for k, v in history.get("sent", {}).items()
            if isinstance(v, int) and now - v < HISTORY_TTL_SEC}
    last_seen = dict(history.get("lastSeen", {}))
    checked = state.get("lastCheckedAt", 0)

    def note(window: str, kind: str, resets_at: int, used_percent=None,
             verified_at: int | None = None) -> None:
        key = f"{kind}:{window}:{resets_at}"
        if key in sent:
            return
        sent[key] = now
        events.append({"window": window, "kind": kind, "resetsAt": resets_at,
                       "key": key, "usedPercent": used_percent, "verifiedAt": verified_at})

    fresh = isinstance(checked, int) and 0 <= now - checked <= CONFIRMED_FRESH_SEC
    remindable = isinstance(checked, int) and 0 <= now - checked <= SCHEDULED_MAX_AGE_SEC

    for win in WINDOWS:
        w = state.get(win)
        if not isinstance(w, dict):
            continue
        resets = w.get("resetsAt")
        if not isinstance(resets, int) or resets <= 0:
            continue
        used_percent = w.get("usedPercent")
        remaining = resets - now
        if remindable and 0 < remaining:
            for minutes in reminder_minutes:
                window_sec = minutes * 60
                floor = REMINDER_MIN_REMAINING_SEC.get(minutes, 300)
                if window_sec >= remaining >= floor:
                    note(win, f"reminder:{minutes}", resets,
                         used_percent if fresh else None,
                         None if fresh else checked)
        seen = last_seen.get(win)
        if seen is None or resets == seen:
            last_seen[win] = resets
            if (resets <= now
                    and f"sched:{win}:{resets}" not in sent
                    and isinstance(checked, int)
                    and 0 <= now - checked <= SCHEDULED_MAX_AGE_SEC):
                note(win, "sched", resets)
        elif resets > seen:
            old = seen
            last_seen[win] = resets
            if (old <= now
                    and isinstance(checked, int)
                    and 0 <= now - checked <= CONFIRMED_FRESH_SEC):
                note(win, "conf", old)
        else:
            last_seen[win] = resets  # backend re-anchored backwards: silent

    return events, {"schema": 1, "lastSeen": last_seen, "sent": sent}


def event_text(event: dict) -> str:
    kind = event["kind"]
    if kind.startswith("reminder:"):
        return reminder_text(event["window"], event["resetsAt"],
                             int(kind.split(":")[1]), event.get("usedPercent"),
                             event.get("verifiedAt"))
    if kind == "sched":
        return scheduled_text(event["window"], event["resetsAt"])
    return confirmed_text(event["window"], event["resetsAt"])


# ---------------------------------------------------------------- run check

def run_check(repo, sender=None, branch: str = github_sync.DEFAULT_BRANCH,
              remote: str = "origin", now: int | None = None) -> int:
    """Full cycle: read state+history from branch, notify, persist history.

    `sender(token, chat_id, text)` is injectable for tests; defaults to
    Telegram using environment secrets. Returns process exit code.
    """
    sender = sender or telegram_send
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    now = int(now if now is not None else time.time())

    try:
        state_raw = github_sync.read_branch_file(repo, github_sync.STATE_FILENAME, branch, remote)
        history_raw = github_sync.read_branch_file(repo, HISTORY_FILE, branch, remote)
    except github_sync.SyncError as exc:
        print(f"error: cannot read {branch}: {exc}")
        return 1

    if state_raw is None:
        print(f"skip: no {github_sync.STATE_FILENAME} on branch {branch} yet")
        return 0
    try:
        state = json.loads(state_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        print("skip: state.json is not valid JSON; not notifying")
        return 0
    errors = validate_state(state)
    if errors:
        print("skip: invalid state.json (" + "; ".join(errors) + "); not notifying")
        return 0

    history = load_history(history_raw)
    reminder_minutes = list(REMINDER_MINUTES)
    if os.environ.get("CODEX_REMIND_15", "").strip().lower() in ("1", "true"):
        reminder_minutes.append(15)
    events, new_history = decide(state, history, now, tuple(reminder_minutes))
    if not events:
        # No notification due, but remember newly observed reset values so a
        # later roll-forward can still be recognized as a confirmed reset.
        if new_history["lastSeen"] != history.get("lastSeen", {}):
            try:
                github_sync.push_files(
                    {HISTORY_FILE: history_bytes(new_history)},
                    repo=repo, branch=branch, remote=remote,
                    commit_msg="codex-state: update observed reset values",
                )
            except github_sync.SyncError as exc:
                print(f"warning: history update push failed: {exc}")
        print("no notifications due")
        return 0

    # Reserve dedup keys FIRST (commit history), then send. At-most-once
    # per window: a crash between reserve and send loses the message rather
    # than duplicating it; Telegram failures roll the reservation back.
    try:
        github_sync.push_files(
            {HISTORY_FILE: history_bytes(new_history)},
            repo=repo, branch=branch, remote=remote,
            commit_msg="codex-state: reserve reset notifications",
        )
    except github_sync.SyncError as exc:
        print(f"error: cannot persist notification history: {exc}")
        return 1

    failed_keys: list[str] = []
    for event in events:
        try:
            sender(token, chat_id, event_text(event))
            print(f"sent {event['kind']} ({event['window']}, resetsAt={event['resetsAt']})")
        except TelegramError as exc:
            print(f"send failed ({event['kind']} {event['window']}): {exc}")
            failed_keys.append(event["key"])

    if failed_keys:
        rollback = {k: v for k, v in new_history["sent"].items() if k not in failed_keys}
        try:
            github_sync.push_files(
                {HISTORY_FILE: history_bytes(
                    {"schema": 1, "lastSeen": new_history["lastSeen"], "sent": rollback})},
                repo=repo, branch=branch, remote=remote,
                commit_msg="codex-state: rollback failed notifications",
            )
        except github_sync.SyncError as exc:
            print(f"warning: rollback push failed: {exc}")
        return 1
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
                       help="evaluate codex-state branch and notify if due")
    args = parser.parse_args(argv)
    if args.test:
        return run_test()
    return run_check(ss.BASE_DIR)


if __name__ == "__main__":
    sys.exit(main())

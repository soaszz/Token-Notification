"""Local state file: non-sensitive usage metadata, UTC epochs, atomic writes.

Asia/Manila is UTC+8 with no DST since 1978, so a fixed offset is correct
(standard-library zoneinfo needs the third-party tzdata package on Windows;
zero dependencies wins).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1
MANILA = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent


def tracker_dir() -> Path:
    """Directory holding local tracker files (state, lock, meta, log)."""
    return Path(os.environ.get("CODEX_TRACKER_DIR") or BASE_DIR)


def state_path() -> Path:
    return tracker_dir() / "state.json"


def meta_path() -> Path:
    return tracker_dir() / ".sync_meta.json"


def lock_path() -> Path:
    return tracker_dir() / ".tracker.lock"


def log_path() -> Path:
    return tracker_dir() / "tracker.log"


def utc_now() -> int:
    return int(time.time())


def format_utc(epoch: int | None) -> str:
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_manila(epoch: int | None) -> str:
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, MANILA).strftime("%Y-%m-%d %H:%M:%S +08:00")


def _window(raw) -> dict:
    if not isinstance(raw, dict):
        return {"usedPercent": None, "windowDurationMins": None, "resetsAt": None}
    return {
        "usedPercent": raw.get("usedPercent"),
        "windowDurationMins": raw.get("windowDurationMins"),
        "resetsAt": raw.get("resetsAt"),
    }


def build_state(probe: dict, checked_at: int | None = None) -> dict:
    """Normalize a codex_client.read_rate_limits() payload into state.json."""
    return {
        "schema": SCHEMA,
        "lastCheckedAt": checked_at if checked_at is not None else utc_now(),
        "lastError": None,
        "planType": probe.get("planType"),
        "primary": _window(probe.get("primary")),
        "secondary": _window(probe.get("secondary")),
    }


def failure_state(previous: dict | None, error: str, checked_at: int | None = None) -> dict:
    """State after a failed probe: keep last known windows, record the error."""
    prev = previous if isinstance(previous, dict) else {}
    return {
        "schema": SCHEMA,
        "lastCheckedAt": checked_at if checked_at is not None else utc_now(),
        "lastError": str(error)[:300],
        "planType": prev.get("planType"),
        "primary": _window(prev.get("primary")),
        "secondary": _window(prev.get("secondary")),
    }


def load_state(path: Path | None = None) -> dict | None:
    p = path or state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(state: dict, path: Path | None = None) -> Path:
    """Atomic write: temp file in same dir, flush, os.replace."""
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return p


def reset_signature(state: dict | None) -> tuple:
    """Fields whose change justifies a GitHub upload.

    usedPercent and lastCheckedAt deliberately excluded: they change every
    poll and must not trigger commits.
    """
    if not state:
        return ("missing",)

    def w(name: str):
        win = state.get(name)
        if not isinstance(win, dict):
            return (None, None)
        return (win.get("resetsAt"), win.get("windowDurationMins"))

    return (
        state.get("planType"),
        ("primary",) + w("primary"),
        ("secondary",) + w("secondary"),
    )

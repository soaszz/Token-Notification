"""Read Codex usage from the official local app-server interface.

Verified in Phase 1 (docs/PHASE1_FEASIBILITY_AUDIT.md): `codex app-server`
JSON-RPC method `account/rateLimits/read` returns ChatGPT Edu rate-limit
windows with machine-readable UTC epoch reset timestamps. The CLI uses its
own stored ChatGPT auth (~/.codex/auth.json); no credentials are handled,
read, or logged here.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from state_store import subprocess_hidden


class CodexProbeError(RuntimeError):
    """app-server unreachable, protocol failure, or malformed response."""


def _spawn():
    return subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_hidden(),
    )


def read_rate_limits(timeout: float = 45.0) -> dict:
    """Return the account/rateLimits/read payload (redacted).

    Shape (official docs, developers.openai.com/codex/app-server):
      {limitId, planType, primary, secondary, rateLimitReachedType, ...}
    each window: {usedPercent, windowDurationMins, resetsAt}
    resetsAt is Unix seconds UTC; null when the backend reports none.
    """
    proc = _spawn()
    lines: queue.Queue = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line.strip())
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(obj: dict) -> None:
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexProbeError(f"app-server pipe closed: {exc}") from exc

    def wait(msg_id: int) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                raise CodexProbeError("app-server exited before responding")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                return msg
        raise CodexProbeError(f"timed out waiting for response id={msg_id}")

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-reset-tracker",
                    "title": "codex-reset-notifier phase 2 tracker",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": False, "requestAttestation": False},
            },
        })
        init = wait(1)
        if "error" in init:
            raise CodexProbeError(f"initialize failed: {init['error']}")

        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {"excludeResetCreditDetails": True},
        })
        resp = wait(2)
        if "error" in resp:
            raise CodexProbeError(f"rateLimits/read failed: {resp['error']}")
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stdin):
            try:
                stream.close()
            except OSError:
                pass

    result = resp.get("result")
    if not isinstance(result, dict):
        raise CodexProbeError("rateLimits/read returned no result object")
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        raise CodexProbeError("rateLimits/read returned no rateLimits object")

    def window(raw) -> dict | None:
        if not isinstance(raw, dict):
            return None
        return {
            "usedPercent": raw.get("usedPercent"),
            "windowDurationMins": raw.get("windowDurationMins"),
            "resetsAt": raw.get("resetsAt"),
        }

    return {
        "limitId": limits.get("limitId"),
        "planType": limits.get("planType"),
        "primary": window(limits.get("primary")),
        "secondary": window(limits.get("secondary")),
        "rateLimitReachedType": limits.get("rateLimitReachedType"),
        "ordinaryUsageAllowed": result.get("ordinaryUsageAllowed"),
    }

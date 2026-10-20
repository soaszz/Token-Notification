# Phase 1: Automatic Codex Reset Detection Feasibility Audit

Audit date: 2026-10-20 (Asia/Manila, UTC+8)
Scope: read-only environment inspection and supported-interface verification. No notification system built, no credentials read, nothing pushed to GitHub.

Label key:

- VERIFIED: confirmed by a successful test in this audit or authoritative OpenAI documentation.
- UNVERIFIED: plausible but not tested or not documented sufficiently.
- UNSUPPORTED: no supported method established.
- BLOCKED: requires an unavailable dependency, permission, or user action.

---

## 1. Environment information

| Item | Value | Label |
|---|---|---|
| OS | Windows 11 (win32) | VERIFIED (`Get-Command`, PowerShell 5.1) |
| Python | 3.13.14 (`python`), 3.13.0 (`py`) | VERIFIED |
| Codex CLI | `codex-cli 0.163.0`, `codex.exe` at `%LOCALAPPDATA%\Programs\...` | VERIFIED (`codex --version`) |
| Codex home | `C:\Users\mypc\.codex` | VERIFIED (`codex doctor --json`) |
| Auth mode | `chatgpt` ("stored ChatGPT tokens": true, "stored API key": false) | VERIFIED (`codex doctor --json`) |
| Plan type reported by backend | `edu` | VERIFIED (`account/rateLimits/read` live response, `planType`) |
| Desktop app | Installed, version 26.1013.4894.0, not running | VERIFIED (`codex doctor --json`) |
| Default model | `gpt-6.1-sol` | VERIFIED (`config.toml`) |

No credentials were printed, copied, or logged during this audit. `auth.json` was never opened; its presence and mode were confirmed only through `codex doctor --json` (redacted output).

## 2. Codex CLI detection results

| Question | Result | Label |
|---|---|---|
| Codex CLI installed | YES, `codex-cli 0.163.0` | VERIFIED |
| CLI exposes usage limits and reset timestamps | YES, through the app-server JSON-RPC interface (`account/rateLimits/read`), which returns `usedPercent`, `windowDurationMins`, and `resetsAt` (Unix seconds) | VERIFIED (documented + live test) |
| `/status` provides 5-hour and weekly usage | YES. Official docs: `/status` — "Show the chat ID, context usage, and rate limits." GitHub issues confirm it renders `5h limit` and `Weekly limit` with reset times | VERIFIED (official slash-commands doc) |
| `/status` machine-readable | NO. `/status` is an interactive TUI slash command only. No `codex status` subcommand exists in 0.163.0 (`codex --help`, `codex login --help`). Feature request for `codex status --json` is still open (openai/codex#20310) | VERIFIED (absence tested) |
| Usage via TUI status line tokens | Percentages only (`five-hour-limit`, `weekly-limit`). Reset-time tokens not present as of 0.133.0 per openai/codex#24080; still requested in open issues | UNVERIFIED for 0.163.0 (not tested) |
| Official docs: authenticated endpoint for ChatGPT Edu Codex usage | No public HTTP/REST endpoint documented. The documented programmatic surface is the local app-server JSON-RPC method `account/rateLimits/read` | VERIFIED (app-server docs), REST endpoint UNSUPPORTED |

Notes:

- The official Codex GitHub Action (`openai/codex-action@v1`) authenticates with `OPENAI_API_KEY` (API billing) — it is not a path to ChatGPT Edu Codex usage. VERIFIED (official docs).
- `codex doctor --json` gives health/auth/config only, no usage data. VERIFIED.

## 3. Available usage-information sources

| Source | Machine-readable | Reset timestamps | Credentials needed | Label |
|---|---|---|---|---|
| `codex app-server` JSON-RPC `account/rateLimits/read` | YES (JSON) | YES: `primary.resetsAt`, `secondary.resetsAt` (Unix seconds, UTC) | Local `~/.codex/auth.json` (ChatGPT session) | VERIFIED (documented at developers.openai.com/codex/app-server § "Rate limits (ChatGPT)" and live-tested) |
| `account/rateLimits/updated` server notification | YES (push) | Emits on change | same | VERIFIED (documented) |
| TUI `/status` | No (human text) | Shown as formatted local time | interactive session | VERIFIED documented; not machine-consumable |
| TUI status line tokens | Partial (percentages) | No reset-time token established | config only | UNVERIFIED |
| Web dashboard `https://chatgpt.com/codex/settings/usage` | No | Yes (manual view) | Browser session | VERIFIED exists (referenced in official-linked issues); not automated |
| OpenAI API usage endpoints (`/v1/usage` etc.) | Yes | N/A | API key | UNSUPPORTED for this purpose — measures API billing, not ChatGPT Edu Codex quota (explicitly out of scope) |

Live test performed (read-only, no Codex prompt sent, zero usage consumed):

```
method: account/rateLimits/read
result (redacted):
{
  "limitId": "codex",
  "planType": "edu",
  "primary":   { "usedPercent": 48, "windowDurationMins": 300,   "resetsAt": 1792515611 },
  "secondary": { "usedPercent": 66, "windowDurationMins": 10080, "resetsAt": 1792926804 },
  "rateLimitReachedType": null,
  "ordinaryUsageAllowed": true
}
```

Test procedure: spawned `codex app-server --stdio`, sent `initialize` + `account/rateLimits/read` (JSON-RPC over stdio), parsed response, killed process. `accountId` field was withheld from output. No auth file touched.

## 4. Five-hour reset detection feasibility

- Remaining usage percentage: YES — `primary.usedPercent` (48% used at test time). VERIFIED.
- Actual reset timestamp: YES — `primary.resetsAt = 1792515611` → 2026-10-20 17:00:11 UTC → 2026-10-21 01:00:11 Asia/Manila. Backend-owned Unix seconds; no local arithmetic. VERIFIED.
- Window length: `primary.windowDurationMins = 300` (5 hours). VERIFIED.
- Reset-occurred detection: available without the dashboard. Method: after `resetsAt` passes, re-read `account/rateLimits/read`; a confirmed reset shows `usedPercent` drop and/or a new future `resetsAt`. Push variant: `account/rateLimits/updated` notification. VERIFIED (method documented and read path tested).
- Important distinction: `resetsAt` passing is only a **scheduled** reset. Confirmed reset requires a post-reset read. Backend can also replace a window mid-cycle (hard resets, window re-anchoring observed in openai/codex#9508), so never compute reset = now + 5h. VERIFIED (documented behavior in GitHub issue discussion).

## 5. Weekly reset detection feasibility

- Remaining usage percentage: YES — `secondary.usedPercent` (66% at test time). VERIFIED.
- Actual reset timestamp: YES — `secondary.resetsAt = 1792926804` → 2026-10-25 11:13:24 UTC → 2026-10-25 19:13:24 Asia/Manila. VERIFIED.
- Window length: `secondary.windowDurationMins = 10080` (7 days). VERIFIED.
- Weekly reset date must never be invented: window is backend-anchored (tied to account/window history, can be replaced — see openai/codex#9508). Always read `resetsAt`. VERIFIED.
- Note: in some account states `secondary` can be `null` (e.g., weekly-only enforcement periods). Handle null. UNVERIFIED for this account (currently non-null).

## 6. ChatGPT Edu compatibility

- `planType: "edu"` returned by the backend → ChatGPT Edu detected. VERIFIED.
- Edu accounts work through Sign in with ChatGPT (`auth mode: chatgpt`) and the local app-server rate-limit read. VERIFIED (live test on this account).
- No Edu-specific restrictions found on reading own usage. VERIFIED (single test).
- Reset behavior may be workspace-governed (Edu/Work admins can enforce limits); admin-side configuration was not inspected. UNVERIFIED.

## 7. GitHub Actions compatibility

### A. Fully cloud-based automatic tracking

- Technical feasibility: NOT ESTABLISHED. GitHub Actions would need to call ChatGPT/Codex backend usage APIs with a ChatGPT session. No official documented HTTP endpoint for ChatGPT Edu Codex usage exists (only local app-server JSON-RPC). Official Codex GitHub Action uses `OPENAI_API_KEY` (API billing) — wrong quota. UNSUPPORTED.
- Required authentication: ChatGPT session tokens (`~/.codex/auth.json`) or browser cookies. Putting these in GitHub Secrets = uploading ChatGPT session material to CI. Rejected under security rules. BLOCKED.
- Reliability: N/A. Works while PC off: theoretically yes, but auth path forbidden. Edu support: no supported cloud path. Label: UNSUPPORTED.

### B. Local tracking + cloud-scheduled notifications (recommended)

- Local Windows scheduled task runs a small probe (the Phase-1-tested `account/rateLimits/read` script) every N minutes, writes `state.json` (UTC epoch `resetsAt`, `usedPercent`, `capturedAt`, `planType`) and pushes it to the repo (or a private gist).
- GitHub Actions cron reads `state.json`, compares `resetsAt` (UTC) to current UTC, sends Telegram when passed, then waits for the next local poll to confirm the actual reset.
- Technical feasibility: every component individually VERIFIED (local read tested; git push and Actions cron are standard). End-to-end integration: UNVERIFIED (Phase 2).
- Required authentication: local ChatGPT session (stays local), GitHub token for push (repo-scoped, no ChatGPT material), Telegram bot token in Actions secrets.
- Security: no ChatGPT credentials leave the PC. VERIFIED by design.
- Reliability: notifications fire while PC is off (state already in cloud). Freshness limited to last local poll. Confirmation of reset requires one poll after PC turns on again.
- Edu: supported (local read is account-agnostic). Label: CONDITIONAL — works if a poll happened before shutdown; stale-state detection needed (workflow must warn if `capturedAt` older than X).

### C. Manual reset timestamp configuration + cloud notifications

- Technical feasibility: trivially yes (Actions cron + timestamp comparison). VERIFIED (mechanism; not built).
- Authentication: Telegram secret only.
- Reliability: high for firing; detection is manual and reset windows can be replaced by the backend, so timestamps can be wrong. UNVERIFIED as a workflow; Edu: irrelevant (no account access needed).
- Works while PC off: YES. Label: CONDITIONAL (fires on time, correctness not guaranteed).

Verdict on GitHub Actions: hybrid B is the only path that is both automatic and PC-off-capable for ChatGPT Edu. Pure cloud (A) is UNSUPPORTED/BLOCKED on credentials.

## 8. Security risks

| Risk | Assessment |
|---|---|
| Extracting tokens from `auth.json` | Rejected. Not done; `auth.json` never read. Probe relies on CLI using its own stored auth. |
| Browser cookies | Rejected. Not read. |
| Reverse-engineering private endpoints | Rejected. Only documented app-server JSON-RPC used. |
| Committing secrets to GitHub | Must never include `auth.json`, cookies, Telegram bot token in committed files. Telegram token → GitHub Actions secrets only. |
| Uploading ChatGPT session to Actions | Rejected; this is why option A is BLOCKED. |
| Credential exposure in logs | Probe prints only limit fields; `accountId` withheld. Keep this behavior. |
| Rate-limit abuse | Probe is a read of existing usage; no prompts sent, no window manipulation. Poll interval ≥ 60 s recommended to avoid hammering backend (ponytail: one poll, not ten). |
| Backend window replacement | Never treat a passed timestamp as proof of renewal; always confirm by re-read. |

## 9. Recommended architecture

Phase 2 shape (not built in this phase):

1. **Local probe** (Windows Task Scheduler, every 2–5 min while PC on): spawn `codex app-server`, `initialize` → `account/rateLimits/read` → write `state.json`:
   `{ "captured_at": <unix>, "plan_type": "edu", "primary": {used_percent, window_duration_mins, resets_at}, "secondary": {...} }`
2. **State push**: git commit/push of `state.json` only (no secrets). Skip push when unchanged.
3. **GitHub Actions cron** (`schedule: */10 * * * *`): read `state.json` (UTC), if `resets_at` ≤ now → Telegram message with local-time conversion (UTC → Asia/Manila), mark notified in workflow-safe state (e.g., issue comment / committed flag / Actions cache — decide in Phase 2).
4. **Confirmation**: notification wording distinguishes `scheduled` vs `confirmed` — message 1 = "scheduled reset time reached (pending confirmation)", message 2 after next local poll shows `used_percent` drop = "confirmed reset".
5. All internal math in UTC epoch; Asia/Manila (UTC+8) only at display layer.

Why not GitHub-hosted detection: no supported ChatGPT Edu usage API; credential upload forbidden (§7A).

## 10. Remaining blockers

1. BLOCKED: cloud-side automatic detection — no official ChatGPT Edu Codex usage endpoint; would require uploading ChatGPT session credentials (rejected).
2. UNVERIFIED: end-to-end hybrid pipeline (local push → Actions cron → Telegram) — components exist, integration untested. Phase 2.
3. UNVERIFIED: status-line reset-time tokens in 0.163.0 — irrelevant for the plan (app-server covers it).
4. UNVERIFIED: Telegram delivery and Actions secret setup — Phase 2.
5. UNVERIFIED: how long backend retains/replaces `resetsAt` during hard resets; needs observation over time.
6. BLOCKED (action required later): Telegram bot token + chat ID creation by user before Phase 2 notification testing.
7. UNVERIFIED: whether `secondary` can be `null` on this Edu account during enforcement changes; code must handle null regardless.

## 11. Exact next steps

1. Phase 2a: build the local probe script (reuse the audited probe: `codex app-server --stdio` + `account/rateLimits/read`, redacted output) writing `state.json` with UTC epochs.
2. Add Windows Task Scheduler entry (2–5 min interval) running the probe; verify `state.json` updates over 24 h.
3. Commit `state.json` (no secrets) and confirm push works.
4. Add GitHub Actions cron workflow comparing `resets_at` (UTC) to `now`; Telegram send behind secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
5. Implement scheduled/confirmed two-stage notification logic; timestamps rendered in Asia/Manila.
6. Manual end-to-end test with a fake near-term `resets_at` in a test branch before relying on it.
7. Never: upload `auth.json`, compute `now + 5h`, or claim reset without a confirming re-read.

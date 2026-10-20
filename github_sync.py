"""Upload snapshot files (state.json, notify_history.json) to codex-state.

Design notes (ponytail: conflict-proof by construction):
- Each push builds a commit whose tree = parent tree + the provided files
  swapped in (single-file tree when the branch doesn't exist yet), so it can
  never carry master changes, clobber unrelated files on the state branch,
  or conflict on anything except the files being updated. The local tracker
  swaps only state.json; the notification workflow swaps only
  notify_history.json — they cannot overwrite each other's data.
- Only `refs/heads/codex-state` is ever pushed. Master and the working tree
  are untouched; no per-poll commits (callers gate on content).
- Auth reuses the repo's existing git credentials (HTTPS remote -> Git
  Credential Manager; GITHUB_TOKEN in Actions). Upgrade path: signed
  commits / gist storage if branch pushes ever get rejected by policy.
- Race handling: if the remote branch advanced between read and push, the
  rejected push is retried with the fresh remote tip as parent (up to
  `attempts`), re-checking for identical content each round.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from state_store import BASE_DIR

STATE_FILENAME = "state.json"
DEFAULT_BRANCH = "codex-state"


class SyncError(RuntimeError):
    pass


def _git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise SyncError(f"git {' '.join(args)} failed: {tail}")
    return proc


def _is_auth_error(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in (
        "authentication failed",
        "could not read username",
        "terminal prompts disabled",
        "permission denied",
        "invalid username or password",
    ))


def remote_branch_sha(repo: Path, branch: str = DEFAULT_BRANCH, remote: str = "origin") -> str | None:
    proc = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}", check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if _is_auth_error(err):
            raise SyncError(f"git ls-remote auth failed: {err[-300:]}")
        # Network failure is not the same as "branch missing".
        raise SyncError(f"git ls-remote failed: {err[-300:]}")
    line = (proc.stdout or "").strip()
    return line.split()[0] if line else None


def _file_blob_at(repo: Path, commit: str, filename: str) -> str | None:
    proc = _git(repo, "rev-parse", f"{commit}:{filename}", check=False)
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _fetch_branch(repo: Path, branch: str, remote: str) -> None:
    """Best-effort: bring remote objects local so rev-parse/commit-tree work."""
    _git(repo, "fetch", remote, f"refs/heads/{branch}", check=False)


def _hash_bytes(repo: Path, data: bytes) -> str:
    """git hash-object --stdin: raw bytes, no text-mode newline translation."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SyncError(f"git hash-object failed: {proc.stderr.decode(errors='replace')[-300:]}")
    return proc.stdout.decode("ascii", errors="replace").strip()


def _tree_with_files(repo: Path, prev: str | None, blobs: dict[str, str]) -> str:
    """Tree = parent tree with the named files swapped in.

    Parent-less trees contain exactly the provided files. Unrelated parent
    entries are preserved untouched.
    """
    entries: list[str] = []
    seen: set[str] = set()
    if prev:
        raw = _git(repo, "-c", "core.quotePath=false", "ls-tree", "-r", "-z", prev).stdout
        for part in raw.split("\0"):
            if not part:
                continue
            meta, sep, name = part.partition("\t")
            if not sep:
                continue
            if name in blobs:
                mode = meta.split(" ", 2)[0]
                entries.append(f"{mode} blob {blobs[name]}\t{name}\0")
                seen.add(name)
            else:
                entries.append(part + "\0")
    for name, blob in blobs.items():
        if name not in seen:
            entries.append(f"100644 blob {blob}\t{name}\0")
    return _git(repo, "mktree", "-z", input_text="".join(entries), check=True).stdout.strip()


def push_files(
    files: dict[str, bytes],
    repo: Path,
    branch: str = DEFAULT_BRANCH,
    remote: str = "origin",
    attempts: int = 3,
    commit_msg: str = "codex-state: update snapshot",
) -> str:
    """Atomically push all `files` to `branch`. Returns 'pushed' or 'unchanged'.

    'unchanged' when every file already matches the remote branch tip.
    Raises SyncError on auth/network failure or after exhausting retries.
    """
    if not files:
        raise SyncError("no files to push")
    blobs = {name: _hash_bytes(repo, data) for name, data in files.items()}

    def matches(prev: str) -> bool:
        return all(_file_blob_at(repo, prev, name) == blob for name, blob in blobs.items())

    prev = remote_branch_sha(repo, branch, remote)
    if prev:
        _fetch_branch(repo, branch, remote)
        if matches(prev):
            return "unchanged"

    last_err = ""
    for _ in range(attempts):
        prev = remote_branch_sha(repo, branch, remote)
        if prev:
            _fetch_branch(repo, branch, remote)
            if matches(prev):
                return "unchanged"

        tree = _tree_with_files(repo, prev, blobs)
        commit_args = ("commit-tree", tree)
        if prev:
            commit_args += ("-p", prev)
        commit = _git(repo, *commit_args, "-m", commit_msg, check=True).stdout.strip()

        proc = _git(repo, "push", remote, f"{commit}:refs/heads/{branch}", check=False)
        if proc.returncode == 0:
            return "pushed"

        err = ((proc.stderr or "") + (proc.stdout or "")).strip()
        if _is_auth_error(err):
            raise SyncError(f"git push auth failed: {err[-300:]}")
        last_err = err[-500:]

    raise SyncError(f"push rejected {attempts}x (remote races?): {last_err}")


def read_branch_file(
    repo: Path,
    filename: str,
    branch: str = DEFAULT_BRANCH,
    remote: str = "origin",
) -> bytes | None:
    """Read one file from the tip of the remote branch. None if absent."""
    prev = remote_branch_sha(repo, branch, remote)
    if not prev:
        return None
    _fetch_branch(repo, branch, remote)
    raw = subprocess.run(
        ["git", "-C", str(repo), "show", f"{prev}:{filename}"],
        capture_output=True,
    )
    if raw.returncode != 0:
        return None
    return raw.stdout


def sync_state(
    state_file: Path,
    repo: Path | None = None,
    branch: str = DEFAULT_BRANCH,
    remote: str = "origin",
    attempts: int = 3,
) -> str:
    """Push state_file as state.json on `branch`. Returns 'pushed'/'unchanged'.

    Raises SyncError on missing file, auth/network failure, or retries exhausted.
    """
    repo = repo or BASE_DIR
    state_file = Path(state_file)
    if not state_file.is_file():
        raise SyncError(f"state file missing: {state_file}")
    return push_files(
        {STATE_FILENAME: state_file.read_bytes()},
        repo=repo,
        branch=branch,
        remote=remote,
        attempts=attempts,
        commit_msg="codex-state: update usage reset metadata",
    )

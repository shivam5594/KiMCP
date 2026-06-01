"""Snapshot mechanism per `safety.md`.

  - git mode: commit current state on the project's repo.
  - copy mode: timestamped directory copy under `<project>/.kimcp/snapshots/`.
  - off: explicitly disabled (config must acknowledge; documented in audit log).

`snapshot()` returns a reference string that the tool output envelope puts in
`meta.snapshot_ref`. Format:
  - `git:<sha>`              — committed snapshot
  - `copy:<absolute-path>`   — copy-mode snapshot directory
  - `disabled`               — snapshot intentionally off
"""

from __future__ import annotations

import datetime as _dt
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be taken but was required."""


def is_git_repo(path: Path) -> bool:
    """Return True if `path` is inside a git working tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def snapshot(
    project_root: Path,
    *,
    mode: str = "git",
    reason: str = "pre-mutation",
) -> str:
    """Take a snapshot and return a reference string."""
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise SnapshotError(f"not a directory: {project_root}")

    if mode == "off":
        log.info("snapshot disabled by config for %s", project_root)
        return "disabled"

    if mode == "git" and is_git_repo(project_root):
        return _git_snapshot(project_root, reason=reason)

    if mode == "git":
        log.info(
            "%s is not a git repo; falling back to copy-mode snapshot",
            project_root,
        )

    return _copy_snapshot(project_root, reason=reason)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _git_snapshot(project_root: Path, *, reason: str) -> str:
    subprocess.run(
        ["git", "-C", str(project_root), "add", "-A"],
        check=True,
        capture_output=True,
    )
    # `--allow-empty` so a snapshot-mark is still recorded when nothing is dirty.
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "-c",
            "user.name=kimcp",
            "-c",
            "user.email=kimcp@local",
            "commit",
            "--allow-empty",
            "-m",
            f"kimcp-snapshot: {reason}",
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise SnapshotError(f"git commit failed: {commit.stderr.strip()}")

    sha_proc = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return f"git:{sha_proc.stdout.strip()}"


def _copy_snapshot(project_root: Path, *, reason: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest_root = project_root / ".kimcp" / "snapshots" / ts
    dest_root.mkdir(parents=True, exist_ok=True)

    for entry in project_root.iterdir():
        if entry.name == ".kimcp":
            # Don't recurse into our own state directory.
            continue
        target = dest_root / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)

    (dest_root / "_kimcp_snapshot.txt").write_text(
        f"reason: {reason}\ntimestamp: {ts}\n",
        encoding="utf-8",
    )
    return f"copy:{dest_root}"


class SnapshotPolicy:
    """Per-server-session cadence governor for ``snapshot()``.

    Tracks how many mutating calls have been made per ``project_root``
    and decides whether the Nth call should actually snapshot or skip.
    When ``every_n_calls=1`` (the default), every call snapshots — same
    semantics as calling ``snapshot()`` directly. With higher N, only
    the 1st, (N+1)th, (2N+1)th, ... calls snapshot; intermediate calls
    return a ``"skipped:..."`` reference so audit logs and
    ``meta.snapshot_ref`` still record the decision.

    State lives in-memory on the server instance; restarting the
    server resets the counter. Git history (or copy-mode directories)
    persists across restarts, so recovery from a prior snapshot is
    unaffected — only the cadence of NEW snapshots is throttled.

    The counter is per-project-root: editing schematics in two
    different projects within one session does not interleave their
    cadences. Each project gets its own independent ``1, 2, …, N``
    sequence.
    """

    def __init__(self, every_n_calls: int = 1) -> None:
        if every_n_calls < 1:
            raise ValueError(
                f"every_n_calls must be >= 1; got {every_n_calls}"
            )
        self.every_n_calls = every_n_calls
        self._counters: dict[Path, int] = {}

    def maybe_snapshot(
        self,
        project_root: Path,
        *,
        mode: str = "git",
        reason: str = "pre-mutation",
    ) -> str:
        project_root = project_root.resolve()
        next_count = self._counters.get(project_root, 0) + 1
        self._counters[project_root] = next_count
        if (next_count - 1) % self.every_n_calls != 0:
            return (
                f"skipped:every-{self.every_n_calls}:call={next_count}"
            )
        return snapshot(project_root, mode=mode, reason=reason)


def take_snapshot(
    policy: SnapshotPolicy | None,
    project_root: Path,
    *,
    mode: str = "git",
    reason: str = "pre-mutation",
) -> str:
    """Take a snapshot through ``policy`` when provided, else fall back
    to the unconditional ``snapshot()`` function.

    Tools call this rather than ``snapshot()`` directly so the
    server-injected policy can throttle the cadence
    (``safety.snapshot_every_n_calls``). Tools instantiated outside a
    server (e.g. unit tests) get the original always-snapshot behavior
    automatically.
    """
    if policy is None:
        return snapshot(project_root, mode=mode, reason=reason)
    return policy.maybe_snapshot(project_root, mode=mode, reason=reason)


__all__ = [
    "SnapshotError",
    "SnapshotPolicy",
    "is_git_repo",
    "snapshot",
    "take_snapshot",
]

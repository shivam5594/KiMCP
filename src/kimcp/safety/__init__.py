"""Safety primitives — snapshots, audit log, confirmation tokens.

See `.claude/skills/kimcp-architecture/safety.md` for the policy.
"""

from __future__ import annotations

from kimcp.safety.audit import audit_log_path, record
from kimcp.safety.snapshot import (
    SnapshotError,
    SnapshotPolicy,
    is_git_repo,
    snapshot,
    take_snapshot,
)

__all__ = [
    "SnapshotError",
    "SnapshotPolicy",
    "audit_log_path",
    "is_git_repo",
    "record",
    "snapshot",
    "take_snapshot",
]

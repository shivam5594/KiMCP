"""Audit log — append-only JSONL at `<project>/.kimcp/audit.log`.

One JSON object per line with a stable minimal schema:

    {
      "ts":  "2026-04-14T18:30:00Z",
      "tool": "move_component",
      "input_summary": { ... },
      "snapshot_ref": "git:abc123",
      "note": "destructive-confirm bypassed"
    }

Input summaries are the responsibility of the caller — do NOT blindly dump
raw input, since some tools accept large payloads. Callers compose a compact
summary and pass it in.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


def audit_log_path(project_root: Path) -> Path:
    return project_root / ".kimcp" / "audit.log"


def record(
    project_root: Path,
    *,
    tool: str,
    input_summary: dict[str, Any],
    snapshot_ref: str | None = None,
    note: str = "",
) -> None:
    path = audit_log_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tool": tool,
        "input_summary": input_summary,
        "snapshot_ref": snapshot_ref,
        "note": note,
    }

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")


__all__ = ["audit_log_path", "record"]

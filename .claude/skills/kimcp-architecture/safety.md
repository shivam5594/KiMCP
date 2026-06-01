# Safety Model

PCB and schematic edits are expensive to undo. AI-assisted edits without safeguards can destroy work. This document is the policy.

## Principles

1. **Nothing destructive happens silently.** Snapshot or refuse.
2. **Dry-run is a first-class mode**, not a debug flag. Every mutating tool supports it.
3. **Read vs write is declared at the tool level.** No tool is "might-mutate" — either it does or it doesn't.
4. **User intent is confirmed for irreversible actions.** The server refuses destructive ops without a confirmation token unless the session has pre-authorized them.

## Classification

Each tool is one of:

- `read` — no state change. Safe to run freely.
- `mutate` — modifies project state. Supports `dry_run=true`. Snapshot taken before first mutation in a session (configurable per-tool vs per-op).
- `destructive` — irreversible or hard-to-reverse (file delete, library overwrite, schematic structural delete, mass rename ≥ threshold). Requires snapshot + confirmation.
- `external` — runs a process with side effects outside the project (email, upload). Requires explicit user config to enable.

## Snapshot mechanism

Snapshot = a git commit if the project is a git repo, else a timestamped directory copy in `<project>/.kimcp/snapshots/<ts>/`.

- Snapshot reference returned in tool output `meta.snapshot_ref`.
- `restore_snapshot(ref)` is a tool — undoing is discoverable and testable.
- Rotation: directory copies pruned to a configurable count (default 20). Git commits never auto-removed.
- `.gitignore` entry for snapshots when using copy-mode.

## Dry-run semantics

- `dry_run=true` returns the same output shape as a real run plus a `dry_run_diff` field describing what *would* change.
- Dry-run never writes files, never starts `kicad-cli` with write flags, never mutates IPC state.
- Diff format: for S-expression writes, a structured diff (paths added/removed/changed); for file outputs, a manifest of files that would be created.

## Confirmation tokens

- `destructive` tools return a `confirmation_required` error on first call with a `confirmation_token`.
- Second call with the token within a short TTL (default 60 s) proceeds.
- Session can set `auto_confirm_destructive=true` to bypass — explicit, logged, and shown in every subsequent tool response's `meta.warnings`.

## Thresholds (configurable)

- Mass rename threshold: 20 nets / 20 ref-des by default → elevates to `destructive`.
- File delete of any kind → `destructive`.
- Library remove / overwrite → `destructive`.
- Drop of an IPC-connected design change that would lose unsaved GUI state → `destructive`.

## Concurrency

- Two sessions writing the same project at once is a conflict.
- Server enforces an advisory lock (`<project>/.kimcp/lockfile`) per project per write.
- Readers do not block; writers queue with a timeout.
- Stale lock detection (PID no longer alive → break lock with a warning).

## Backend-specific safety

- **IPC API**: refuse writes when the user has unsaved GUI changes detected via IPC; require explicit `--even-if-dirty` override.
- **S-expression parser**: never write files without a complete successful re-parse of our own output (round-trip validation).
- **kicad-cli**: write to a temp directory, atomically rename on success.
- **SWIG pcbnew**: wrap in try/except; on exception, abandon the op and report (no partial writes).

## Logging

- Every destructive op logged to `<project>/.kimcp/audit.log` with timestamp, tool, input summary, snapshot ref.
- Audit log append-only; rotation separate from snapshots.

## What the user always sees

- Tool response `meta.warnings` surfaces any `live_sync=false`, `backup_skipped`, `auto_confirm=true`, or rule violations.
- No silent degradation.

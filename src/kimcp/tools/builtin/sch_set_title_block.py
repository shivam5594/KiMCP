"""sch_set_title_block — mutate the `(title_block ...)` of a .kicad_sch (M12).

The **first MUTATE tool that edits a KiCAD file**. Every subsequent
schematic/PCB mutation tool follows the pattern this one establishes:

1. Preflight: resolve + validate the path, parse the file, confirm the
   top-level shape.
2. Plan changes in memory against the parsed tree (no writes yet).
3. If ``dry_run=True`` → return the plan and bail out. Per ADR-0008.
4. Snapshot the project *before* writing (git commit or copy-mode).
5. Atomic save via ``SexprDocument.save()`` — round-trip validates
   the serialized output before the rename.

Why sch_set_title_block is the right first mutation:

* **Narrow blast radius.** Editing ``title`` / ``date`` / ``rev`` /
  ``company`` / ``comment1..9`` cannot break connectivity, references,
  or geometry. If the mutation goes wrong, the schematic still opens.
* **Fits the moat.** Prompt-driven schematic creation starts with a
  named board; "generate a power-supply schematic, title it 'Main 5V'"
  needs title_block writes.
* **Exercises the full sexpr path.** Load → find node → mutate leaf →
  round-trip serialize → atomic save. That codepath is what every
  schematic mutator will use.
* **Correct backend.** KiCAD 9.x IPC exposes no title-block writer;
  ADR-0015 makes the SEXPR backend the authoritative path for headless
  schematic edits.

Field semantics:

* ``None`` on an input field = **don't touch it**. Pre-existing values
  survive unchanged.
* ``""`` (empty string) = **explicit set to empty**. KiCAD honors
  empty string values in title_block.
* Missing fields are created on first set; matching existing values
  no-op (no write, no snapshot).

Status enum:

* **ok**                — changes applied and written.
* **no_changes**        — every requested field already matched; no
                          write performed, no snapshot taken.
* **dry_run**           — caller passed ``dry_run=True``; returns the
                          list of fields that would change without
                          touching the filesystem.
* **sch_not_found**     — path missing, not a file, or wrong suffix.
* **invalid_schema**    — parseable but top_head isn't ``kicad_sch``.
* **parse_failed**      — the SEXPR parser rejected the file bytes.
* **write_failed**      — atomic save / round-trip validation raised.

Classification rationale: MUTATE (not DESTRUCTIVE) — we modify project
state but reversibly via the snapshot taken before the write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, SnapshotPolicy, snapshot, take_snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.cache import ParseCache
from kimcp.tools.builtin._sexpr_build import load_sexpr_doc
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)


# Simple string-valued title_block fields. Each lives as
# ``(title "...")`` / ``(date "...")`` / etc. directly under
# ``(title_block ...)``.
_SIMPLE_FIELDS: tuple[str, ...] = ("title", "date", "rev", "company")

# KiCAD title_block exposes nine numbered comment slots. They live as
# ``(comment N "text")`` with N as an unquoted integer atom.
_COMMENT_SLOTS: tuple[int, ...] = tuple(range(1, 10))


# -- input / output --------------------------------------------------------


class SchSetTitleBlockInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    title: str | None = Field(
        default=None,
        description=(
            "Schematic title. None = leave existing value untouched; "
            "empty string = explicitly clear."
        ),
    )
    date: str | None = Field(
        default=None,
        description=(
            "Schematic date. Free-form — KiCAD doesn't constrain the "
            "format. None = leave untouched; empty string = clear."
        ),
    )
    rev: str | None = Field(
        default=None,
        description="Revision label. None = leave untouched; empty string = clear.",
    )
    company: str | None = Field(
        default=None,
        description="Company / org name. None = leave untouched; empty string = clear.",
    )
    comment1: str | None = Field(default=None, description="Comment slot 1.")
    comment2: str | None = Field(default=None, description="Comment slot 2.")
    comment3: str | None = Field(default=None, description="Comment slot 3.")
    comment4: str | None = Field(default=None, description="Comment slot 4.")
    comment5: str | None = Field(default=None, description="Comment slot 5.")
    comment6: str | None = Field(default=None, description="Comment slot 6.")
    comment7: str | None = Field(default=None, description="Comment slot 7.")
    comment8: str | None = Field(default=None, description="Comment slot 8.")
    comment9: str | None = Field(default=None, description="Comment slot 9.")
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, plan the edits and report which fields would change "
            "without writing. Per ADR-0008, every mutating tool supports "
            "dry-run."
        ),
    )


class SchSetTitleBlockOutput(ToolOutput):
    status: Literal[
        "ok",
        "no_changes",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path to the .kicad_sch. Null only when the "
            "file couldn't be located at all."
        ),
    )
    fields_changed: list[str] = Field(
        default_factory=list,
        description=(
            "Logical names of the title_block fields this call changed "
            "(e.g. ['title', 'rev', 'comment1']). Empty for no_changes, "
            "dry_run-with-nothing-to-do, and all error statuses. For "
            "dry_run, lists what *would* change."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses (reason + actionable hint).",
    )


# -- tool ------------------------------------------------------------------


class SchSetTitleBlockTool(Tool[SchSetTitleBlockInput, SchSetTitleBlockOutput]):
    """Mutate the ``(title_block ...)`` of a .kicad_sch via the SEXPR backend."""

    name = "sch_set_title_block"
    version = "0.1.0"
    description = (
        "Set title / date / rev / company / comment1..9 on a .kicad_sch "
        "schematic's title_block. None-valued inputs are left untouched; "
        "empty strings explicitly clear. Supports dry_run; snapshots the "
        "project before writing per ADR-0008."
    )
    input_model = SchSetTitleBlockInput
    output_model = SchSetTitleBlockOutput
    # MUTATE — we modify the .kicad_sch on disk. Not DESTRUCTIVE: a
    # snapshot is taken before the write (reversible).
    classification = ToolClass.MUTATE
    mutates = True
    # SEXPR is the only backend that can mutate a 9.x schematic headlessly
    # (ADR-0015). The dispatcher gates on its availability — SexprBackend's
    # probe is unconditionally True, so in practice this just flows through.
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    _parse_cache: ParseCache | None = None

    def set_config(self, config: Config) -> None:
        self._config = config

    _snapshot_policy: SnapshotPolicy | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    def set_snapshot_policy(self, policy: SnapshotPolicy) -> None:
        self._snapshot_policy = policy

    async def run(self, input: SchSetTitleBlockInput) -> SchSetTitleBlockOutput:
        # 1. Preflight: resolve and validate the path up front so we
        # never parse a file we know will be rejected.
        sch_path = input.sch_path.expanduser().resolve()

        if not sch_path.exists():
            return SchSetTitleBlockOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchSetTitleBlockOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchSetTitleBlockOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_set_title_block runs on a "
                    "schematic file, not a project or board."
                ),
            )

        # 2. Parse the schematic. Anything the parser rejects becomes
        # parse_failed — we surface the underlying message so the user
        # has something to act on.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchSetTitleBlockOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        # 3. Shape check: must be a schematic file. Touching title_block
        # on a .kicad_pcb or .kicad_sym would silently corrupt it.
        if doc.top_head != "kicad_sch":
            return SchSetTitleBlockOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'. Is this really a "
                    "schematic file?"
                ),
            )

        # 4. Collect the requested edits. None = leave alone, "" or any
        # other string = set. Distinguishing None from "" matters: users
        # who want to *clear* a title must be able to do so without
        # passing an unrelated field's current value.
        simple_edits: dict[str, str] = {}
        for name in _SIMPLE_FIELDS:
            value = getattr(input, name)
            if value is not None:
                simple_edits[name] = value

        comment_edits: dict[int, str] = {}
        for slot in _COMMENT_SLOTS:
            value = getattr(input, f"comment{slot}")
            if value is not None:
                comment_edits[slot] = value

        if not simple_edits and not comment_edits:
            return SchSetTitleBlockOutput(
                status="no_changes",
                sch_path=str(sch_path),
                note=(
                    "no title_block fields were requested — pass at least "
                    "one of title / date / rev / company / comment1..9."
                ),
            )

        # 5. Find or create the title_block node. KiCAD usually emits an
        # empty `(title_block)` even for blank sheets, but it's legal to
        # omit it entirely — we must tolerate both shapes.
        title_block = doc.root.find("title_block")
        created_title_block = False
        if title_block is None:
            title_block = SList(items=[SAtom(text="title_block")])
            _insert_title_block(doc.root, title_block)
            created_title_block = True

        # 6. Apply simple fields, tracking which ones actually changed.
        fields_changed: list[str] = []
        for name, value in simple_edits.items():
            if _apply_simple_field(title_block, name, value):
                fields_changed.append(name)

        for slot, value in comment_edits.items():
            if _apply_comment_slot(title_block, slot, value):
                fields_changed.append(f"comment{slot}")

        # Edge case: title_block was synthesized but every field we
        # added happened to match an empty-but-present KiCAD default.
        # `_apply_*` returns True on first insert, so fields_changed is
        # non-empty whenever we created anything — but we also mark the
        # change when the user explicitly "sets" a value identical to
        # what was already there but we had to create the node. The
        # practical upshot: if title_block was missing and the user
        # asked for any field, we always count it as a change.
        if created_title_block and not fields_changed:
            # Defensive — shouldn't be reachable given the above, but
            # if it is, fall through to no_changes cleanly rather than
            # writing a schematic with an orphan empty title_block.
            doc.root.items.remove(title_block)
            doc.root.mark_dirty()
            return SchSetTitleBlockOutput(
                status="no_changes",
                sch_path=str(sch_path),
                note="all requested values already matched the existing state.",
            )

        if not fields_changed:
            return SchSetTitleBlockOutput(
                status="no_changes",
                sch_path=str(sch_path),
                note="all requested values already matched the existing state.",
            )

        # 7. Dry-run short-circuit — plan returned, file untouched.
        if input.dry_run:
            return SchSetTitleBlockOutput(
                status="dry_run",
                sch_path=str(sch_path),
                fields_changed=fields_changed,
                note=(
                    "dry_run=True; no files were written. Re-run with "
                    "dry_run=False to apply these changes."
                ),
            )

        # 8. Snapshot before write. The project root is the directory
        # containing the schematic — small, predictable, and matches
        # what a user would reasonably back up. If no config is wired
        # (standalone tool use without a server), default to 'git' so
        # the safety posture isn't worse than production.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_set_title_block:{sch_path.name}",
            )
        except SnapshotError as exc:
            return SchSetTitleBlockOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=(
                    f"snapshot failed before write: {exc}. No mutation "
                    "was applied. Fix the snapshot path or set "
                    "safety.snapshot_mode='off' to skip."
                ),
            )

        # 9. Atomic save. SexprDocument.save round-trip-parses its own
        # output before the rename and raises on drift; we surface that
        # as write_failed so callers don't treat a bad serialization as
        # success.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchSetTitleBlockOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=(
                    f"save failed after snapshot: {exc}. The snapshot "
                    "captures the pre-mutation state; restore from there "
                    "if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchSetTitleBlockOutput(
            status="ok",
            sch_path=str(sch_path),
            fields_changed=fields_changed,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _insert_title_block(root: SList, title_block: SList) -> None:
    """Insert a fresh title_block node at a reasonable position.

    KiCAD's canonical ordering is ``version`` → ``generator`` →
    ``uuid`` → ``paper`` → ``title_block`` → (rest). We follow it when
    possible so a fresh insert reads naturally; when the expected
    predecessor is missing, we fall back to appending.
    """
    preferred_predecessors = ("paper", "uuid", "generator", "version")
    for head in preferred_predecessors:
        existing = root.find(head)
        if existing is not None:
            idx = root.items.index(existing)
            root.insert(idx + 1, title_block)
            return
    # No anchor found — append to the end. Still valid syntactically.
    root.append(title_block)


def _apply_simple_field(title_block: SList, head: str, value: str) -> bool:
    """Set ``(head "value")`` under title_block. Returns True if the
    file bytes will change.

    Rules:
    * If the field is absent, append it — returns True.
    * If present with the same quoted value, no-op — returns False.
    * If present with a different value or a non-atom payload, rewrite
      — returns True.
    """
    existing = title_block.find(head)
    if existing is None:
        title_block.append(
            SList(
                items=[
                    SAtom(text=head),
                    SAtom(text=value, quoted=True),
                ]
            )
        )
        return True

    # Shape: (head "value"). If someone hand-edited the file and broke
    # the shape, we normalize back to the canonical form.
    if len(existing.items) < 2 or not isinstance(existing.items[1], SAtom):
        # Replace the entire tail with a single quoted atom.
        existing.set_items([SAtom(text=head), SAtom(text=value, quoted=True)])
        return True

    current = existing.items[1]
    if current.text == value and current.quoted:
        return False
    current.set_text(value, quoted=True)
    return True


def _apply_comment_slot(title_block: SList, slot: int, value: str) -> bool:
    """Set ``(comment <slot> "value")`` under title_block.

    Like _apply_simple_field but keyed by the numeric slot atom at
    index 1 rather than the head atom. Returns True if the file bytes
    will change.
    """
    for existing in title_block.find_all("comment"):
        if len(existing.items) < 2 or not isinstance(existing.items[1], SAtom):
            continue
        slot_atom = existing.items[1]
        try:
            existing_slot = int(slot_atom.text)
        except ValueError:
            continue
        if existing_slot != slot:
            continue

        # Matching comment entry. Ensure the value atom is in shape.
        if len(existing.items) < 3 or not isinstance(existing.items[2], SAtom):
            existing.set_items(
                [
                    SAtom(text="comment"),
                    SAtom(text=str(slot)),
                    SAtom(text=value, quoted=True),
                ]
            )
            return True

        current = existing.items[2]
        if current.text == value and current.quoted:
            return False
        current.set_text(value, quoted=True)
        return True

    # Not found — append a fresh entry.
    title_block.append(
        SList(
            items=[
                SAtom(text="comment"),
                SAtom(text=str(slot)),
                SAtom(text=value, quoted=True),
            ]
        )
    )
    return True


__all__ = [
    "SchSetTitleBlockInput",
    "SchSetTitleBlockOutput",
    "SchSetTitleBlockTool",
]

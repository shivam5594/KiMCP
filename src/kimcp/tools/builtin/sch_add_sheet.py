"""sch_add_sheet — place a hierarchical subsheet on a .kicad_sch (M29).

The first multi-sheet mutator. KiCAD's hierarchical-design model
lets a large schematic decompose into subsheets that cross-reference
each other via hierarchical labels + sheet pins. Prior milestones
landed the single-sheet toolkit (M14-M21); this tool unlocks the
parent-sheet placement of a subsheet box that embeds a child
.kicad_sch.

Node shape (KiCAD 9.x)::

    (sheet
      (at X Y)
      (size W H)
      (fields_autoplaced yes)
      (stroke (width 0) (type solid))
      (fill (color 0 0 0 0.0000))
      (uuid "<new>")
      (property "Sheetname" "<display>"
        (at X (Y - 0.8) 0)
        (effects (font (size 1.27 1.27)) (justify left bottom)))
      (property "Sheetfile" "<relative-path>"
        (at X (Y + H + 0.8) 0)
        (effects (font (size 1.27 1.27)) (justify left top))))

Scope of the first ship
-----------------------

* **No sheet pins.** The parent's ``(pin ...)`` children inside the
  sheet block (matching the child's hierarchical_label names) are
  deferred — wire the child labels first, then a future tool will
  generate the matching parent pins.
* **Child file creation is opt-in.** ``create_if_missing=True`` (the
  default) synthesizes a minimal valid .kicad_sch at ``sheet_file`` if
  it's absent. Off by default would mean most callers still have to
  hand-write the child; on by default gets a working subsheet link
  from one call.
* **No ``(instances ...)`` block emitted for the parent.** KiCAD 9.x
  auto-populates it on first open/save using the project file's name;
  guessing a project name here would force a second write to correct
  and would drift when the .kicad_pro is renamed.
* **Relative path storage.** ``sheet_file`` is always stored relative
  to the parent schematic's directory (KiCAD's canonical form). Absolute
  inputs get rebased automatically.

Status enum
-----------

* **ok**                     — sheet appended (+ child created if requested).
* **dry_run**                — caller passed ``dry_run=True``.
* **sch_not_found**          — parent path missing / not a file / wrong suffix.
* **invalid_schema**         — parent top_head isn't ``kicad_sch``.
* **parse_failed**           — SEXPR parser rejected the parent bytes.
* **invalid_input**          — empty sheet_name / sheet_file, bad suffix,
                               non-positive dimensions.
* **sheet_file_conflict**    — target sheet_file exists but isn't a
                               regular file, or (when create_if_missing
                               is True and the file exists) isn't a
                               valid .kicad_sch.
* **write_failed**           — atomic save / round-trip validation raised.

Backend: SEXPR, required.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, SnapshotPolicy, snapshot, take_snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import (
    apply_grid_snap,
    at_node,
    at_node_explicit,
    atom,
    effects_node,
    flag_node,
    fmt_mm,
    load_sexpr_doc,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


# Property-label vertical offset from the sheet box top / bottom, in mm.
# KiCAD writes 0.7-0.8 mm — we use 0.8, which matches eeschema's
# fields_autoplaced emission for a default-stroked sheet.
_LABEL_OFFSET_MM = 0.8

# Minimal .kicad_sch body written to create an empty child on demand.
# Matches eeschema's own "new sheet" output: version stamp, eeschema
# generator + generator_version pair, root UUID, A4 paper, empty
# lib_symbols block.
#
# Version stamp history (this is the field KiCAD reads to decide
# whether to show the "old file, will be upgraded on save" banner):
#   20230121 — KiCAD 7 schematic format
#   20240108 — KiCAD 8 schematic format
#   20250114 — KiCAD 9 early
#   20250610 — KiCAD 9 late / KiCAD 10 (what KiCAD 10 writes itself)
#
# We stamp the newest we've seen on disk from KiCAD 10 (`20250610`)
# and emit the `(generator_version ...)` field that KiCAD 9+ always
# writes — without it, KiCAD 10 treats the child as pre-v9 legacy,
# which breaks hierarchical-sheet resolution even after the banner's
# "upgrade on save". Bump both in lockstep when upstream moves.
_EMPTY_CHILD_TEMPLATE = (
    "(kicad_sch\n"
    '\t(version 20250610)\n'
    '\t(generator "eeschema")\n'
    '\t(generator_version "9.99")\n'
    '\t(uuid "{uuid}")\n'
    '\t(paper "A4")\n'
    "\t(lib_symbols)\n"
    ")\n"
)


# -- input / output --------------------------------------------------------


class SchAddSheetInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description=(
            "Path to the parent .kicad_sch file. The new sheet node is "
            "appended here."
        ),
    )
    sheet_name: str = Field(
        ...,
        description=(
            "Display name for the subsheet (shown in the eeschema tab "
            "bar and the sheet-name property label). Must be non-empty."
        ),
    )
    sheet_file: str = Field(
        ...,
        description=(
            "Subsheet filename, relative to the parent's directory (e.g. "
            "'power.kicad_sch'). Absolute paths are rebased to the parent's "
            "directory. Must end in .kicad_sch."
        ),
    )
    at_x: float = Field(..., description="Top-left X of the sheet box, in mm.")
    at_y: float = Field(..., description="Top-left Y of the sheet box, in mm.")
    size_w: float = Field(
        default=50.0,
        gt=0.0,
        description="Sheet-box width in mm. Defaults to 50 mm.",
    )
    size_h: float = Field(
        default=50.0,
        gt=0.0,
        description="Sheet-box height in mm. Defaults to 50 mm.",
    )
    create_if_missing: bool = Field(
        default=True,
        description=(
            "If True (the default) and sheet_file does not exist, "
            "create a minimal valid .kicad_sch at that path so KiCAD "
            "doesn't error on open. If False, emit only the parent-"
            "sheet node — caller owns the child file."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report what would be written without mutating anything.",
    )


class SchAddSheetOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "invalid_input",
        "sheet_file_conflict",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the parent .kicad_sch.",
    )
    sheet_file: str | None = Field(
        default=None,
        description="Relative sheet_file path as written to the parent.",
    )
    sheet_file_abs: str | None = Field(
        default=None,
        description="Resolved absolute path to the child .kicad_sch.",
    )
    sheet_uuid: str | None = Field(
        default=None,
        description="UUID of the new sheet (populated on status=ok only).",
    )
    child_created: bool = Field(
        default=False,
        description=(
            "True when the child .kicad_sch did not exist and was "
            "synthesized. False when the child already existed or "
            "create_if_missing was False."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchAddSheetTool(Tool[SchAddSheetInput, SchAddSheetOutput]):
    """Place a hierarchical subsheet on a parent .kicad_sch."""

    name = "sch_add_sheet"
    version = "0.1.0"
    description = (
        "Place a hierarchical subsheet box on a parent .kicad_sch. By "
        "default, creates the child .kicad_sch file at sheet_file with "
        "a minimal valid body so KiCAD won't error on open. Sheet pins "
        "(matching the child's hierarchical labels) are deferred to a "
        "future milestone. Plan box corner and size on the 100-mil "
        "schematic grid (2.54 mm default; see safety.grid_snap_mm) so "
        "the four corners and any sheet-pin attachment points land on "
        "grid; off-grid inputs are snapped (cites KICAD-318) and a "
        "meta.warnings entry is emitted. Supports dry_run; snapshots "
        "before write per ADR-0008."
    )
    input_model = SchAddSheetInput
    output_model = SchAddSheetOutput
    classification = ToolClass.MUTATE
    mutates = True
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

    async def run(self, input: SchAddSheetInput) -> SchAddSheetOutput:
        # 1. Input sanity — fail fast before any filesystem work.
        if not input.sheet_name:
            return SchAddSheetOutput(
                status="invalid_input",
                note="sheet_name must be non-empty.",
            )
        if not input.sheet_file:
            return SchAddSheetOutput(
                status="invalid_input",
                note="sheet_file must be non-empty.",
            )
        if not input.sheet_file.lower().endswith(".kicad_sch"):
            return SchAddSheetOutput(
                status="invalid_input",
                note=(
                    f"sheet_file must end in .kicad_sch; got {input.sheet_file!r}."
                ),
            )

        # 2. Parent path checks.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddSheetOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddSheetOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddSheetOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 3. Resolve sheet_file against the parent's directory. Absolute
        # inputs rebase; relative inputs are joined. KiCAD stores only
        # the relative form in the parent, so we compute both.
        parent_dir = sch_path.parent
        raw = Path(input.sheet_file)
        child_abs = (raw if raw.is_absolute() else parent_dir / raw).resolve()
        try:
            child_rel = child_abs.relative_to(parent_dir)
            child_rel_str = str(child_rel)
        except ValueError:
            # Absolute path outside the parent dir — KiCAD tolerates
            # this but it's unusual. Store the absolute form unchanged.
            child_rel_str = str(child_abs)

        # 4. Parse the parent schematic + validate shape.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddSheetOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )
        if doc.top_head != "kicad_sch":
            return SchAddSheetOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 5. Child-file state.
        child_created = False
        if child_abs.exists():
            if not child_abs.is_file():
                return SchAddSheetOutput(
                    status="sheet_file_conflict",
                    sch_path=str(sch_path),
                    sheet_file=child_rel_str,
                    sheet_file_abs=str(child_abs),
                    note=(
                        f"sheet_file exists but is not a regular file: "
                        f"{child_abs}"
                    ),
                )
            # When create_if_missing is True we still want the existing
            # child to be valid .kicad_sch — otherwise the link is broken
            # from the start. Skip the parse when create_if_missing=False:
            # the caller explicitly opted out of child management.
            if input.create_if_missing:
                try:
                    child_doc = SexprDocument.from_path(child_abs)
                except SexprParseError as exc:
                    return SchAddSheetOutput(
                        status="sheet_file_conflict",
                        sch_path=str(sch_path),
                        sheet_file=child_rel_str,
                        sheet_file_abs=str(child_abs),
                        note=(
                            f"existing sheet_file {child_abs} is not valid "
                            f"SEXPR: {exc}"
                        ),
                    )
                if child_doc.top_head != "kicad_sch":
                    return SchAddSheetOutput(
                        status="sheet_file_conflict",
                        sch_path=str(sch_path),
                        sheet_file=child_rel_str,
                        sheet_file_abs=str(child_abs),
                        note=(
                            f"existing sheet_file {child_abs} has top-level "
                            f"'({child_doc.top_head or '?'} ...)'; expected "
                            "(kicad_sch ...)."
                        ),
                    )
        # Existence-branch ends: child_abs exists and is either a
        # validated .kicad_sch or we opted out of validating it.

        # Apply grid snap per safety.grid_snap_mm. Snap all four fields —
        # position AND size — so the outline lands on grid in both
        # dimensions and doesn't float a half-tick off.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {
                "at_x": input.at_x,
                "at_y": input.at_y,
                "size_w": input.size_w,
                "size_h": input.size_h,
            },
            grid_snap_mm,
        )
        at_x = snapped["at_x"]
        at_y = snapped["at_y"]
        size_w = snapped["size_w"]
        size_h = snapped["size_h"]

        # 6. Dry-run short-circuit. Report everything but write nothing.
        if input.dry_run:
            note = (
                f"dry_run=True; would place sheet {input.sheet_name!r} "
                f"({size_w}x{size_h}mm) at ({at_x}, "
                f"{at_y}) linking {child_rel_str}."
            )
            if input.create_if_missing and not child_abs.exists():
                note += f" Would also create empty child at {child_abs}."
            out_dry = SchAddSheetOutput(
                status="dry_run",
                sch_path=str(sch_path),
                sheet_file=child_rel_str,
                sheet_file_abs=str(child_abs),
                sheet_uuid=None,
                child_created=False,
                note=note,
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            return out_dry

        # 7. Synthesize the sheet node.
        sheet_uuid = str(uuid_mod.uuid4())
        sheet_node = _build_sheet_node(
            sheet_name=input.sheet_name,
            sheet_file=child_rel_str,
            at_x=at_x,
            at_y=at_y,
            size_w=size_w,
            size_h=size_h,
            sheet_uuid=sheet_uuid,
        )
        doc.root.append(sheet_node)

        # 8. Snapshot before any filesystem write. Covers both the
        # parent mutation and the synthesized child — one unified
        # rollback point per ADR-0008.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode
        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_sheet:{sch_path.name}:{input.sheet_name}",
            )
        except SnapshotError as exc:
            return SchAddSheetOutput(
                status="write_failed",
                sch_path=str(sch_path),
                sheet_file=child_rel_str,
                sheet_file_abs=str(child_abs),
                note=f"snapshot failed before write: {exc}.",
            )

        # 9. Create child if requested and missing. We do the child
        # write *before* the parent so that if child creation fails,
        # the parent's on-disk state stays unchanged.
        if input.create_if_missing and not child_abs.exists():
            try:
                child_abs.parent.mkdir(parents=True, exist_ok=True)
                child_abs.write_text(
                    _EMPTY_CHILD_TEMPLATE.format(uuid=uuid_mod.uuid4()),
                    encoding="utf-8",
                )
                child_created = True
            except OSError as exc:
                out_fail = SchAddSheetOutput(
                    status="write_failed",
                    sch_path=str(sch_path),
                    sheet_file=child_rel_str,
                    sheet_file_abs=str(child_abs),
                    note=f"failed to create child schematic: {exc}.",
                )
                out_fail.meta.snapshot_ref = snapshot_ref
                return out_fail

        # 10. Write the parent schematic.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddSheetOutput(
                status="write_failed",
                sch_path=str(sch_path),
                sheet_file=child_rel_str,
                sheet_file_abs=str(child_abs),
                child_created=child_created,
                note=f"parent save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddSheetOutput(
            status="ok",
            sch_path=str(sch_path),
            sheet_file=child_rel_str,
            sheet_file_abs=str(child_abs),
            sheet_uuid=sheet_uuid,
            child_created=child_created,
        )
        out.meta.snapshot_ref = snapshot_ref
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        return out


# -- helpers ---------------------------------------------------------------


def _property_node(
    *,
    name: str,
    value: str,
    at_x: float,
    at_y: float,
    justify: tuple[str, ...],
) -> SList:
    """``(property "name" "value" (at X Y 0) (effects ...))``.

    Shared between Sheetname and Sheetfile — they differ only in text
    and justify direction. Justify tuple is passed through to
    ``effects_node`` unchanged.

    Uses :func:`at_node_explicit` because KiCAD 10's sheet-property
    parser is strict: omitting the angle atom on a property inside a
    ``(sheet …)`` node (which plain ``at_node`` would do when angle is
    zero) produces the ``Failed to load schematic`` error at load.
    """
    return slist(
        atom("property"),
        atom(name, quoted=True),
        atom(value, quoted=True),
        at_node_explicit(at_x, at_y, 0.0),
        effects_node(justify=justify),
    )


def _build_sheet_node(
    *,
    sheet_name: str,
    sheet_file: str,
    at_x: float,
    at_y: float,
    size_w: float,
    size_h: float,
    sheet_uuid: str,
) -> SList:
    """Synthesize the ``(sheet ...)`` envelope written by eeschema.

    Sheet boxes use a (solid) stroke and a transparent fill; those
    constants are pinned to match KiCAD's default emission so
    round-trip validation passes on save.
    """
    sheetname_node = _property_node(
        name="Sheetname",
        value=sheet_name,
        at_x=at_x,
        at_y=at_y - _LABEL_OFFSET_MM,
        justify=("left", "bottom"),
    )
    sheetfile_node = _property_node(
        name="Sheetfile",
        value=sheet_file,
        at_x=at_x,
        at_y=at_y + size_h + _LABEL_OFFSET_MM,
        justify=("left", "top"),
    )

    # (stroke (width 0) (type solid)) — solid (not default) for the
    # sheet outline; the default wire stroke is 'default', the default
    # sheet stroke is 'solid'. Worth flagging because it's easy to
    # reuse stroke_default_node() and end up with a dashed sheet.
    stroke = slist(
        atom("stroke"),
        slist(atom("width"), atom("0")),
        slist(atom("type"), atom("solid")),
    )

    # (fill (color 0 0 0 0.0000)) — transparent fill; KiCAD writes the
    # alpha as a four-decimal float even when it's zero, so match.
    fill = slist(
        atom("fill"),
        slist(
            atom("color"),
            atom("0"),
            atom("0"),
            atom("0"),
            atom("0.0000"),
        ),
    )

    # The four attribute flags below are NOT cosmetic — KiCAD 10's
    # SCH_IO_KICAD_SEXPR loader requires them on every `(sheet …)`
    # reference. Omitting any one of them causes the parent load to
    # fail with the unhelpful `Failed to load schematic` (exit 3, no
    # stderr detail) for the entire hierarchy. Values match eeschema's
    # emission for a newly-placed sheet: simulation + BOM + board all
    # default-on, DNP off. If the caller ever needs to toggle these,
    # lift them into SchAddSheetInput.
    return slist(
        atom("sheet"),
        at_node(at_x, at_y),
        slist(atom("size"), atom(fmt_mm(size_w)), atom(fmt_mm(size_h))),
        flag_node("exclude_from_sim", False),
        flag_node("in_bom", True),
        flag_node("on_board", True),
        flag_node("dnp", False),
        flag_node("fields_autoplaced", True),
        stroke,
        fill,
        uuid_node(sheet_uuid),
        sheetname_node,
        sheetfile_node,
    )


def _find_sheet_by_uuid(root: SList, sheet_uuid: str) -> SList | None:
    """Return the first ``(sheet ...)`` matching ``sheet_uuid``.

    Test-facing helper; kept module-private (leading underscore) until
    a second consumer emerges.
    """
    for child in root.items:
        if not isinstance(child, SList) or child.head != "sheet":
            continue
        uuid_node_child = child.find("uuid")
        if uuid_node_child is None or len(uuid_node_child.items) < 2:
            continue
        payload = uuid_node_child.items[1]
        if isinstance(payload, SAtom) and payload.text == sheet_uuid:
            return child
    return None


__all__ = [
    "SchAddSheetInput",
    "SchAddSheetOutput",
    "SchAddSheetTool",
    "_build_sheet_node",
    "_find_sheet_by_uuid",
]

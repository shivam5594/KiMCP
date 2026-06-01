"""sch_add_wire — append a wire segment to a .kicad_sch (M15).

The connectivity primitive. M14 places symbols; this tool wires them
together. Together they unlock the minimum prompt-driven flow:

    LLM reads schematic
      → places symbols via sch_add_symbol
      → connects them via sch_add_wire
      → re-reads to verify

Scope of the first ship
-----------------------

* **Single segment per call.** The wire is a straight line from
  ``(start_x, start_y)`` to ``(end_x, end_y)``. Multi-segment polylines
  are built by repeated calls — matches how eeschema stores them on
  disk (each segment is its own ``(wire …)`` entry).
* **No auto-junction.** If the wire terminates on an existing wire, the
  caller must add a junction via ``sch_add_junction`` (M16). KiCAD also
  doesn't auto-insert junctions on save — they have to be present in
  the source.
* **Default stroke only.** ``(stroke (width 0) (type default))`` — what
  eeschema emits. Non-default strokes are schematic-graphic territory,
  not electrical connectivity.
* **No grid snapping.** Coordinates pass through verbatim. Off-grid
  endpoints are legal sexpr but cause ERC warnings; the caller owns
  grid alignment.

Status enum
-----------

* **ok**               — wire appended and written.
* **dry_run**          — caller passed ``dry_run=True``.
* **sch_not_found**    — path missing, not a file, or wrong suffix.
* **invalid_schema**   — top_head isn't ``kicad_sch``.
* **parse_failed**     — the SEXPR parser rejected the file bytes.
* **invalid_geometry** — start and end coincide (zero-length wire).
* **write_failed**     — atomic save / round-trip validation raised.

Backend: SEXPR, required. IPC on KiCAD 9.x has no schematic writer.
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
    atom,
    fmt_mm,
    load_sexpr_doc,
    slist,
    stroke_default_node,
    uuid_node,
)

log = logging.getLogger(__name__)


# -- input / output --------------------------------------------------------


class SchAddWireInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    start_x: float = Field(..., description="Wire start-point X, in millimetres.")
    start_y: float = Field(..., description="Wire start-point Y, in millimetres.")
    end_x: float = Field(..., description="Wire end-point X, in millimetres.")
    end_y: float = Field(..., description="Wire end-point Y, in millimetres.")
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, report the wire that would be added without writing. "
            "Per ADR-0008, every mutating tool supports dry-run."
        ),
    )


class SchAddWireOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "invalid_geometry",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .kicad_sch.",
    )
    wire_uuid: str | None = Field(
        default=None,
        description=(
            "UUID assigned to the new wire segment. Populated on status=ok. "
            "Null for dry_run (no UUID is allocated) and for error statuses."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses.",
    )


# -- tool ------------------------------------------------------------------


class SchAddWireTool(Tool[SchAddWireInput, SchAddWireOutput]):
    """Append a single wire segment to a .kicad_sch via the SEXPR backend."""

    name = "sch_add_wire"
    version = "0.2.0"
    description = (
        "Place a single wire segment connecting two points on a .kicad_sch. "
        "This is the DEFAULT primitive for connecting two pins on the same "
        "sheet — prefer it over sch_add_label whenever a routable path "
        "exists. Wires are what a human reads when following connectivity; "
        "labels are net-name references and should be used sparingly. "
        "To get precise wire endpoints, call sch_get_symbol_pins first to "
        "obtain the absolute (x, y) coordinates of each symbol's pins, "
        "then wire between those coordinates. Do NOT guess pin positions. "
        "Multi-segment polylines are built by repeated calls (one wire per "
        "segment), typically routed as orthogonal H/V runs aligned to the "
        "schematic grid. Use sch_add_junction where a wire terminates on "
        "an existing wire. Coordinates snap to safety.grid_snap_mm "
        "(default 2.54 mm). Supports dry_run; snapshots per ADR-0008."
    )
    input_model = SchAddWireInput
    output_model = SchAddWireOutput
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

    async def run(self, input: SchAddWireInput) -> SchAddWireOutput:
        # 1. Preflight path validation.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddWireOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddWireOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddWireOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_add_wire runs on a schematic "
                    "file, not a project or board."
                ),
            )

        # 2. Apply grid snap per safety.grid_snap_mm (default 2.54 mm,
        # opt out with null). We snap BEFORE the zero-length check so
        # a snap that collapses two close endpoints is caught as
        # invalid_geometry — matching what KiCAD actually stores on
        # disk rather than the caller's original off-grid intent.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {
                "start_x": input.start_x,
                "start_y": input.start_y,
                "end_x": input.end_x,
                "end_y": input.end_y,
            },
            grid_snap_mm,
        )
        start_x = snapped["start_x"]
        start_y = snapped["start_y"]
        end_x = snapped["end_x"]
        end_y = snapped["end_y"]

        # 3. Geometry check. A zero-length wire is accepted by the parser
        # but is electrically meaningless (and triggers ERC 'zero-length
        # wire' warnings). Reject up front — caller almost certainly
        # meant something else.
        if start_x == end_x and start_y == end_y:
            out_geom = SchAddWireOutput(
                status="invalid_geometry",
                sch_path=str(sch_path),
                note=(
                    f"start and end coincide at ({start_x}, {start_y}); "
                    "a zero-length wire has no electrical meaning. Pass distinct "
                    "endpoints or use sch_add_junction for a connection point."
                    + (
                        " (Coordinates were grid-snapped — the pre-snap "
                        "inputs may have been distinct.)"
                        if snap_warning is not None
                        else ""
                    )
                ),
            )
            if snap_warning is not None:
                out_geom.meta.warnings.append(snap_warning)
            return out_geom

        # 4. Parse + shape check.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddWireOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddWireOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 5. Dry-run short-circuit. No UUID allocated, no snapshot taken.
        if input.dry_run:
            out_dry = SchAddWireOutput(
                status="dry_run",
                sch_path=str(sch_path),
                wire_uuid=None,
                note=(
                    f"dry_run=True; would add wire from ({start_x}, "
                    f"{start_y}) to ({end_x}, {end_y}). "
                    "Re-run with dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            return out_dry

        # 6. Generate UUID and synthesize the wire node.
        wire_uuid = str(uuid_mod.uuid4())
        wire_node = _build_wire_node(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            wire_uuid=wire_uuid,
        )
        doc.root.append(wire_node)

        # 6. Snapshot before write.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_wire:{sch_path.name}",
            )
        except SnapshotError as exc:
            return SchAddWireOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=(
                    f"snapshot failed before write: {exc}. No mutation was "
                    "applied."
                ),
            )

        # 7. Atomic save with round-trip validation.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddWireOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddWireOutput(
            status="ok",
            sch_path=str(sch_path),
            wire_uuid=wire_uuid,
        )
        out.meta.snapshot_ref = snapshot_ref
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        return out


# -- helpers ---------------------------------------------------------------


def _build_wire_node(
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    wire_uuid: str,
) -> SList:
    """Synthesize ``(wire (pts (xy S) (xy E)) (stroke ...) (uuid ...))``.

    KiCAD's canonical wire shape in 9.x. The ``(pts …)`` list holds the
    two endpoints as ``(xy X Y)`` children; ``(stroke …)`` defaults to
    width=0/type=default (zero means "use schematic default"), and the
    UUID is a quoted string so round-trip validation accepts it.
    """
    pts: list[SAtom | SList] = [
        atom("pts"),
        slist(atom("xy"), atom(fmt_mm(start_x)), atom(fmt_mm(start_y))),
        slist(atom("xy"), atom(fmt_mm(end_x)), atom(fmt_mm(end_y))),
    ]
    return slist(
        atom("wire"),
        SList(items=pts),
        stroke_default_node(),
        uuid_node(wire_uuid),
    )


__all__ = [
    "SchAddWireInput",
    "SchAddWireOutput",
    "SchAddWireTool",
    "_build_wire_node",
]

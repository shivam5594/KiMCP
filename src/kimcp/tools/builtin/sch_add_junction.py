"""sch_add_junction — mark a connection point where wires cross (M16).

KiCAD does not auto-insert junctions on save; every T-junction (three
or more wire ends meeting at a point) must carry an explicit
``(junction ...)`` node or the net is broken even if the coordinates
coincide visually. This tool places one.

Format pinned from KiCAD 9.x::

    (junction
      (at X Y)
      (diameter 0)           ; 0 = use schematic default
      (color 0 0 0 0)        ; RGBA; all-zero = default
      (uuid "..."))

Scope of the first ship
-----------------------

* **Single junction per call.** No bulk / batch.
* **Default diameter and color only.** Non-default values are editor
  affordances, not electrical — deferred to a sch_edit_junction tool
  when (if) anyone asks for styling.
* **No proximity check.** Adding a junction at ``(X, Y)`` when no
  wires meet there produces a legal but harmless orphan; KiCAD renders
  a dot regardless. Validation is the caller's responsibility.

Status enum
-----------

* **ok**             — junction appended and written.
* **dry_run**        — caller passed ``dry_run=True``.
* **sch_not_found**  — path missing / not a file / wrong suffix.
* **invalid_schema** — top_head isn't ``kicad_sch``.
* **parse_failed**   — the SEXPR parser rejected the file bytes.
* **write_failed**   — atomic save raised.

Backend: SEXPR, required. Same rationale as M14/M15.
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
from kimcp.sexpr.nodes import SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import (
    apply_grid_snap,
    at_node,
    atom,
    int_node,
    load_sexpr_doc,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


# -- input / output --------------------------------------------------------


class SchAddJunctionInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    at_x: float = Field(..., description="Junction X coordinate in millimetres.")
    at_y: float = Field(..., description="Junction Y coordinate in millimetres.")
    dry_run: bool = Field(
        default=False,
        description="If True, report the junction that would be added without writing.",
    )


class SchAddJunctionOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .kicad_sch.",
    )
    junction_uuid: str | None = Field(
        default=None,
        description="UUID of the new junction (populated on status=ok only).",
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses.",
    )


# -- tool ------------------------------------------------------------------


class SchAddJunctionTool(Tool[SchAddJunctionInput, SchAddJunctionOutput]):
    """Append a ``(junction ...)`` node to a .kicad_sch via SEXPR."""

    name = "sch_add_junction"
    version = "0.1.0"
    description = (
        "Place a connection junction at a point on a .kicad_sch. Required "
        "wherever 3+ wire ends meet — KiCAD does not auto-insert junctions. "
        "Plan the coordinate on the 100-mil schematic grid (2.54 mm default; "
        "see safety.grid_snap_mm) so the junction lands exactly on the wire "
        "endpoint it joins; off-grid inputs are snapped (cites KICAD-318) "
        "and a meta.warnings entry is emitted. Emits default diameter and "
        "color. Supports dry_run; snapshots before write per ADR-0008."
    )
    input_model = SchAddJunctionInput
    output_model = SchAddJunctionOutput
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

    async def run(self, input: SchAddJunctionInput) -> SchAddJunctionOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddJunctionOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddJunctionOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddJunctionOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddJunctionOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddJunctionOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Apply grid snap per safety.grid_snap_mm (default 2.54 mm, opt
        # out with null). Runs before dry-run so the preview shows the
        # coords that would actually land on disk, not the off-grid
        # inputs.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {"at_x": input.at_x, "at_y": input.at_y}, grid_snap_mm
        )
        at_x, at_y = snapped["at_x"], snapped["at_y"]

        if input.dry_run:
            out_dry = SchAddJunctionOutput(
                status="dry_run",
                sch_path=str(sch_path),
                junction_uuid=None,
                note=(
                    f"dry_run=True; would add junction at ({at_x}, "
                    f"{at_y}). Re-run with dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            return out_dry

        junction_uuid = str(uuid_mod.uuid4())
        doc.root.append(
            _build_junction_node(
                at_x=at_x,
                at_y=at_y,
                junction_uuid=junction_uuid,
            )
        )

        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_junction:{sch_path.name}",
            )
        except SnapshotError as exc:
            return SchAddJunctionOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=f"snapshot failed before write: {exc}.",
            )

        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddJunctionOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddJunctionOutput(
            status="ok",
            sch_path=str(sch_path),
            junction_uuid=junction_uuid,
        )
        out.meta.snapshot_ref = snapshot_ref
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        return out


# -- helpers ---------------------------------------------------------------


def _build_junction_node(*, at_x: float, at_y: float, junction_uuid: str) -> SList:
    """``(junction (at X Y) (diameter 0) (color 0 0 0 0) (uuid "..."))``.

    ``diameter 0`` tells KiCAD to use the schematic's global default;
    ``color 0 0 0 0`` is the "no-override" RGBA sentinel. Both match
    eeschema's emitted form for a default-styled junction.
    """
    return slist(
        atom("junction"),
        at_node(at_x, at_y),
        int_node("diameter", 0),
        slist(
            atom("color"),
            atom("0"),
            atom("0"),
            atom("0"),
            atom("0"),
        ),
        uuid_node(junction_uuid),
    )


__all__ = [
    "SchAddJunctionInput",
    "SchAddJunctionOutput",
    "SchAddJunctionTool",
    "_build_junction_node",
]

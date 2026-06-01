"""sch_add_no_connect -- place a no-connect flag on a .kicad_sch (M21).

KiCAD's ERC flags unconnected pins as errors. Placing a no-connect
flag (the small ``X`` mark) on a pin tells the checker: "I know this
pin isn't wired, and that's intentional." Without it, ERC reports a
spurious error for every intentionally-floating pin (e.g. NC pins on
an IC, unused outputs on a multi-gate package).

Format pinned from KiCAD 9.x::

    (no_connect
      (at X Y)
      (uuid "..."))

The simplest structural node in a schematic -- just coordinates and a
UUID. No stroke, no shape, no properties. The ``X`` glyph is drawn
by eeschema at render time from the ``(at ...)`` anchor.

Scope of the first ship
-----------------------

* **Single no-connect per call.** No bulk.
* **No pin-snap.** Coordinates pass through verbatim. Off-pin placement
  is legal sexpr but ERC won't associate it with a pin unless the
  coordinates match exactly.

Status enum
-----------

* **ok**             -- no-connect appended and written.
* **dry_run**        -- caller passed ``dry_run=True``.
* **sch_not_found**  -- path missing / not a file / wrong suffix.
* **invalid_schema** -- top_head isn't ``kicad_sch``.
* **parse_failed**   -- the SEXPR parser rejected the file bytes.
* **write_failed**   -- snapshot or atomic save raised.

Backend: SEXPR, required. Same rationale as M14-M20.
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
    atom,
    load_sexpr_doc,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


# -- input / output --------------------------------------------------------


class SchAddNoConnectInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    at_x: float = Field(
        ..., description="No-connect X coordinate in millimetres."
    )
    at_y: float = Field(
        ..., description="No-connect Y coordinate in millimetres."
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the no-connect that would be added without writing.",
    )


class SchAddNoConnectOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None, description="Resolved absolute path to the .kicad_sch."
    )
    no_connect_uuid: str | None = Field(
        default=None,
        description="UUID of the new no-connect flag (populated on status=ok only).",
    )
    note: str | None = Field(
        default=None, description="Diagnostic string for non-ok statuses."
    )


# -- tool ------------------------------------------------------------------


class SchAddNoConnectTool(Tool[SchAddNoConnectInput, SchAddNoConnectOutput]):
    """Place a no-connect flag on a .kicad_sch via the SEXPR backend."""

    name = "sch_add_no_connect"
    version = "0.1.0"
    description = (
        "Place a no-connect flag (X mark) at a point on a .kicad_sch. "
        "Suppresses ERC 'unconnected pin' errors at that coordinate. "
        "Coordinates must match the pin endpoint exactly for ERC to "
        "associate the flag with the pin — both live on the 100-mil "
        "schematic grid (2.54 mm default; see safety.grid_snap_mm). "
        "Off-grid inputs are snapped (cites KICAD-318) and a meta.warnings "
        "entry is emitted. Supports dry_run; snapshots before write per "
        "ADR-0008."
    )
    input_model = SchAddNoConnectInput
    output_model = SchAddNoConnectOutput
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

    async def run(self, input: SchAddNoConnectInput) -> SchAddNoConnectOutput:
        # 1. Preflight path.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddNoConnectOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddNoConnectOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddNoConnectOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 2. Parse.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddNoConnectOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddNoConnectOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 3. Apply grid snap per safety.grid_snap_mm (default 2.54 mm,
        # opt out with null).
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {"at_x": input.at_x, "at_y": input.at_y}, grid_snap_mm
        )
        at_x, at_y = snapped["at_x"], snapped["at_y"]

        # 4. Dry-run.
        if input.dry_run:
            out_dry = SchAddNoConnectOutput(
                status="dry_run",
                sch_path=str(sch_path),
                no_connect_uuid=None,
                note=(
                    f"dry_run=True; would add no_connect at ({at_x}, "
                    f"{at_y}). Re-run with dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            return out_dry

        # 5. Synthesize + append.
        nc_uuid = str(uuid_mod.uuid4())
        doc.root.append(
            _build_no_connect_node(
                at_x=at_x,
                at_y=at_y,
                nc_uuid=nc_uuid,
            )
        )

        # 5. Snapshot.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_no_connect:{sch_path.name}",
            )
        except SnapshotError as exc:
            return SchAddNoConnectOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=f"snapshot failed before write: {exc}.",
            )

        # 6. Save.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddNoConnectOutput(
                status="write_failed",
                sch_path=str(sch_path),
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddNoConnectOutput(
            status="ok",
            sch_path=str(sch_path),
            no_connect_uuid=nc_uuid,
        )
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _build_no_connect_node(
    *, at_x: float, at_y: float, nc_uuid: str
) -> SList:
    """``(no_connect (at X Y) (uuid "..."))``."""
    return slist(
        atom("no_connect"),
        at_node(at_x, at_y),
        uuid_node(nc_uuid),
    )


def _find_no_connect_by_uuid(root: SList, nc_uuid: str) -> SList | None:
    """Return the ``(no_connect ...)`` matching ``nc_uuid``, or None."""
    for child in root.items:
        if not isinstance(child, SList) or child.head != "no_connect":
            continue
        uuid_child = child.find("uuid")
        if uuid_child is None or len(uuid_child.items) < 2:
            continue
        payload = uuid_child.items[1]
        if isinstance(payload, SAtom) and payload.text == nc_uuid:
            return child
    return None


__all__ = [
    "SchAddNoConnectInput",
    "SchAddNoConnectOutput",
    "SchAddNoConnectTool",
    "_build_no_connect_node",
    "_find_no_connect_by_uuid",
]

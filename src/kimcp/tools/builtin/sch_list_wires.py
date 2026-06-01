"""sch_list_wires — enumerate wires, junctions and no-connect markers (M28).

Third schematic READ tool. M26 answers "what components?", M27 "what
nets?", this one "how is the geometry laid out?". Together they give
an LLM the full read-side picture of a sheet before it proposes
edits.

Covers three node heads that share a common trait: they are anchored
primitives with no associated symbol or net name, and they're all the
connectivity scaffolding between the labels and the footprints.

* **wire**       (``(wire (pts (xy X1 Y1) (xy X2 Y2)) (stroke ...) (uuid ...))``)
  Straight copper segment. Emitted one entry per segment — KiCAD's
  wire polyline is stored as ``n`` independent wires, not one
  multi-point polyline.
* **junction**   (``(junction (at X Y) (diameter D) (color ...) (uuid ...))``)
  Dot at a T-junction. KiCAD does not auto-insert these, so their
  absence is a common "my net is broken" bug.
* **no_connect** (``(no_connect (at X Y) (uuid ...))``)
  "X" marker on an intentionally-unconnected pin. Required for ERC
  cleanliness on microcontroller GPIOs you didn't wire up.

Returns one entry per node (``WireInstance`` / ``JunctionInstance`` /
``NoConnectInstance``). Filters:

* ``include`` (list of ``wire`` / ``junction`` / ``no_connect``) — pick
  which kinds to enumerate. Defaults to all three.

Document order is preserved within each kind and across kinds — the
caller can rebuild the sheet's geometry traversal order from the
combined list if needed.

Hierarchical sheets are out of scope — only the requested sheet is
walked, matching M26 / M27.

Status enum:

* **ok**             — node lists populated (may all be empty).
* **sch_not_found**  — path missing / wrong suffix.
* **parse_failed**   — SEXPR parser rejected the file.
* **invalid_schema** — parseable but top_head isn't ``kicad_sch``.

READ classification: no filesystem writes, no subprocess, no snapshot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.cache import ParseCache
from kimcp.tools.builtin._sexpr_build import load_sexpr_doc
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)


IncludeKind = Literal["wire", "junction", "no_connect"]
_DEFAULT_INCLUDE: frozenset[str] = frozenset({"wire", "junction", "no_connect"})


# -- envelope sub-models ---------------------------------------------------


class WireInstance(BaseModel):
    """One straight wire segment from the .kicad_sch."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="Wire UUID (stable across edits).")
    start_x: float = Field(..., description="Segment start X in millimetres.")
    start_y: float = Field(..., description="Segment start Y in millimetres.")
    end_x: float = Field(..., description="Segment end X in millimetres.")
    end_y: float = Field(..., description="Segment end Y in millimetres.")


class JunctionInstance(BaseModel):
    """One `(junction ...)` dot — required wherever 3+ wire ends meet."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="Junction UUID (stable across edits).")
    at_x: float = Field(..., description="Junction X in millimetres.")
    at_y: float = Field(..., description="Junction Y in millimetres.")
    diameter: float = Field(
        ...,
        description=(
            "Rendered dot diameter in millimetres. 0.0 means 'use the "
            "schematic default' (the common case)."
        ),
    )


class NoConnectInstance(BaseModel):
    """One `(no_connect ...)` marker — intentionally-unused pin flag."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="No-connect UUID (stable across edits).")
    at_x: float = Field(..., description="Marker X in millimetres.")
    at_y: float = Field(..., description="Marker Y in millimetres.")


# -- input / output --------------------------------------------------------


class SchListWiresInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    include: list[IncludeKind] | None = Field(
        default=None,
        description=(
            "Which node kinds to include. Any non-empty subset of "
            "['wire', 'junction', 'no_connect']. Null (the default) "
            "returns all three."
        ),
    )


class SchListWiresOutput(ToolOutput):
    status: Literal[
        "ok",
        "sch_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    sch_path: str | None = Field(default=None)
    wires: list[WireInstance] = Field(default_factory=list)
    junctions: list[JunctionInstance] = Field(default_factory=list)
    no_connects: list[NoConnectInstance] = Field(default_factory=list)
    total: int = Field(
        default=0,
        description=(
            "Sum of wires + junctions + no_connects after filtering. Mirrored so "
            "callers don't have to len() three separate lists."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchListWiresTool(Tool[SchListWiresInput, SchListWiresOutput]):
    """Enumerate wires, junctions, and no-connect markers on a .kicad_sch."""

    name = "sch_list_wires"
    version = "0.1.0"
    description = (
        "Enumerate connectivity geometry on a .kicad_sch: wire segments, "
        "junction dots, and no-connect markers. Returns three parallel "
        "lists. Each wire is one straight segment (polylines are stored "
        "as N separate wires). The include parameter narrows to a subset "
        "of {wire, junction, no_connect}."
    )
    input_model = SchListWiresInput
    output_model = SchListWiresOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    async def run(self, input: SchListWiresInput) -> SchListWiresOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or not sch_path.is_file():
            return SchListWiresOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchListWiresOutput(
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
            return SchListWiresOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchListWiresOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        include: frozenset[str] = (
            _DEFAULT_INCLUDE if input.include is None else frozenset(input.include)
        )

        wires: list[WireInstance] = []
        junctions: list[JunctionInstance] = []
        no_connects: list[NoConnectInstance] = []
        for child in doc.root.items:
            if not isinstance(child, SList):
                continue
            if child.head == "wire" and "wire" in include:
                parsed_wire = _parse_wire(child)
                if parsed_wire is not None:
                    wires.append(parsed_wire)
            elif child.head == "junction" and "junction" in include:
                parsed_junc = _parse_junction(child)
                if parsed_junc is not None:
                    junctions.append(parsed_junc)
            elif child.head == "no_connect" and "no_connect" in include:
                parsed_nc = _parse_no_connect(child)
                if parsed_nc is not None:
                    no_connects.append(parsed_nc)

        total = len(wires) + len(junctions) + len(no_connects)
        return SchListWiresOutput(
            status="ok",
            sch_path=str(sch_path),
            wires=wires,
            junctions=junctions,
            no_connects=no_connects,
            total=total,
        )


# -- parse helpers ---------------------------------------------------------


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _atom_at_index(node: SList, idx: int) -> str | None:
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


def _child_atom_text(parent: SList, head: str, idx: int = 1) -> str | None:
    child = parent.find(head)
    if child is None or len(child.items) <= idx:
        return None
    return _atom_text(child.items[idx])


def _try_float(txt: str | None, default: float = 0.0) -> float:
    if txt is None:
        return default
    try:
        return float(txt)
    except ValueError:
        return default


def _parse_at(node: SList) -> tuple[float, float]:
    at = node.find("at")
    if at is None:
        return 0.0, 0.0
    return (
        _try_float(_atom_at_index(at, 1)),
        _try_float(_atom_at_index(at, 2)),
    )


def _parse_wire(node: SList) -> WireInstance | None:
    """Parse ``(wire (pts (xy X1 Y1) (xy X2 Y2)) ... (uuid "..."))``.

    KiCAD encodes multi-point polylines as N separate wires (each with
    its own UUID). Ignore any (xy) beyond the first two — a malformed
    fixture with more points still resolves cleanly to its endpoints.
    Returns None when uuid or pts are missing.
    """
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None
    pts = node.find("pts")
    if pts is None:
        return None
    xys: list[SList] = [
        item for item in pts.items
        if isinstance(item, SList) and item.head == "xy"
    ]
    if len(xys) < 2:
        return None
    return WireInstance(
        uuid=uuid,
        start_x=_try_float(_atom_at_index(xys[0], 1)),
        start_y=_try_float(_atom_at_index(xys[0], 2)),
        end_x=_try_float(_atom_at_index(xys[1], 1)),
        end_y=_try_float(_atom_at_index(xys[1], 2)),
    )


def _parse_junction(node: SList) -> JunctionInstance | None:
    """Parse ``(junction (at X Y) (diameter D) (color ...) (uuid "..."))``."""
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None
    at_x, at_y = _parse_at(node)
    diameter_txt = _child_atom_text(node, "diameter")
    diameter = _try_float(diameter_txt)
    return JunctionInstance(
        uuid=uuid,
        at_x=at_x,
        at_y=at_y,
        diameter=diameter,
    )


def _parse_no_connect(node: SList) -> NoConnectInstance | None:
    """Parse ``(no_connect (at X Y) (uuid "..."))``."""
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None
    at_x, at_y = _parse_at(node)
    return NoConnectInstance(uuid=uuid, at_x=at_x, at_y=at_y)


__all__ = [
    "IncludeKind",
    "JunctionInstance",
    "NoConnectInstance",
    "SchListWiresInput",
    "SchListWiresOutput",
    "SchListWiresTool",
    "WireInstance",
]

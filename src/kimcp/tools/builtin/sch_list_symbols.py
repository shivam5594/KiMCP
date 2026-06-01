"""sch_list_symbols — enumerate placed symbol instances on a .kicad_sch (M26).

The **first schematic-introspection READ tool**. The mutators in M14-M21
let an LLM build a schematic; this is how it *looks at one*. Every
design-review prompt and every "what's on this sheet?" query starts
here — the LLM queries the placed-symbol list, then decides what to
wire or move next.

Returns, for each top-level ``(symbol ...)`` instance on the sheet,
the fields a design flow actually needs:

* **uuid**       — unique identifier, stable across edits.
* **lib_id**     — library-qualified symbol name (e.g. ``Device:R``).
* **reference**  — designator (``R1``, ``U3``, …) from the visible property.
* **value**      — component value (``10k``, ``100nF``, …).
* **footprint**  — assigned footprint path (``""`` if unassigned).
* **at_x / at_y / angle** — anchor position in mm + rotation in degrees.
* **unit**       — unit number (1 for single-unit symbols).
* **in_bom**     — whether the instance contributes to BOMs.
* **on_board**   — whether the instance appears on the PCB.
* **dnp**        — Do Not Populate flag.

Hierarchical sheets are out of scope for this first ship — the tool
reports only the symbols on the sheet the caller passed. Recursive
enumeration across the sheet tree belongs in a later milestone once we
implement sheet reads.

Status enum:

* **ok**             — symbols enumerated (list may be empty).
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


# -- envelope sub-models ---------------------------------------------------


class SymbolInstance(BaseModel):
    """One placed symbol from the .kicad_sch's top-level symbol list."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="Instance UUID (stable across edits).")
    lib_id: str = Field(
        ..., description="Library-qualified symbol name (e.g. 'Device:R')."
    )
    reference: str = Field(
        ...,
        description=(
            "Reference designator (e.g. 'R1'). May be '?' on an "
            "unannotated schematic."
        ),
    )
    value: str = Field(..., description="Component value (e.g. '10k', '100nF').")
    footprint: str = Field(
        ...,
        description=(
            "Assigned footprint library path. Empty string when no "
            "footprint is assigned yet (pre-CvPcb state)."
        ),
    )
    at_x: float = Field(..., description="Anchor X coordinate in millimetres.")
    at_y: float = Field(..., description="Anchor Y coordinate in millimetres.")
    angle: float = Field(
        ..., description="Rotation angle in degrees (0 / 90 / 180 / 270 typically)."
    )
    unit: int = Field(
        ...,
        description=(
            "Unit number for multi-unit symbols. 1 for single-unit "
            "symbols (resistors, capacitors, power ports, …)."
        ),
    )
    in_bom: bool = Field(
        ...,
        description="Whether this instance contributes to BOMs.",
    )
    on_board: bool = Field(
        ...,
        description="Whether this instance appears on the PCB.",
    )
    dnp: bool = Field(
        ...,
        description="Do Not Populate flag — excluded from fab assembly.",
    )


# -- input / output --------------------------------------------------------


class SchListSymbolsInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    reference_prefix: str | None = Field(
        default=None,
        description=(
            "Filter to instances whose reference starts with this "
            "prefix (e.g. 'R' for every resistor, 'U' for every IC). "
            "Case-sensitive. Null returns every symbol."
        ),
    )
    lib_id_contains: str | None = Field(
        default=None,
        description=(
            "Filter to instances whose lib_id contains this substring "
            "(e.g. 'Capacitor' for every capacitor variant). Case-"
            "sensitive. Null returns every symbol."
        ),
    )


class SchListSymbolsOutput(ToolOutput):
    status: Literal[
        "ok",
        "sch_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    sch_path: str | None = Field(default=None)
    symbols: list[SymbolInstance] = Field(
        default_factory=list,
        description="Placed symbol instances matching the filters, in document order.",
    )
    total: int = Field(
        default=0,
        description="Length of symbols after filtering. Mirrored so callers "
        "don't have to len() the list.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchListSymbolsTool(Tool[SchListSymbolsInput, SchListSymbolsOutput]):
    """Enumerate placed symbol instances on a .kicad_sch."""

    name = "sch_list_symbols"
    version = "0.1.0"
    description = (
        "Enumerate placed symbol instances on a .kicad_sch. Returns one "
        "entry per (symbol ...) on the sheet with uuid, lib_id, reference, "
        "value, footprint, position, rotation, unit, in_bom / on_board / "
        "dnp flags. Supports reference_prefix and lib_id_contains filters."
    )
    input_model = SchListSymbolsInput
    output_model = SchListSymbolsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    async def run(self, input: SchListSymbolsInput) -> SchListSymbolsOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or not sch_path.is_file():
            return SchListSymbolsOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchListSymbolsOutput(
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
            return SchListSymbolsOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchListSymbolsOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        instances: list[SymbolInstance] = []
        for child in doc.root.items:
            if not isinstance(child, SList) or child.head != "symbol":
                continue
            parsed = _parse_symbol_instance(child)
            if parsed is None:
                continue
            if (
                input.reference_prefix is not None
                and not parsed.reference.startswith(input.reference_prefix)
            ):
                continue
            if (
                input.lib_id_contains is not None
                and input.lib_id_contains not in parsed.lib_id
            ):
                continue
            instances.append(parsed)

        return SchListSymbolsOutput(
            status="ok",
            sch_path=str(sch_path),
            symbols=instances,
            total=len(instances),
        )


# -- parse helpers ---------------------------------------------------------


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _child_atom_text(parent: SList, head: str, idx: int = 1) -> str | None:
    """Find a child `(<head> atom ...)` and return its atom at ``idx``."""
    child = parent.find(head)
    if child is None or len(child.items) <= idx:
        return None
    return _atom_text(child.items[idx])


def _atom_at_index(node: SList, idx: int) -> str | None:
    """Return the atom text at ``idx`` of ``node`` if present and atomic."""
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


def _flag(parent: SList, head: str) -> bool | None:
    """Parse a `(flag yes|no)` style child. Returns None when absent."""
    child = parent.find(head)
    if child is None:
        return None
    txt = _atom_at_index(child, 1)
    if txt is None:
        return None
    return txt.lower() in {"yes", "true", "1"}


def _property_value(symbol: SList, name: str) -> str | None:
    """Return the (property "name" "value" ...) value or None."""
    for child in symbol.items:
        if not isinstance(child, SList) or child.head != "property":
            continue
        if len(child.items) < 3:
            continue
        key = _atom_text(child.items[1])
        if key != name:
            continue
        val = _atom_text(child.items[2])
        if val is not None:
            return val
    return None


def _parse_symbol_instance(node: SList) -> SymbolInstance | None:
    """Extract the SymbolInstance envelope from one `(symbol ...)` node.

    Returns None when the node is structurally missing the fields we
    treat as non-optional (uuid, lib_id). KiCAD-written schematics
    always have these; the guard covers hand-crafted or malformed
    fixtures without surfacing them as corruption-level errors.
    """
    lib_id = _child_atom_text(node, "lib_id")
    uuid = _child_atom_text(node, "uuid")
    if lib_id is None or uuid is None:
        return None

    # (at X Y [angle])
    at = node.find("at")
    at_x = at_y = 0.0
    angle = 0.0
    if at is not None:
        try:
            x_txt = _atom_at_index(at, 1)
            y_txt = _atom_at_index(at, 2)
            if x_txt is not None:
                at_x = float(x_txt)
            if y_txt is not None:
                at_y = float(y_txt)
            a_txt = _atom_at_index(at, 3)
            if a_txt is not None:
                angle = float(a_txt)
        except ValueError:
            pass  # defensive: bad numeric → keep defaults

    unit_txt = _child_atom_text(node, "unit")
    try:
        unit = int(unit_txt) if unit_txt is not None else 1
    except ValueError:
        unit = 1

    in_bom = _flag(node, "in_bom")
    on_board = _flag(node, "on_board")
    dnp = _flag(node, "dnp")

    reference = _property_value(node, "Reference") or "?"
    value = _property_value(node, "Value") or ""
    footprint = _property_value(node, "Footprint") or ""

    return SymbolInstance(
        uuid=uuid,
        lib_id=lib_id,
        reference=reference,
        value=value,
        footprint=footprint,
        at_x=at_x,
        at_y=at_y,
        angle=angle,
        unit=unit,
        in_bom=bool(in_bom) if in_bom is not None else True,
        on_board=bool(on_board) if on_board is not None else True,
        dnp=bool(dnp) if dnp is not None else False,
    )


__all__ = [
    "SchListSymbolsInput",
    "SchListSymbolsOutput",
    "SchListSymbolsTool",
    "SymbolInstance",
]

"""sch_get_symbol_pins — return absolute pin coordinates for a placed symbol.

The **missing link** between symbol placement and wire routing. After
placing a symbol via ``sch_add_symbol`` or ``sch_compose``, the caller
knows the symbol's anchor coordinate but NOT where each pin ended up —
pins are offset from the anchor by amounts defined in the lib_symbol
definition, and rotated by the instance's angle.

Without this tool, the AI has to guess wire endpoints, leading to either
broken connections or falling back to labels (the "label spam" problem).
With it, the workflow becomes:

    place symbol → sch_get_symbol_pins("U8") → wire to pin coordinates

For each pin, the tool returns:

* **number**  — pin number as declared in the lib_symbol (e.g. "3").
* **name**    — pin function name (e.g. "VIN", "GND", "FB").
* **x / y**   — absolute position in mm after applying the instance's
                anchor offset + rotation.
* **angle**   — absolute pin angle in degrees (for wire approach
                direction). 0° = rightward, 90° = downward, 180° =
                leftward, 270° = upward.
* **type**    — electrical type (input, output, passive, power_in, etc.).

Coordinate math
---------------

A pin in the lib_symbol has a local ``(at px py pa)`` relative to the
symbol's origin. The instance has an anchor ``(at ax ay ia)`` with
rotation ``ia``. The absolute pin position is:

    abs_x = ax + px*cos(ia) - py*sin(ia)
    abs_y = ay + px*sin(ia) + py*cos(ia)
    abs_angle = (pa + ia) mod 360

This matches how KiCAD internally resolves pin positions for ERC and
netlist generation.

Status enum:

* **ok**               — pins enumerated successfully.
* **sch_not_found**    — path missing or wrong suffix.
* **parse_failed**     — SEXPR parser rejected the file.
* **invalid_schema**   — parseable but top_head isn't ``kicad_sch``.
* **symbol_not_found** — no placed symbol matches the given reference.
* **lib_symbol_not_found** — the placed symbol's lib_id has no matching
                            entry in the schematic's lib_symbols block.

READ classification: no filesystem writes, no subprocess, no snapshot.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import load_sexpr_doc
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol
from kimcp.tools.builtin.sch_list_symbols import (
    _atom_text,
    _child_atom_text,
    _property_value,
)

log = logging.getLogger(__name__)


# -- envelope sub-models ---------------------------------------------------


class PinInfo(BaseModel):
    """One pin's absolute position on the schematic sheet."""

    number: str = Field(..., description="Pin number (e.g. '3', 'A1').")
    name: str = Field(..., description="Pin function name (e.g. 'VIN', 'GND').")
    x: float = Field(..., description="Absolute X position in mm.")
    y: float = Field(..., description="Absolute Y position in mm.")
    angle: float = Field(
        ...,
        description=(
            "Absolute pin angle in degrees. Indicates the direction a "
            "wire should approach from: 0°=from right, 90°=from below, "
            "180°=from left, 270°=from above."
        ),
    )
    electrical_type: str = Field(
        ...,
        description=(
            "Electrical type: input, output, bidirectional, tri_state, "
            "passive, free, unspecified, power_in, power_out, "
            "open_collector, open_emitter, no_connect."
        ),
    )


# -- input / output --------------------------------------------------------


class SchGetSymbolPinsInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    reference: str = Field(
        ...,
        description=(
            "Reference designator of the placed symbol (e.g. 'U8', 'R1'). "
            "Case-sensitive. Must match an existing instance on the sheet."
        ),
    )


class SchGetSymbolPinsOutput(ToolOutput):
    status: Literal[
        "ok",
        "sch_not_found",
        "parse_failed",
        "invalid_schema",
        "symbol_not_found",
        "lib_symbol_not_found",
    ]
    sch_path: str | None = Field(default=None)
    reference: str | None = Field(default=None)
    lib_id: str | None = Field(default=None)
    at_x: float | None = Field(
        default=None, description="Symbol anchor X in mm."
    )
    at_y: float | None = Field(
        default=None, description="Symbol anchor Y in mm."
    )
    angle: float | None = Field(
        default=None, description="Symbol rotation in degrees."
    )
    pins: list[PinInfo] = Field(
        default_factory=list,
        description="Pin positions in absolute schematic coordinates.",
    )
    total: int = Field(
        default=0,
        description="Number of pins returned.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchGetSymbolPinsTool(Tool[SchGetSymbolPinsInput, SchGetSymbolPinsOutput]):
    """Return absolute pin coordinates for a placed symbol instance."""

    name = "sch_get_symbol_pins"
    version = "0.1.0"
    description = (
        "Return absolute (x, y) coordinates for every pin of a placed "
        "symbol instance. Given a reference designator (e.g. 'U8'), reads "
        "the symbol's anchor position and rotation from the schematic, "
        "then resolves each pin's offset from the lib_symbol definition "
        "to compute absolute positions. "
        "USE THIS AFTER PLACING SYMBOLS to get precise wire endpoints. "
        "The workflow is: place symbols → call sch_get_symbol_pins for "
        "each symbol → use the returned (x, y) coordinates as wire "
        "start/end points in sch_add_wire or sch_compose. This eliminates "
        "guesswork and prevents broken connections or label-spam fallback."
    )
    input_model = SchGetSymbolPinsInput
    output_model = SchGetSymbolPinsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    async def run(self, input: SchGetSymbolPinsInput) -> SchGetSymbolPinsOutput:
        # 1. Load and validate the schematic.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or not sch_path.is_file():
            return SchGetSymbolPinsOutput(
                status="sch_not_found",
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchGetSymbolPinsOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a .kicad_sch file (got suffix {sch_path.suffix!r}).",
            )

        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchGetSymbolPinsOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchGetSymbolPinsOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 2. Find the placed symbol instance by reference designator.
        instance = _find_instance_by_reference(doc.root, input.reference)
        if instance is None:
            return SchGetSymbolPinsOutput(
                status="symbol_not_found",
                sch_path=str(sch_path),
                reference=input.reference,
                note=(
                    f"no placed symbol with reference {input.reference!r} "
                    "found on this sheet."
                ),
            )

        # 3. Extract anchor position and rotation.
        lib_id = _child_atom_text(instance, "lib_id") or ""
        at_node = instance.find("at")
        anchor_x = anchor_y = 0.0
        inst_angle = 0.0
        if at_node is not None:
            try:
                x_txt = _atom_text(at_node.items[1]) if len(at_node.items) > 1 else None
                y_txt = _atom_text(at_node.items[2]) if len(at_node.items) > 2 else None
                a_txt = _atom_text(at_node.items[3]) if len(at_node.items) > 3 else None
                if x_txt:
                    anchor_x = float(x_txt)
                if y_txt:
                    anchor_y = float(y_txt)
                if a_txt:
                    inst_angle = float(a_txt)
            except (ValueError, IndexError):
                pass

        # Check for mirror — KiCAD stores mirror as a separate child node
        # within the symbol instance. (mirror x) flips the symbol
        # horizontally, (mirror y) flips vertically. This affects pin
        # coordinate transformation.
        mirror_x = False
        mirror_y = False
        mirror_node = instance.find("mirror")
        if mirror_node is not None:
            for item in mirror_node.items[1:]:
                if isinstance(item, SAtom):
                    if item.text == "x":
                        mirror_x = True
                    elif item.text == "y":
                        mirror_y = True

        # 4. Find the lib_symbol definition.
        lib_symbols = doc.root.find("lib_symbols")
        if lib_symbols is None:
            return SchGetSymbolPinsOutput(
                status="lib_symbol_not_found",
                sch_path=str(sch_path),
                reference=input.reference,
                lib_id=lib_id,
                at_x=anchor_x,
                at_y=anchor_y,
                angle=inst_angle,
                note="schematic has no lib_symbols section.",
            )

        lib_symbol = _find_lib_symbol(lib_symbols, lib_id)
        if lib_symbol is None:
            return SchGetSymbolPinsOutput(
                status="lib_symbol_not_found",
                sch_path=str(sch_path),
                reference=input.reference,
                lib_id=lib_id,
                at_x=anchor_x,
                at_y=anchor_y,
                angle=inst_angle,
                note=(
                    f"lib_id {lib_id!r} not found in lib_symbols. The "
                    "symbol definition may not be embedded."
                ),
            )

        # 5. Extract pins from the lib_symbol and compute absolute positions.
        pins = _extract_pins_with_positions(
            lib_symbol,
            anchor_x=anchor_x,
            anchor_y=anchor_y,
            inst_angle_deg=inst_angle,
            mirror_x=mirror_x,
            mirror_y=mirror_y,
        )

        return SchGetSymbolPinsOutput(
            status="ok",
            sch_path=str(sch_path),
            reference=input.reference,
            lib_id=lib_id,
            at_x=anchor_x,
            at_y=anchor_y,
            angle=inst_angle,
            pins=pins,
            total=len(pins),
        )


# -- helpers ---------------------------------------------------------------


def _find_instance_by_reference(root: SList, reference: str) -> SList | None:
    """Find a top-level ``(symbol ...)`` whose Reference property matches."""
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        ref = _property_value(child, "Reference")
        if ref == reference:
            return child
    return None


def _extract_pins_with_positions(
    lib_symbol: SList,
    *,
    anchor_x: float,
    anchor_y: float,
    inst_angle_deg: float,
    mirror_x: bool,
    mirror_y: bool,
) -> list[PinInfo]:
    """Walk the lib_symbol, extract each pin's local coords, and transform
    to absolute schematic coordinates.

    Pin structure in lib_symbol:
        (symbol "Name_N_M"
          (pin <type> <style>
            (at px py [pa])
            (length L)
            (name "PIN_NAME" ...)
            (number "PIN_NUM" ...)
          )
        )

    Transformation:
        1. Apply mirror (if any): mirror_x flips Y, mirror_y flips X
        2. Rotate by instance angle
        3. Translate by anchor position
    """
    inst_angle_rad = math.radians(inst_angle_deg)
    cos_a = math.cos(inst_angle_rad)
    sin_a = math.sin(inst_angle_rad)

    seen: set[str] = set()
    pins: list[PinInfo] = []

    for node in lib_symbol.walk():
        if not isinstance(node, SList) or node.head != "pin":
            continue

        # Extract pin number.
        number_node = node.find("number")
        if number_node is None or len(number_node.items) < 2:
            continue
        number_atom = number_node.items[1]
        if not isinstance(number_atom, SAtom):
            continue
        pin_number = number_atom.text

        # Deduplicate (multi-unit symbols repeat pins).
        if pin_number in seen:
            continue
        seen.add(pin_number)

        # Extract pin name.
        name_node = node.find("name")
        pin_name = ""
        if name_node is not None and len(name_node.items) >= 2:
            name_atom = name_node.items[1]
            if isinstance(name_atom, SAtom):
                pin_name = name_atom.text

        # Extract electrical type (second atom after "pin" head).
        electrical_type = ""
        if len(node.items) >= 2 and isinstance(node.items[1], SAtom):
            electrical_type = node.items[1].text

        # Extract local pin position (at px py [pa]).
        at_node = node.find("at")
        px = py = 0.0
        pin_angle_local = 0.0
        if at_node is not None:
            try:
                px_txt = _atom_text(at_node.items[1]) if len(at_node.items) > 1 else None
                py_txt = _atom_text(at_node.items[2]) if len(at_node.items) > 2 else None
                pa_txt = _atom_text(at_node.items[3]) if len(at_node.items) > 3 else None
                if px_txt:
                    px = float(px_txt)
                if py_txt:
                    py = float(py_txt)
                if pa_txt:
                    pin_angle_local = float(pa_txt)
            except (ValueError, IndexError):
                pass

        # Apply mirror before rotation.
        if mirror_x:
            py = -py
            # Mirror x flips the Y axis, which also affects pin angle.
            pin_angle_local = (-pin_angle_local) % 360
        if mirror_y:
            px = -px
            pin_angle_local = (180 - pin_angle_local) % 360

        # Rotate by instance angle and translate by anchor.
        abs_x = anchor_x + px * cos_a - py * sin_a
        abs_y = anchor_y + px * sin_a + py * cos_a

        # Round to KiCAD's display precision (2 decimal places in mm).
        abs_x = round(abs_x, 2)
        abs_y = round(abs_y, 2)

        abs_angle = (pin_angle_local + inst_angle_deg) % 360

        pins.append(PinInfo(
            number=pin_number,
            name=pin_name,
            x=abs_x,
            y=abs_y,
            angle=abs_angle,
            electrical_type=electrical_type,
        ))

    return pins


__all__ = [
    "PinInfo",
    "SchGetSymbolPinsInput",
    "SchGetSymbolPinsOutput",
    "SchGetSymbolPinsTool",
]

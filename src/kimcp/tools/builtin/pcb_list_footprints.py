"""pcb_list_footprints — enumerate footprint instances on a .kicad_pcb.

The PCB-side companion to ``sch_list_symbols``. Walks the top-level
``(footprint ...)`` children of a ``.kicad_pcb`` s-expression and
returns one entry per instance with the fields an LLM actually needs
to reason about placement and routing:

* **lib_ref** — the fully-qualified ``LibName:FootprintName`` (e.g.
  ``Resistor_SMD:R_0603_1608Metric``). This is the footprint's head
  atom in the file.
* **reference** — the designator from the Reference property
  (``R1``, ``U3``, ``C14``). Primary sort key.
* **value** — the Value property (e.g. ``10k``, ``NE555``).
* **layer** — ``F.Cu`` for top-side, ``B.Cu`` for bottom-side (and
  occasionally other signal layers on inner-copper assemblies). KiCAD
  writes a single layer per footprint — the footprint's origin layer.
* **at_x / at_y / angle** — placement.
* **uuid** — stable instance identity across edits.

Filters compose (AND semantics): combining ``layer="top"`` and
``ref_contains="R"`` returns top-side resistors. Rotation / position
range filters are out of scope for v1 — they'd need a numeric-range
DSL and nobody's asked for them yet.

Why SEXPR-direct and not IPC: the IPC board API exposes a richer
``BoardItem`` model, but for read-only enumeration the parse cost is
low and avoids a running-KiCAD dependency. Same choice as
``sch_list_symbols`` for the same reason.

Status enum:

* **ok**             — footprints enumerated (list may be empty).
* **pcb_not_found**  — path missing / wrong suffix.
* **parse_failed**   — SEXPR parser rejected the file.
* **invalid_schema** — parseable but top_head isn't ``kicad_pcb``.

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
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)


# Layer filter vocabulary. "top" / "bottom" are user-friendly aliases;
# the canonical KiCAD names are F.Cu / B.Cu but operators think in
# top/bottom so we accept both. Pass-through for any other explicit
# layer name lets rare inner-copper footprints still filter cleanly.
LayerFilter = Literal["top", "bottom"] | str
_LAYER_ALIASES: dict[str, str] = {"top": "F.Cu", "bottom": "B.Cu"}


# -- envelope sub-models ---------------------------------------------------


class FootprintInstance(BaseModel):
    """One footprint from the .kicad_pcb's top-level footprint list."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="Footprint UUID (stable across edits).")
    lib_ref: str = Field(
        ...,
        description=(
            "Fully-qualified library ref ``LibName:FootprintName``, e.g. "
            "``Resistor_SMD:R_0603_1608Metric``."
        ),
    )
    reference: str = Field(
        ..., description="Reference designator from the Reference property."
    )
    value: str = Field(..., description="Value property (component value).")
    layer: str = Field(
        ...,
        description=(
            "Origin layer — ``F.Cu`` (top) or ``B.Cu`` (bottom) for most "
            "parts."
        ),
    )
    at_x: float = Field(..., description="Origin X in millimetres.")
    at_y: float = Field(..., description="Origin Y in millimetres.")
    angle: float = Field(..., description="Rotation angle in degrees.")


# -- input / output --------------------------------------------------------


class PcbListFootprintsInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    layer: str | None = Field(
        default=None,
        description=(
            "Filter by origin layer. Accepts 'top' / 'bottom' (aliases for "
            "'F.Cu' / 'B.Cu') or any explicit KiCAD layer name. Null "
            "returns every footprint regardless of layer."
        ),
    )
    ref_contains: str | None = Field(
        default=None,
        description=(
            "Filter to footprints whose reference contains this substring "
            "(e.g. 'R' for every resistor, 'U1' for U1 through U1x). "
            "Case-sensitive. Null returns every reference."
        ),
    )
    value_contains: str | None = Field(
        default=None,
        description=(
            "Filter by substring-match on the Value property. Case-sensitive."
        ),
    )
    lib_contains: str | None = Field(
        default=None,
        description=(
            "Filter by substring-match on the lib_ref (``LibName:FootprintName``). "
            "Useful for 'all 0603 parts' style queries."
        ),
    )


class PcbListFootprintsOutput(ToolOutput):
    status: Literal[
        "ok",
        "pcb_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    pcb_path: str | None = Field(default=None)
    footprints: list[FootprintInstance] = Field(
        default_factory=list,
        description="Footprints after filtering, sorted by reference designator.",
    )
    total: int = Field(
        default=0,
        description="Count of footprints after filtering.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class PcbListFootprintsTool(Tool[PcbListFootprintsInput, PcbListFootprintsOutput]):
    """Enumerate footprint instances on a .kicad_pcb."""

    name = "pcb_list_footprints"
    version = "0.1.0"
    description = (
        "Enumerate footprint instances on a .kicad_pcb. Returns lib_ref, "
        "reference, value, layer, origin (x/y/angle), and uuid for each. "
        "Supports layer ('top' / 'bottom' / explicit name), ref, value, and "
        "lib substring filters."
    )
    input_model = PcbListFootprintsInput
    output_model = PcbListFootprintsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(self, input: PcbListFootprintsInput) -> PcbListFootprintsOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists() or not pcb_path.is_file():
            return PcbListFootprintsOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbListFootprintsOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r})."
                ),
            )

        try:
            doc = SexprDocument.from_path(pcb_path)
        except SexprParseError as exc:
            return PcbListFootprintsOutput(
                status="parse_failed",
                pcb_path=str(pcb_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_pcb":
            return PcbListFootprintsOutput(
                status="invalid_schema",
                pcb_path=str(pcb_path),
                note=(
                    f"expected top-level '(kicad_pcb ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Resolve layer alias once so the inner loop compares canonical
        # KiCAD names against the field verbatim.
        layer_filter = (
            _LAYER_ALIASES.get(input.layer, input.layer) if input.layer else None
        )

        footprints: list[FootprintInstance] = []
        for child in doc.root.items:
            if not isinstance(child, SList) or child.head != "footprint":
                continue
            parsed = _parse_footprint(child)
            if parsed is None:
                continue
            if layer_filter is not None and parsed.layer != layer_filter:
                continue
            if input.ref_contains is not None and input.ref_contains not in parsed.reference:
                continue
            if (
                input.value_contains is not None
                and input.value_contains not in parsed.value
            ):
                continue
            if (
                input.lib_contains is not None
                and input.lib_contains not in parsed.lib_ref
            ):
                continue
            footprints.append(parsed)

        # Sort by reference for deterministic output. Pure lex order
        # (not natural) — matches how every other ``_list`` tool
        # orders; a natural-sort helper would be nice but isn't load-
        # bearing for a read tool.
        footprints.sort(key=lambda f: f.reference)

        return PcbListFootprintsOutput(
            status="ok",
            pcb_path=str(pcb_path),
            footprints=footprints,
            total=len(footprints),
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


def _find_property(node: SList, key: str) -> str | None:
    """Return the string value of ``(property "<key>" "<value>" ...)``.

    KiCAD PCB property nodes are positional: ``[head, key_atom,
    value_atom, ...]``. ``value`` is stored as a quoted atom so an
    empty string survives round-tripping.
    """
    for prop in node.find_all("property"):
        if len(prop.items) < 3:
            continue
        key_atom = prop.items[1]
        if not isinstance(key_atom, SAtom) or key_atom.text != key:
            continue
        value_atom = prop.items[2]
        if isinstance(value_atom, SAtom):
            return value_atom.text
    return None


def _parse_footprint(node: SList) -> FootprintInstance | None:
    """Extract a FootprintInstance from one ``(footprint ...)`` node.

    Returns None when required fields (lib_ref, uuid, reference) are
    missing. KiCAD-written boards always have them; we guard against
    hand-crafted fixtures without promoting them to
    corruption-level errors.
    """
    # lib_ref is positional at [1] — the quoted atom right after the
    # head "footprint".
    lib_ref = _atom_at_index(node, 1)
    uuid = _child_atom_text(node, "uuid")
    if lib_ref is None or uuid is None:
        return None

    reference = _find_property(node, "Reference")
    if reference is None:
        return None

    # Value can legitimately be an empty string (e.g. a fiducial marker
    # or silk-only part). Preserve that rather than treating missing
    # and empty the same.
    value = _find_property(node, "Value") or ""

    # Layer is a required child — every KiCAD footprint has one. Fall
    # back to empty string on absence so the layer filter can still
    # reject without blowing up the whole listing.
    layer = _child_atom_text(node, "layer") or ""

    at_node = node.find("at")
    at_x = at_y = 0.0
    angle = 0.0
    if at_node is not None:
        try:
            x_txt = _atom_at_index(at_node, 1)
            y_txt = _atom_at_index(at_node, 2)
            if x_txt is not None:
                at_x = float(x_txt)
            if y_txt is not None:
                at_y = float(y_txt)
            a_txt = _atom_at_index(at_node, 3)
            if a_txt is not None:
                angle = float(a_txt)
        except ValueError:
            pass  # defensive: bad numeric → keep defaults

    return FootprintInstance(
        uuid=uuid,
        lib_ref=lib_ref,
        reference=reference,
        value=value,
        layer=layer,
        at_x=at_x,
        at_y=at_y,
        angle=angle,
    )


__all__ = [
    "FootprintInstance",
    "LayerFilter",
    "PcbListFootprintsInput",
    "PcbListFootprintsOutput",
    "PcbListFootprintsTool",
]

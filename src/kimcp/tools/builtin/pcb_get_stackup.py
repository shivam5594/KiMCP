"""pcb_get_stackup — read the layer stack of a .kicad_pcb.

Answers the physical-construction questions that come before any
routing / DRC discussion: "how many copper layers?", "what's the
board thickness?", "is there a defined dielectric between F.Cu and
B.Cu and what's its epsilon_r?".

Two sources in the .kicad_pcb contribute:

* **``(layers ...)`` at the top level** — the canonical layer roster
  KiCAD uses for every PCB. Always present, even on a fresh board.
  Each entry is ``(N "<name>" <type> ["<user_name>"])`` — e.g.
  ``(0 "F.Cu" signal)``, ``(32 "B.Adhes" user "B.Adhesive")``. This
  is the "what layers exist" view.

* **``(setup (stackup ...))``** — the physical layer stack with
  thicknesses, materials, dielectric constants, and mask/silk colors.
  Populated when the user opens Board Setup → Board Stackup and saves.
  Optional: boards that never customize the stackup simply omit the
  ``(stackup ...)`` subnode, which is legitimate — KiCAD falls back
  to a canonical default. We report this as ``has_explicit_stackup=
  False`` rather than an error.

What's in the stackup entries:

* Copper layers (``type="copper"``) carry a thickness (typically 35 µm
  = 0.035 mm for 1 oz copper).
* Dielectric entries (``type="core"`` or ``"prepreg"``) additionally
  carry ``material``, ``epsilon_r``, and ``loss_tangent`` — the
  impedance-modeling inputs.
* Mask / silk / paste entries carry ``color`` and ``thickness``.

Total thickness: sum of every stackup entry's thickness, not just
copper. This matches what the fab needs to quote — the total
physical board thickness including mask and silk layers.

Status enum:

* **ok**             — read succeeded. May have ``has_explicit_stackup
                        =False`` if only the canonical ``(layers ...)``
                        section is present.
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


# -- envelope sub-models ---------------------------------------------------


class LayerDefinition(BaseModel):
    """One entry from the canonical ``(layers ...)`` section.

    Every .kicad_pcb has this section; it's the roster of layers the
    board editor offers. Copper layers are 0..31 with ``F.Cu=0`` and
    ``B.Cu=31``; technical and user layers are 32+.
    """

    model_config = ConfigDict(extra="allow")

    number: int = Field(
        ..., description="KiCAD internal layer number (0..31 for copper, 32+ for tech/user)."
    )
    name: str = Field(..., description="Canonical layer name (e.g. ``F.Cu``, ``B.SilkS``).")
    type: str = Field(
        ...,
        description=(
            "Layer class — ``'signal'`` (normal copper), ``'power'`` (plane), "
            "``'mixed'``, ``'jumper'``, or ``'user'`` (non-copper tech layer)."
        ),
    )
    user_name: str | None = Field(
        default=None,
        description=(
            "User-visible rename from the Board Setup UI. Null when the layer "
            "uses its canonical name."
        ),
    )


class StackupLayer(BaseModel):
    """One physical layer in the explicit stackup.

    Fields populate differently by type:

    * Copper (``type='copper'``): ``name`` + ``thickness``.
    * Dielectric (``type='core'`` / ``'prepreg'``): ``name`` (usually
      ``"dielectric N"``) + ``thickness`` + ``material`` + ``epsilon_r``
      + ``loss_tangent``.
    * Mask / silk / paste: ``name`` + ``thickness`` + ``color``.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(
        ...,
        description=(
            "Physical layer name — canonical layer (e.g. ``F.Cu``) or "
            "``'dielectric N'`` for substrate entries."
        ),
    )
    type: str = Field(
        ...,
        description=(
            "Stackup type — ``'copper'``, ``'core'``, ``'prepreg'``, "
            "``'Top Solder Mask'``, ``'Top Silk Screen'``, etc."
        ),
    )
    thickness: float = Field(
        default=0.0,
        description="Layer thickness in millimetres. 0 when unspecified.",
    )
    material: str | None = Field(
        default=None,
        description="Dielectric material name (e.g. ``'FR4'``). Dielectrics only.",
    )
    color: str | None = Field(
        default=None,
        description="Display/fab color — applies to mask / silk / paste layers.",
    )
    epsilon_r: float | None = Field(
        default=None,
        description="Relative permittivity (dielectric constant). Dielectrics only.",
    )
    loss_tangent: float | None = Field(
        default=None,
        description="Dielectric loss tangent (dissipation factor). Dielectrics only.",
    )


# -- input / output --------------------------------------------------------


class PcbGetStackupInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )


class PcbGetStackupOutput(ToolOutput):
    status: Literal[
        "ok",
        "pcb_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    pcb_path: str | None = Field(default=None)
    layers: list[LayerDefinition] = Field(
        default_factory=list,
        description=(
            "Canonical ``(layers ...)`` roster — every layer the editor knows "
            "about, in KiCAD layer-number order."
        ),
    )
    copper_layer_count: int = Field(
        default=0,
        description=(
            "Count of copper layers (``type == 'signal'``, ``'power'``, "
            "``'mixed'``, or ``'jumper'``). The number you quote to a fab."
        ),
    )
    total_layer_count: int = Field(
        default=0,
        description="Count of all layers in ``(layers ...)`` including tech/user.",
    )
    has_explicit_stackup: bool = Field(
        default=False,
        description=(
            "True when ``(setup (stackup ...))`` is present in the file. False "
            "boards rely on KiCAD's default stackup and ``stackup`` is empty."
        ),
    )
    stackup: list[StackupLayer] = Field(
        default_factory=list,
        description="Physical layer stack from top to bottom. Empty when no explicit stackup.",
    )
    total_thickness_mm: float = Field(
        default=0.0,
        description=(
            "Sum of every ``stackup`` entry's thickness. Matches the board "
            "thickness spec most fabs quote in."
        ),
    )
    copper_finish: str | None = Field(
        default=None,
        description=(
            "Copper surface finish (e.g. ``'HASL'``, ``'ENIG'``). Null when "
            "no explicit stackup or no finish specified."
        ),
    )
    dielectric_constraints: bool | None = Field(
        default=None,
        description=(
            "Whether impedance-controlled dielectric constraints are enabled. "
            "Null when no explicit stackup."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class PcbGetStackupTool(Tool[PcbGetStackupInput, PcbGetStackupOutput]):
    """Read the layer stack of a .kicad_pcb."""

    name = "pcb_get_stackup"
    version = "0.1.0"
    description = (
        "Read the layer stackup of a .kicad_pcb: canonical (layers ...) roster "
        "plus any explicit (setup (stackup ...)) with thicknesses, materials, "
        "epsilon_r / loss_tangent, and colors. Reports total thickness and "
        "copper-layer count."
    )
    input_model = PcbGetStackupInput
    output_model = PcbGetStackupOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(self, input: PcbGetStackupInput) -> PcbGetStackupOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists() or not pcb_path.is_file():
            return PcbGetStackupOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbGetStackupOutput(
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
            return PcbGetStackupOutput(
                status="parse_failed",
                pcb_path=str(pcb_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_pcb":
            return PcbGetStackupOutput(
                status="invalid_schema",
                pcb_path=str(pcb_path),
                note=(
                    f"expected top-level '(kicad_pcb ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Canonical layer roster — always present in a KiCAD-written file.
        layers = _parse_layers(doc.root)
        copper_layer_count = sum(
            1 for layer in layers
            if layer.type in _COPPER_LAYER_TYPES
        )

        # Explicit stackup lives under (setup (stackup ...)) and is
        # optional. Absence is a perfectly valid configuration.
        stackup_layers: list[StackupLayer] = []
        total_thickness = 0.0
        copper_finish: str | None = None
        dielectric_constraints: bool | None = None
        has_stackup = False

        setup = doc.root.find("setup")
        if setup is not None:
            stackup_node = setup.find("stackup")
            if stackup_node is not None:
                has_stackup = True
                stackup_layers, copper_finish, dielectric_constraints = (
                    _parse_stackup(stackup_node)
                )
                total_thickness = sum(sl.thickness for sl in stackup_layers)

        return PcbGetStackupOutput(
            status="ok",
            pcb_path=str(pcb_path),
            layers=layers,
            copper_layer_count=copper_layer_count,
            total_layer_count=len(layers),
            has_explicit_stackup=has_stackup,
            stackup=stackup_layers,
            total_thickness_mm=total_thickness,
            copper_finish=copper_finish,
            dielectric_constraints=dielectric_constraints,
        )


# -- parse helpers ---------------------------------------------------------


# Copper-flavored layer types — any of these count toward the "how
# many copper layers does this board have" number. ``user`` /
# ``signal``-adjacent non-copper layers are excluded.
_COPPER_LAYER_TYPES: frozenset[str] = frozenset(
    {"signal", "power", "mixed", "jumper"}
)


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _child_atom_text(parent: SList, head: str, idx: int = 1) -> str | None:
    child = parent.find(head)
    if child is None or len(child.items) <= idx:
        return None
    return _atom_text(child.items[idx])


def _float_child(parent: SList, head: str, idx: int = 1) -> float | None:
    txt = _child_atom_text(parent, head, idx)
    if txt is None:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _parse_layers(root: SList) -> list[LayerDefinition]:
    """Extract the canonical layer roster from ``(layers ...)``.

    Each entry is ``(<number> "<name>" <type> ["<user_name>"])``.
    KiCAD writes them in layer-number order; we preserve that.
    Malformed entries (too few fields, non-integer number) are
    dropped rather than raised — fixture tolerance.
    """
    layers_node = root.find("layers")
    if layers_node is None:
        return []

    out: list[LayerDefinition] = []
    for child in layers_node.items[1:]:
        if not isinstance(child, SList):
            continue
        if len(child.items) < 3:
            continue
        # The head of each layer entry IS the number (positional), so
        # items[0] is the number atom, items[1] is the name, items[2]
        # is the type. Careful — the SList's .head is items[0].
        head_atom = child.items[0]
        if not isinstance(head_atom, SAtom):
            continue
        try:
            number = int(head_atom.text)
        except ValueError:
            continue
        name_atom = child.items[1]
        type_atom = child.items[2]
        if not isinstance(name_atom, SAtom) or not isinstance(type_atom, SAtom):
            continue
        user_name: str | None = None
        if len(child.items) >= 4:
            un_atom = child.items[3]
            if isinstance(un_atom, SAtom):
                user_name = un_atom.text
        out.append(
            LayerDefinition(
                number=number,
                name=name_atom.text,
                type=type_atom.text,
                user_name=user_name,
            )
        )
    return out


def _parse_stackup(
    stackup_node: SList,
) -> tuple[list[StackupLayer], str | None, bool | None]:
    """Extract the physical stackup + finish + constraints flag.

    Stackup layers are ``(layer "<name>" (type "<t>") [(thickness n)]
    [(material "m")] [(color "c")] [(epsilon_r n)] [(loss_tangent n)])``.
    Order in the file is top-to-bottom; we preserve that.
    """
    out: list[StackupLayer] = []
    copper_finish: str | None = None
    dielectric_constraints: bool | None = None

    for child in stackup_node.items[1:]:
        if not isinstance(child, SList):
            continue
        head = child.head or ""
        if head == "layer":
            parsed = _parse_stackup_layer(child)
            if parsed is not None:
                out.append(parsed)
        elif head == "copper_finish":
            copper_finish = _atom_at_index(child, 1)
        elif head == "dielectric_constraints":
            flag = _atom_at_index(child, 1)
            if flag is not None:
                dielectric_constraints = flag == "yes"

    return out, copper_finish, dielectric_constraints


def _parse_stackup_layer(node: SList) -> StackupLayer | None:
    """Build one ``StackupLayer`` or None for malformed input.

    Requires at least ``name`` and ``type`` — everything else is
    optional metadata that fills in when KiCAD has it.
    """
    if len(node.items) < 2:
        return None
    name_atom = node.items[1]
    if not isinstance(name_atom, SAtom):
        return None
    name = name_atom.text

    type_str = _child_atom_text(node, "type")
    if type_str is None:
        return None

    thickness = _float_child(node, "thickness") or 0.0
    material = _child_atom_text(node, "material")
    color = _child_atom_text(node, "color")
    epsilon_r = _float_child(node, "epsilon_r")
    loss_tangent = _float_child(node, "loss_tangent")

    return StackupLayer(
        name=name,
        type=type_str,
        thickness=thickness,
        material=material,
        color=color,
        epsilon_r=epsilon_r,
        loss_tangent=loss_tangent,
    )


def _atom_at_index(node: SList, idx: int) -> str | None:
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


__all__ = [
    "LayerDefinition",
    "PcbGetStackupInput",
    "PcbGetStackupOutput",
    "PcbGetStackupTool",
    "StackupLayer",
]

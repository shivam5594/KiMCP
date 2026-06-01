"""lib_add_footprint — author a new footprint in a ``.pretty`` library (M45).

Library-authoring twin of ``lib_add_symbol``. Different storage
shape — KiCAD keeps footprints as *one ``.kicad_mod`` file per
footprint* inside a ``.pretty/`` directory, not a single library
file. So the filesystem layout differs:

* ``lib_add_symbol``  →  edits a multi-entry ``.kicad_sym`` file in place.
* ``lib_add_footprint`` →  creates (or overwrites) ``<name>.kicad_mod``
  inside ``<library>.pretty/``. No per-library index file to keep in
  sync.

Scope of the first ship
-----------------------

* **Pads + outline only.** Pads (SMD or through-hole), courtyard,
  silkscreen, and fabrication outlines. No zones, no 3D model stubs
  (M47 handles those), no solder-mask relief tweaks, no complex pad
  shapes beyond KiCAD's standard enumeration.
* **Single-layer copper on SMD pads.** ``F.Cu`` or ``B.Cu``; dual-side
  SMD isn't something you'd ever want. Through-hole pads get the
  canonical ``*.Cu`` / ``*.Mask`` layer set automatically.
* **No implicit graphics.** Silkscreen and courtyard lines must be
  specified by the caller. We don't auto-draw anything — the KiCAD
  footprint editor's auto-fill is a UX feature, not a format feature.

Conflict policy
---------------

If ``<name>.kicad_mod`` already exists in the directory, we return
``footprint_exists`` unless the caller sets ``overwrite=True``. Same
opt-in semantics as ``lib_add_symbol`` for the same reasons.

Bootstrap
---------

If the ``.pretty`` directory doesn't exist we create it on write.
Unlike a ``.kicad_sym`` file, a ``.pretty`` directory has no
accompanying metadata — its existence + a collection of
``.kicad_mod`` files is the library.

Status enum
-----------

* **ok**                     — footprint written.
* **dry_run**                — caller passed ``dry_run=True``.
* **invalid_input**          — empty name, non-existent pad layer, etc.
* **footprint_exists**       — file present and ``overwrite`` is False.
* **write_failed**           — snapshot / save / round-trip raised.

Backend: SEXPR, required.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import (
    atom,
    effects_node,
    flag_node,
    fmt_mm,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


# KiCAD 10 format stamps for a fresh ``.kicad_mod``. Match the
# GUI's footprint editor on "File → New Footprint".
_KICAD_MOD_VERSION = "20241229"
_KICAD_MOD_GENERATOR = "pcbnew"
_KICAD_MOD_GENERATOR_VERSION = "9.0"

# Standard pad layer sets. Through-hole pads span all copper layers +
# the two solder-mask layers (front + back). SMD pads override with
# just the front-copper trio — callers can flip to back via pad.layer.
_PAD_LAYERS_THT = ("*.Cu", "*.Mask")
_PAD_LAYERS_SMD_FRONT = ("F.Cu", "F.Paste", "F.Mask")
_PAD_LAYERS_SMD_BACK = ("B.Cu", "B.Paste", "B.Mask")

_VALID_PAD_TYPES = frozenset({"smd", "thru_hole", "np_thru_hole", "connect"})
_VALID_PAD_SHAPES = frozenset(
    {"circle", "rect", "roundrect", "oval", "trapezoid", "custom"}
)
_VALID_FP_ATTRS = frozenset(
    {
        "smd",
        "through_hole",
        "exclude_from_pos_files",
        "exclude_from_bom",
        "allow_missing_courtyard",
        "allow_soldermask_bridges",
        "dnp",
    }
)

# Default "Reference" / "Value" property positions (mm) — roughly what
# the footprint wizard drops them at. Callers can override by editing
# post-hoc in the footprint editor.
_REF_DEFAULT_Y = -2.0
_VAL_DEFAULT_Y = 2.0


# -- input sub-models ------------------------------------------------------


class FootprintPad(BaseModel):
    """One pad on the new footprint.

    ``number`` is the pad's pin identifier, matched against symbol pin
    numbers at netlist time. SMD pads use ``pad_type='smd'``; plated
    through-hole uses ``'thru_hole'`` and requires ``drill`` > 0.
    Non-plated holes use ``'np_thru_hole'``.
    """

    model_config = ConfigDict(extra="forbid")

    number: str = Field(
        ...,
        description=(
            "Pad number matched against symbol pin numbers. '0' or '' "
            "marks a mechanical/unconnected pad (mounting hole, "
            "thermal pad)."
        ),
    )
    pad_type: str = Field(
        default="smd",
        description=(
            "Pad family. One of: "
            + ", ".join(sorted(_VALID_PAD_TYPES))
            + ". 'smd' = surface mount, 'thru_hole' = plated through, "
            "'np_thru_hole' = unplated (mechanical) through-hole."
        ),
    )
    shape: str = Field(
        default="roundrect",
        description=(
            "Pad shape. One of: "
            + ", ".join(sorted(_VALID_PAD_SHAPES))
            + ". 'roundrect' is the modern SMD default; 'rect' matches "
            "older libraries; 'circle' + 'oval' are common for "
            "through-hole."
        ),
    )
    x: float = Field(..., description="Pad centre X in mm, relative to footprint origin.")
    y: float = Field(..., description="Pad centre Y in mm, relative to footprint origin.")
    size_w: float = Field(
        ..., gt=0.0, description="Pad width (X extent) in mm."
    )
    size_h: float = Field(
        ..., gt=0.0, description="Pad height (Y extent) in mm."
    )
    drill: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Drill diameter in mm — required for through-hole; 0 for "
            "SMD (then ignored). Rounded holes only; slotted pads aren't "
            "supported by this first ship."
        ),
    )
    layer: str = Field(
        default="F.Cu",
        description=(
            "Copper layer for SMD pads. 'F.Cu' = front (default), "
            "'B.Cu' = back. Through-hole pads ignore this and land on "
            "'*.Cu' automatically."
        ),
    )

    @field_validator("pad_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in _VALID_PAD_TYPES:
            raise ValueError(
                f"unknown pad_type {v!r}; must be one of "
                f"{sorted(_VALID_PAD_TYPES)}"
            )
        return v

    @field_validator("shape")
    @classmethod
    def _check_shape(cls, v: str) -> str:
        if v not in _VALID_PAD_SHAPES:
            raise ValueError(
                f"unknown pad shape {v!r}; must be one of "
                f"{sorted(_VALID_PAD_SHAPES)}"
            )
        return v


class FootprintLine(BaseModel):
    """One line segment on silkscreen / courtyard / fab layers."""

    model_config = ConfigDict(extra="forbid")

    start_x: float = Field(..., description="Start X in mm.")
    start_y: float = Field(..., description="Start Y in mm.")
    end_x: float = Field(..., description="End X in mm.")
    end_y: float = Field(..., description="End Y in mm.")
    layer: str = Field(
        default="F.SilkS",
        description=(
            "KiCAD layer name. Common choices: 'F.SilkS' / 'B.SilkS' for "
            "silkscreen outlines, 'F.CrtYd' / 'B.CrtYd' for courtyards, "
            "'F.Fab' / 'B.Fab' for fabrication references."
        ),
    )
    width: float = Field(
        default=0.12,
        gt=0.0,
        description=(
            "Stroke width in mm. 0.12 is the KiCAD-recommended silkscreen "
            "minimum; courtyards typically use 0.05."
        ),
    )


# -- input / output --------------------------------------------------------


class LibAddFootprintInput(BaseModel):
    lib_path: Path = Field(
        ...,
        description=(
            "Path to the .pretty/ library directory (e.g. "
            "'project/custom.pretty'). Created on write if missing."
        ),
    )
    footprint_name: str = Field(
        ...,
        description=(
            "Footprint name — becomes the filename stem "
            "('<name>.kicad_mod') AND the ``(footprint \"name\" ...)`` "
            "head atom. Must be non-empty."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Human-readable description for the footprint picker. "
            "Stored as the ``(descr ...)`` child."
        ),
    )
    tags: str = Field(
        default="",
        description=(
            "Space-separated keyword string for search "
            "('SMD 0603 resistor'). Stored as ``(tags ...)``."
        ),
    )
    reference: str = Field(
        default="REF**",
        description=(
            "Default Reference property value. 'REF**' is the KiCAD "
            "placeholder that gets replaced with the annotated "
            "designator (e.g. 'R42') when the footprint is used."
        ),
    )
    value: str = Field(
        default="",
        description=(
            "Default Value property. Empty falls back to ``footprint_name``."
        ),
    )
    layer: str = Field(
        default="F.Cu",
        description=(
            "Primary layer for the footprint. Almost always 'F.Cu' "
            "(front-side) — 'B.Cu' signals a back-side-only part."
        ),
    )
    attributes: list[str] = Field(
        default_factory=list,
        description=(
            "Footprint ``attr`` flags. Typical: ['smd'] for an SMD "
            "component, ['through_hole'] for THT, optionally "
            "'exclude_from_bom' / 'exclude_from_pos_files' for "
            "non-populated references."
        ),
    )
    pads: list[FootprintPad] = Field(
        default_factory=list,
        description=(
            "Pad table. Empty is accepted (a graphics-only footprint — "
            "logo, fiducial, tooling hole) but unusual for functional "
            "parts."
        ),
    )
    lines: list[FootprintLine] = Field(
        default_factory=list,
        description=(
            "Graphic-line table. Each entry becomes an ``(fp_line ...)`` "
            "child. Use for silkscreen outlines, courtyards, fab marks."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "If True and <name>.kicad_mod already exists, replace it. "
            "Defaults to False — opt in explicitly to avoid accidental "
            "clobbering of a hand-tuned footprint."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned write without touching the filesystem.",
    )

    @field_validator("attributes")
    @classmethod
    def _check_attributes(cls, v: list[str]) -> list[str]:
        for a in v:
            if a not in _VALID_FP_ATTRS:
                raise ValueError(
                    f"unknown footprint attribute {a!r}; must be one of "
                    f"{sorted(_VALID_FP_ATTRS)}"
                )
        return v


class LibAddFootprintOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "invalid_input",
        "footprint_exists",
        "write_failed",
    ]
    lib_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .pretty/ directory.",
    )
    footprint_path: str | None = Field(
        default=None,
        description="Absolute path to the written .kicad_mod file.",
    )
    footprint_name: str | None = Field(default=None)
    pad_count: int = Field(default=0)
    line_count: int = Field(default=0)
    created_library: bool = Field(
        default=False,
        description="True when the .pretty directory was created by this call.",
    )
    overwrote: bool = Field(
        default=False,
        description="True when an existing .kicad_mod was replaced.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibAddFootprintTool(Tool[LibAddFootprintInput, LibAddFootprintOutput]):
    """Author a new footprint in a KiCAD .pretty library directory."""

    name = "lib_add_footprint"
    version = "0.1.0"
    description = (
        "Create a new KiCAD footprint (.kicad_mod) in a .pretty/ library "
        "directory. Writes pads, silkscreen / courtyard / fab lines, "
        "Reference + Value properties, footprint attributes. Creates "
        "the .pretty/ directory on write if missing. Conflicts return "
        "footprint_exists unless overwrite=True. Supports dry_run; "
        "snapshots before write per ADR-0008."
    )
    input_model = LibAddFootprintInput
    output_model = LibAddFootprintOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    async def run(self, input: LibAddFootprintInput) -> LibAddFootprintOutput:
        # 1. Input sanity.
        if not input.footprint_name:
            return LibAddFootprintOutput(
                status="invalid_input",
                note="footprint_name must be non-empty.",
            )
        # Pad-number uniqueness within the footprint. KiCAD tolerates
        # duplicates (multiple pads on the same net, e.g. power pad +
        # thermal tab on pad 0), so we only warn about duplicates
        # *among numbered pads*. Two '0' pads are normal; two '1' pads
        # are almost certainly a bug.
        seen: set[str] = set()
        for pad in input.pads:
            if pad.number in ("", "0"):
                continue
            if pad.number in seen:
                return LibAddFootprintOutput(
                    status="invalid_input",
                    note=(
                        f"duplicate pad number {pad.number!r}; if this "
                        "is intentional (parallel thermal pad), use '0' "
                        "or '' for the extra pads instead."
                    ),
                )
            seen.add(pad.number)

        # Through-hole pads need a drill; SMDs must not.
        for pad in input.pads:
            if pad.pad_type in ("thru_hole", "np_thru_hole") and pad.drill <= 0.0:
                return LibAddFootprintOutput(
                    status="invalid_input",
                    note=(
                        f"pad {pad.number!r} is {pad.pad_type!r} but has "
                        f"no drill; through-hole pads must set drill > 0."
                    ),
                )

        lib_path = input.lib_path.expanduser().resolve()
        fp_file = lib_path / f"{input.footprint_name}.kicad_mod"

        # 2. Existence / overwrite check.
        existed = fp_file.exists()
        if existed and not input.overwrite:
            return LibAddFootprintOutput(
                status="footprint_exists",
                lib_path=str(lib_path),
                footprint_path=str(fp_file),
                footprint_name=input.footprint_name,
                note=(
                    f"{fp_file.name} already exists in {lib_path}. Pass "
                    "overwrite=True to replace."
                ),
            )

        created_library = not lib_path.exists()

        # 3. Dry-run short-circuit.
        if input.dry_run:
            action = "replace" if existed else "write"
            hint = (
                f" Would also create library directory at {lib_path}."
                if created_library
                else ""
            )
            return LibAddFootprintOutput(
                status="dry_run",
                lib_path=str(lib_path),
                footprint_path=str(fp_file),
                footprint_name=input.footprint_name,
                pad_count=len(input.pads),
                line_count=len(input.lines),
                created_library=False,
                overwrote=False,
                note=(
                    f"dry_run=True; would {action} {fp_file.name} with "
                    f"{len(input.pads)} pad(s), {len(input.lines)} "
                    f"line(s).{hint}"
                ),
            )

        # 4. Synthesize the footprint tree.
        fp_root = _build_footprint_node(
            name=input.footprint_name,
            description=input.description,
            tags=input.tags,
            layer=input.layer,
            reference=input.reference,
            value=input.value or input.footprint_name,
            attributes=input.attributes,
            pads=input.pads,
            lines=input.lines,
        )
        doc = SexprDocument(path=fp_file, source=b"", root=fp_root)

        # 5. Snapshot before any filesystem write. If the directory
        # doesn't exist yet, snapshot its parent — that's the part
        # that'd need restoring to roll back a "create new library"
        # outcome.
        snapshot_target = lib_path if lib_path.exists() else lib_path.parent
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode
        snapshot_ref: str | None = None
        try:
            snapshot_ref = snapshot(
                snapshot_target,
                mode=snapshot_mode,
                reason=(
                    f"lib_add_footprint:{lib_path.name}:{input.footprint_name}"
                ),
            )
        except SnapshotError as exc:
            return LibAddFootprintOutput(
                status="write_failed",
                lib_path=str(lib_path),
                footprint_path=str(fp_file),
                footprint_name=input.footprint_name,
                note=f"snapshot failed before write: {exc}.",
            )

        # 6. Write.
        try:
            lib_path.mkdir(parents=True, exist_ok=True)
            doc.save(fp_file)
        except (OSError, RuntimeError) as exc:
            out_fail = LibAddFootprintOutput(
                status="write_failed",
                lib_path=str(lib_path),
                footprint_path=str(fp_file),
                footprint_name=input.footprint_name,
                note=(
                    f"save failed after snapshot: {exc}. Restore from the "
                    "snapshot if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = LibAddFootprintOutput(
            status="ok",
            lib_path=str(lib_path),
            footprint_path=str(fp_file),
            footprint_name=input.footprint_name,
            pad_count=len(input.pads),
            line_count=len(input.lines),
            created_library=created_library,
            overwrote=existed,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _footprint_property(
    *,
    name: str,
    value: str,
    at_x: float,
    at_y: float,
    layer: str,
    hidden: bool = False,
) -> SList:
    """``(property "Name" "Value" (at X Y) (layer "...") (uuid "...") (effects ...))``.

    Footprint properties differ from schematic / library-symbol
    properties in two ways: they carry a ``(layer ...)`` child (props
    can live on silkscreen, fab, etc.) and a ``(uuid ...)`` — each
    footprint-property instance is individually addressable in the PCB
    editor. The at-node here uses KiCAD's footprint convention of
    ``(at X Y)`` with no explicit angle atom when the angle is 0;
    footprint parser isn't as strict as the sheet-property parser.
    """
    items: list[SAtom | SList] = [
        atom("property"),
        atom(name, quoted=True),
        atom(value, quoted=True),
        slist(atom("at"), atom(fmt_mm(at_x)), atom(fmt_mm(at_y))),
        slist(atom("layer"), atom(layer, quoted=True)),
    ]
    if hidden:
        items.append(flag_node("hide", True))
    items.append(uuid_node(str(uuid_mod.uuid4())))
    items.append(effects_node())
    return SList(items=items)


def _build_pad_node(pad: FootprintPad) -> SList:
    """Synthesize one ``(pad "N" <type> <shape> ...)`` block."""
    # Layer set selection — through-hole always uses *.Cu / *.Mask;
    # SMD picks front or back based on pad.layer. The three tuples have
    # different arities (THT is 2, SMD front/back are 3), so the local
    # has to widen to ``tuple[str, ...]`` for mypy.
    layers: tuple[str, ...]
    if pad.pad_type in ("thru_hole", "np_thru_hole"):
        layers = _PAD_LAYERS_THT
    elif pad.layer == "B.Cu":
        layers = _PAD_LAYERS_SMD_BACK
    else:
        layers = _PAD_LAYERS_SMD_FRONT

    items: list[SAtom | SList] = [
        atom("pad"),
        atom(pad.number, quoted=True),
        atom(pad.pad_type),
        atom(pad.shape),
        slist(atom("at"), atom(fmt_mm(pad.x)), atom(fmt_mm(pad.y))),
        slist(atom("size"), atom(fmt_mm(pad.size_w)), atom(fmt_mm(pad.size_h))),
    ]
    if pad.drill > 0.0:
        items.append(slist(atom("drill"), atom(fmt_mm(pad.drill))))
    items.append(
        slist(atom("layers"), *(atom(layer_name, quoted=True) for layer_name in layers))
    )
    items.append(uuid_node(str(uuid_mod.uuid4())))
    return SList(items=items)


def _build_line_node(line: FootprintLine) -> SList:
    """Synthesize one ``(fp_line ...)`` block."""
    stroke = slist(
        atom("stroke"),
        slist(atom("width"), atom(fmt_mm(line.width))),
        slist(atom("type"), atom("solid")),
    )
    return slist(
        atom("fp_line"),
        slist(atom("start"), atom(fmt_mm(line.start_x)), atom(fmt_mm(line.start_y))),
        slist(atom("end"), atom(fmt_mm(line.end_x)), atom(fmt_mm(line.end_y))),
        stroke,
        slist(atom("layer"), atom(line.layer, quoted=True)),
        uuid_node(str(uuid_mod.uuid4())),
    )


def _build_footprint_node(
    *,
    name: str,
    description: str,
    tags: str,
    layer: str,
    reference: str,
    value: str,
    attributes: list[str],
    pads: list[FootprintPad],
    lines: list[FootprintLine],
) -> SList:
    """Assemble the top-level ``(footprint "name" ...)`` tree.

    Layout follows KiCAD's canonical order for a fresh footprint:
    header fields → layer → descr/tags → properties → attr → graphics
    → pads → (embedded_fonts no). Straying from this order doesn't
    break the parser but makes diffs against GUI-saved footprints
    noisier than they need to be.
    """
    items: list[SAtom | SList] = [
        atom("footprint"),
        atom(name, quoted=True),
        slist(atom("version"), atom(_KICAD_MOD_VERSION)),
        slist(atom("generator"), atom(_KICAD_MOD_GENERATOR, quoted=True)),
        slist(
            atom("generator_version"),
            atom(_KICAD_MOD_GENERATOR_VERSION, quoted=True),
        ),
        slist(atom("layer"), atom(layer, quoted=True)),
    ]
    if description:
        items.append(slist(atom("descr"), atom(description, quoted=True)))
    if tags:
        items.append(slist(atom("tags"), atom(tags, quoted=True)))

    # Canonical property set. Reference sits slightly above origin,
    # Value slightly below — the defaults KiCAD's footprint wizard
    # uses. Datasheet + Description are hidden by convention.
    items.append(
        _footprint_property(
            name="Reference",
            value=reference,
            at_x=0.0,
            at_y=_REF_DEFAULT_Y,
            layer="F.SilkS",
        )
    )
    items.append(
        _footprint_property(
            name="Value",
            value=value,
            at_x=0.0,
            at_y=_VAL_DEFAULT_Y,
            layer="F.Fab",
        )
    )
    items.append(
        _footprint_property(
            name="Datasheet",
            value="",
            at_x=0.0,
            at_y=0.0,
            layer="F.Fab",
            hidden=True,
        )
    )
    items.append(
        _footprint_property(
            name="Description",
            value=description,
            at_x=0.0,
            at_y=0.0,
            layer="F.Fab",
            hidden=True,
        )
    )

    # (attr ...) — only emit when there's at least one attribute,
    # otherwise KiCAD prefers the node absent.
    if attributes:
        attr_node = slist(atom("attr"), *(atom(a) for a in attributes))
        items.append(attr_node)

    # Graphics first (silkscreen, courtyard, fab) so they render
    # behind the pads. KiCAD orders by list position on the layer.
    for line in lines:
        items.append(_build_line_node(line))

    # Pads.
    for pad in pads:
        items.append(_build_pad_node(pad))

    # Trailing (embedded_fonts no) — match GUI output.
    items.append(flag_node("embedded_fonts", False))
    return SList(items=items)


__all__ = [
    "FootprintLine",
    "FootprintPad",
    "LibAddFootprintInput",
    "LibAddFootprintOutput",
    "LibAddFootprintTool",
]

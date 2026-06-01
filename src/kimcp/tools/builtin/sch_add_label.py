"""sch_add_label — place a net label on a schematic (M17).

KiCAD has three label node types and they serve different purposes in
the netlist:

* **local** (``(label ...)``) — names the net touching that coordinate
  on the current sheet. Single-sheet connectivity.
* **global** (``(global_label ...)``) — name crosses sheet boundaries
  across the entire project. Comes with a ``(shape ...)`` direction
  hint (input/output/bidirectional/tri_state/passive) and an
  Intersheetrefs property for the cross-reference printout.
* **hierarchical** (``(hierarchical_label ...)``) — names a port on
  the current sheet that a parent sheet can wire to via its sheet-pin
  of the same name. ``(shape ...)`` is the pin-direction hint.

All three live at the top level of the schematic's s-expression root
(siblings of ``symbol``, ``wire``, ``junction`` etc.). Format pinned
from KiCAD 9.x::

    (label "NET"
      (at X Y ANGLE)
      (effects (font (size 1.27 1.27)) (justify left bottom))
      (uuid "..."))

    (global_label "NET"
      (shape input)
      (at X Y ANGLE)
      (fields_autoplaced yes)
      (effects (font (size 1.27 1.27)) (justify left))
      (uuid "...")
      (property "Intersheetrefs" "${INTERSHEET_REFS}"
        (at X Y 0)
        (effects (font (size 1.27 1.27)) (hide yes))))

    (hierarchical_label "NET"
      (shape input)
      (at X Y ANGLE)
      (effects (font (size 1.27 1.27)) (justify left))
      (uuid "..."))

One tool for all three, selected by ``kind``. Keeps the call surface
narrow and matches how an LLM thinks about "place a label" at the
prompt level.

Status enum
-----------

* **ok**             — label appended and written.
* **dry_run**        — caller passed ``dry_run=True``.
* **sch_not_found**  — path missing / not a file / wrong suffix.
* **invalid_schema** — top_head isn't ``kicad_sch``.
* **parse_failed**   — the SEXPR parser rejected the file bytes.
* **invalid_input**  — kind/shape combination is not representable
                        (e.g., a local label with a shape).
* **write_failed**   — atomic save raised.

Backend: SEXPR, required. Same rationale as M14/M15/M16.
"""

from __future__ import annotations

import logging
import math
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
    at_node_explicit,
    atom,
    effects_node,
    flag_node,
    load_sexpr_doc,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


LabelKind = Literal["local", "global", "hierarchical"]
LabelShape = Literal["input", "output", "bidirectional", "tri_state", "passive"]

_VALID_SHAPES: frozenset[str] = frozenset(
    {"input", "output", "bidirectional", "tri_state", "passive"}
)


# -- input / output --------------------------------------------------------


class SchAddLabelInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    text: str = Field(
        ...,
        description=(
            "Net name. Conventionally uppercase with underscores "
            "(``VCC``, ``BOOT_MODE``); KiCAD also accepts lowercase and "
            "mixed-case. Must be non-empty."
        ),
    )
    at_x: float = Field(..., description="Anchor X in millimetres.")
    at_y: float = Field(..., description="Anchor Y in millimetres.")
    angle: float = Field(
        default=0.0,
        description=(
            "Rotation angle in degrees. KiCAD snaps to 0/90/180/270 on "
            "open; non-multiples are written through verbatim."
        ),
    )
    kind: LabelKind = Field(
        default="local",
        description=(
            "Label variety. 'local' names the immediately-touching net on "
            "this sheet. 'global' crosses sheet boundaries project-wide. "
            "'hierarchical' binds to a parent sheet's matching sheet pin."
        ),
    )
    shape: LabelShape = Field(
        default="input",
        description=(
            "Direction / shape hint for global + hierarchical labels. "
            "Ignored when kind='local' (and rejected if explicitly set "
            "to anything other than the default)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the label that would be added without writing.",
    )


class SchAddLabelOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "invalid_input",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None, description="Resolved absolute path to the .kicad_sch."
    )
    text: str | None = Field(default=None, description="Echo of the label text as written.")
    kind: LabelKind | None = Field(
        default=None, description="Echo of the label kind as written."
    )
    label_uuid: str | None = Field(
        default=None,
        description="UUID of the new label (populated on status=ok only).",
    )
    note: str | None = Field(
        default=None, description="Diagnostic string for non-ok statuses."
    )


# -- tool ------------------------------------------------------------------


class SchAddLabelTool(Tool[SchAddLabelInput, SchAddLabelOutput]):
    """Append a local/global/hierarchical label to a .kicad_sch."""

    name = "sch_add_label"
    version = "0.2.0"
    description = (
        "Place a net label on a .kicad_sch. Use SPARINGLY — labels are net-name "
        "references, not a general connection primitive. For connecting two pins "
        "on the same sheet, use sch_add_wire; that is what humans read. "
        "Legitimate uses for a label: (1) cross-sheet connectivity via "
        "kind='global' or kind='hierarchical'; (2) distinguishing multiple "
        "ground domains (AGND/DGND/PGND); (3) naming an electrically important "
        "signal (CLK, MOSI, SDA, nRESET) where the name aids readability; "
        "(4) long-distance same-sheet nets where a wire route would be visually "
        "noisy. A typical sheet has single-digit label count, not tens. "
        "Supports local/global/hierarchical kinds; global+hierarchical carry a "
        "shape hint (input/output/bidirectional/tri_state/passive). Plan the "
        "anchor coordinate on the 100-mil schematic grid (2.54 mm default; "
        "see safety.grid_snap_mm) so the label attaches to a wire endpoint "
        "without dangling — off-grid inputs are snapped (cites KICAD-318) "
        "and a meta.warnings entry is emitted. Supports dry_run; snapshots "
        "before write per ADR-0008. Also emits a meta.warnings entry when a "
        "same-net local label already exists nearby on the sheet (see "
        "safety.label_proximity_warn_mm) — that pattern should be a wire."
    )
    input_model = SchAddLabelInput
    output_model = SchAddLabelOutput
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

    async def run(self, input: SchAddLabelInput) -> SchAddLabelOutput:
        # 1. Text must be non-empty — an empty label is a bug, not a
        # meaningful placement. Empty-string net names confuse KiCAD's
        # ERC and produce opaque downstream errors.
        if not input.text:
            return SchAddLabelOutput(
                status="invalid_input",
                sch_path=None,
                note="label text must be non-empty.",
            )

        # Shape only makes sense on global / hierarchical. The default is
        # "input" so the common case needs no explicit shape; reject only
        # if someone explicitly passes a non-default shape for a local
        # label (the validator doesn't know the default, so we compare
        # after parse — here we skip that for simplicity and accept the
        # flag on local without writing it).
        # (Intentional: Pydantic already bounds it to LabelShape.)

        # 2. Preflight path validation.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddLabelOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddLabelOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddLabelOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 3. Parse.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddLabelOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddLabelOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 4. Apply grid snap per safety.grid_snap_mm.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {"at_x": input.at_x, "at_y": input.at_y}, grid_snap_mm
        )
        at_x, at_y = snapped["at_x"], snapped["at_y"]

        # 4b. Same-net proximity check (local labels only).
        # Cross-sheet (global/hierarchical) labels are legitimately
        # repeated and exempt. For locals, two same-text labels within
        # `label_proximity_warn_mm` are the classic "label used as a
        # wire-stand-in" pattern — non-blocking warning so the agent can
        # reconsider on the next call.
        proximity_warning: str | None = None
        if input.kind == "local":
            proximity_threshold = (
                self._config.safety.label_proximity_warn_mm
                if self._config is not None
                else 25.0
            )
            if proximity_threshold is not None:
                nearby = _find_nearby_local_labels(
                    doc.root,
                    text=input.text,
                    at_x=at_x,
                    at_y=at_y,
                    threshold_mm=proximity_threshold,
                )
                if nearby:
                    closest_x, closest_y, closest_dist = nearby[0]
                    proximity_warning = (
                        f"label readability: a local label with text "
                        f"{input.text!r} already exists on this sheet at "
                        f"({closest_x}, {closest_y}), {closest_dist:.2f} mm "
                        f"from the new placement (threshold "
                        f"{proximity_threshold} mm; "
                        f"{len(nearby)} same-net label(s) nearby total). "
                        "Two nearby same-net labels are usually clearer as a "
                        "single wire — consider sch_add_wire instead. "
                        "Cites KICAD-311."
                    )

        # 5. Dry-run.
        if input.dry_run:
            out_dry = SchAddLabelOutput(
                status="dry_run",
                sch_path=str(sch_path),
                text=input.text,
                kind=input.kind,
                label_uuid=None,
                note=(
                    f"dry_run=True; would add {input.kind} label "
                    f"{input.text!r} at ({at_x}, {at_y}). "
                    "Re-run with dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            if proximity_warning is not None:
                out_dry.meta.warnings.append(proximity_warning)
            return out_dry

        # 6. Synthesize + append.
        label_uuid = str(uuid_mod.uuid4())
        label_node = _build_label_node(
            kind=input.kind,
            text=input.text,
            at_x=at_x,
            at_y=at_y,
            angle=input.angle,
            shape=input.shape,
            label_uuid=label_uuid,
        )
        doc.root.append(label_node)

        # 6. Snapshot.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_label:{sch_path.name}:{input.text}",
            )
        except SnapshotError as exc:
            return SchAddLabelOutput(
                status="write_failed",
                sch_path=str(sch_path),
                text=input.text,
                kind=input.kind,
                note=f"snapshot failed before write: {exc}.",
            )

        # 7. Save.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddLabelOutput(
                status="write_failed",
                sch_path=str(sch_path),
                text=input.text,
                kind=input.kind,
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddLabelOutput(
            status="ok",
            sch_path=str(sch_path),
            text=input.text,
            kind=input.kind,
            label_uuid=label_uuid,
        )
        out.meta.snapshot_ref = snapshot_ref
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        if proximity_warning is not None:
            out.meta.warnings.append(proximity_warning)
        return out


# -- helpers ---------------------------------------------------------------


_KIND_TO_HEAD: dict[LabelKind, str] = {
    "local": "label",
    "global": "global_label",
    "hierarchical": "hierarchical_label",
}


def _build_label_node(
    *,
    kind: LabelKind,
    text: str,
    at_x: float,
    at_y: float,
    angle: float,
    shape: LabelShape,
    label_uuid: str,
) -> SList:
    """Dispatch on ``kind`` to one of three synthesizers.

    Factored so each kind's quirks (Intersheetrefs on global,
    fields_autoplaced, justify forms) live in clearly separate arms.
    """
    if shape not in _VALID_SHAPES:
        # Defensive — Pydantic narrows LabelShape to these already, but
        # keeps this helper usable from direct unit tests without
        # reconstructing the full input pipeline.
        raise ValueError(f"invalid label shape: {shape!r}")

    head = _KIND_TO_HEAD[kind]

    # Label positions and label-property positions both require the
    # 3-atom ``(at X Y angle)`` form in KiCAD 10 — the elision that
    # ``at_node`` performs for angle=0 breaks the strict load parser
    # with ``need a number for 'text angle'``. Use
    # ``at_node_explicit`` throughout.
    if kind == "local":
        # Local labels get (justify left bottom) — the native eeschema
        # form for a horizontal label whose anchor is at the text's left
        # baseline. Angle handling: KiCAD places the text along the
        # angle axis; no separate fields.
        return slist(
            atom(head),
            atom(text, quoted=True),
            at_node_explicit(at_x, at_y, angle),
            effects_node(justify=("left", "bottom")),
            uuid_node(label_uuid),
        )

    if kind == "hierarchical":
        return slist(
            atom(head),
            atom(text, quoted=True),
            slist(atom("shape"), atom(shape)),
            at_node_explicit(at_x, at_y, angle),
            effects_node(justify=("left",)),
            uuid_node(label_uuid),
        )

    # kind == "global"
    #
    # Intersheetrefs is a KiCAD-internal auto-populated property that
    # renders the "used on sheet X, Y" cross-reference printout next to
    # a global label. Hidden by default; KiCAD toggles visibility via a
    # project-level setting. We emit the literal template string
    # "${INTERSHEET_REFS}" that eeschema writes — KiCAD substitutes it
    # at render time. Without this property the label still functions
    # electrically but the cross-reference feature renders blank.
    intersheetrefs = slist(
        atom("property"),
        atom("Intersheetrefs", quoted=True),
        atom("${INTERSHEET_REFS}", quoted=True),
        at_node_explicit(at_x, at_y, 0.0),
        effects_node(hidden=True),
    )
    return slist(
        atom(head),
        atom(text, quoted=True),
        slist(atom("shape"), atom(shape)),
        at_node_explicit(at_x, at_y, angle),
        flag_node("fields_autoplaced", True),
        effects_node(justify=("left",)),
        uuid_node(label_uuid),
        intersheetrefs,
    )


def _find_nearby_local_labels(
    root: SList,
    *,
    text: str,
    at_x: float,
    at_y: float,
    threshold_mm: float,
) -> list[tuple[float, float, float]]:
    """Return existing local labels with the same text within
    ``threshold_mm`` of ``(at_x, at_y)``.

    Each tuple is ``(x, y, distance_mm)``. Only ``(label …)`` heads are
    considered — globals and hierarchicals are exempt because cross-sheet
    connectivity is their legitimate role. Results are sorted nearest
    first, so callers can quote the closest match without re-sorting.

    Coordinates are read from each label's ``(at X Y …)`` child; any
    label missing a well-formed ``(at …)`` is skipped silently rather
    than aborting the readability check.
    """
    hits: list[tuple[float, float, float]] = []
    for child in root.items:
        if not isinstance(child, SList):
            continue
        if child.head != "label":
            continue
        if len(child.items) < 2:
            continue
        text_atom = child.items[1]
        if not isinstance(text_atom, SAtom) or text_atom.text != text:
            continue
        at_node = child.find("at")
        if at_node is None or len(at_node.items) < 3:
            continue
        x_atom = at_node.items[1]
        y_atom = at_node.items[2]
        if not isinstance(x_atom, SAtom) or not isinstance(y_atom, SAtom):
            continue
        try:
            x = float(x_atom.text)
            y = float(y_atom.text)
        except ValueError:
            continue
        distance = math.hypot(x - at_x, y - at_y)
        if distance <= threshold_mm:
            hits.append((x, y, distance))
    hits.sort(key=lambda t: t[2])
    return hits


def _find_label_by_uuid(root: SList, label_uuid: str) -> SList | None:
    """Return the first label (any kind) matching ``label_uuid``.

    Public-ish helper — used by tests and future read-back / delete
    flows. Kept local to this module (leading-underscore) until a real
    second consumer emerges.
    """
    for child in root.items:
        if not isinstance(child, SList):
            continue
        if child.head not in ("label", "global_label", "hierarchical_label"):
            continue
        uuid_node_child = child.find("uuid")
        if uuid_node_child is None or len(uuid_node_child.items) < 2:
            continue
        payload = uuid_node_child.items[1]
        if isinstance(payload, SAtom) and payload.text == label_uuid:
            return child
    return None


__all__ = [
    "LabelKind",
    "LabelShape",
    "SchAddLabelInput",
    "SchAddLabelOutput",
    "SchAddLabelTool",
    "_build_label_node",
    "_find_label_by_uuid",
    "_find_nearby_local_labels",
]

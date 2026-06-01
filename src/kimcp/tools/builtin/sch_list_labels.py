"""sch_list_labels — enumerate net labels on a .kicad_sch (M27).

The second schematic-introspection READ tool. M26 answered "what
components are on this sheet?"; this one answers "what nets are named
here?" — essential before wiring, deleting, or reviewing connectivity.

KiCAD encodes three label varieties at the top level of the sheet's
s-expression (siblings of ``symbol``, ``wire``, ``junction``):

* **local** (``(label ...)``) — names the touching net on this sheet
  only. No shape / direction.
* **global** (``(global_label ...)``) — project-wide net crossing sheet
  boundaries. Carries a ``(shape ...)`` hint
  (input/output/bidirectional/tri_state/passive) plus an auto-rendered
  ``Intersheetrefs`` property.
* **hierarchical** (``(hierarchical_label ...)``) — binds to a parent
  sheet's matching sheet pin of the same name. Same ``(shape ...)``
  vocabulary as global.

Returns one entry per label with uuid, kind, text, shape (None for
local), anchor position, and rotation. Filters by ``kind`` and
``text_contains`` for "list only globals" / "find every CLK* label"
style queries.

Hierarchical-sheet recursion is out of scope — only labels on the
requested sheet are returned. Matches M26's single-sheet contract; a
future sheet-walker will reuse both.

Status enum:

* **ok**             — labels enumerated (list may be empty).
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


LabelKind = Literal["local", "global", "hierarchical"]

# Map between s-expression head names and the tool's kind enum. Keeps
# parse/filter logic from spelling the three heads at every callsite.
_HEAD_TO_KIND: dict[str, LabelKind] = {
    "label": "local",
    "global_label": "global",
    "hierarchical_label": "hierarchical",
}


# -- envelope sub-models ---------------------------------------------------


class LabelInstance(BaseModel):
    """One label from the .kicad_sch's top-level label list."""

    model_config = ConfigDict(extra="allow")

    uuid: str = Field(..., description="Label UUID (stable across edits).")
    kind: LabelKind = Field(
        ...,
        description=(
            "Label variety. 'local' = same-sheet net name; 'global' = "
            "project-wide; 'hierarchical' = subsheet port."
        ),
    )
    text: str = Field(..., description="Net name as written on the label.")
    shape: str | None = Field(
        default=None,
        description=(
            "Direction / shape hint for global + hierarchical labels "
            "(input / output / bidirectional / tri_state / passive). "
            "Always None for local labels."
        ),
    )
    at_x: float = Field(..., description="Anchor X coordinate in millimetres.")
    at_y: float = Field(..., description="Anchor Y coordinate in millimetres.")
    angle: float = Field(
        ..., description="Rotation angle in degrees (0 / 90 / 180 / 270 typically)."
    )


# -- input / output --------------------------------------------------------


class SchListLabelsInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    kind: LabelKind | None = Field(
        default=None,
        description=(
            "Filter to a single label kind. Null returns all three kinds "
            "(the default)."
        ),
    )
    text_contains: str | None = Field(
        default=None,
        description=(
            "Filter to labels whose text contains this substring "
            "(e.g. 'CLK' for every clock net). Case-sensitive. Null "
            "returns every label."
        ),
    )


class SchListLabelsOutput(ToolOutput):
    status: Literal[
        "ok",
        "sch_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    sch_path: str | None = Field(default=None)
    labels: list[LabelInstance] = Field(
        default_factory=list,
        description="Labels matching the filters, in document order.",
    )
    total: int = Field(
        default=0,
        description=(
            "Length of labels after filtering. Mirrored so callers don't "
            "have to len() the list."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchListLabelsTool(Tool[SchListLabelsInput, SchListLabelsOutput]):
    """Enumerate net labels (local/global/hierarchical) on a .kicad_sch."""

    name = "sch_list_labels"
    version = "0.1.0"
    description = (
        "Enumerate net labels on a .kicad_sch. Covers all three kinds: "
        "local (same-sheet), global (project-wide), and hierarchical "
        "(subsheet port). Returns uuid, kind, text, shape (null for "
        "local), anchor position, and rotation. Supports kind and "
        "text_contains filters."
    )
    input_model = SchListLabelsInput
    output_model = SchListLabelsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    async def run(self, input: SchListLabelsInput) -> SchListLabelsOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or not sch_path.is_file():
            return SchListLabelsOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchListLabelsOutput(
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
            return SchListLabelsOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchListLabelsOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        labels: list[LabelInstance] = []
        for child in doc.root.items:
            if not isinstance(child, SList):
                continue
            kind = _HEAD_TO_KIND.get(child.head or "")
            if kind is None:
                continue
            parsed = _parse_label(child, kind)
            if parsed is None:
                continue
            if input.kind is not None and parsed.kind != input.kind:
                continue
            if (
                input.text_contains is not None
                and input.text_contains not in parsed.text
            ):
                continue
            labels.append(parsed)

        return SchListLabelsOutput(
            status="ok",
            sch_path=str(sch_path),
            labels=labels,
            total=len(labels),
        )


# -- parse helpers ---------------------------------------------------------


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _atom_at_index(node: SList, idx: int) -> str | None:
    """Return the atom text at ``idx`` of ``node`` if present and atomic."""
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


def _child_atom_text(parent: SList, head: str, idx: int = 1) -> str | None:
    """Find a child ``(<head> atom ...)`` and return its atom at ``idx``."""
    child = parent.find(head)
    if child is None or len(child.items) <= idx:
        return None
    return _atom_text(child.items[idx])


def _parse_label(node: SList, kind: LabelKind) -> LabelInstance | None:
    """Extract a LabelInstance from one `(label|global_label|hierarchical_label ...)`.

    Returns None when the node is missing fields we treat as non-optional
    (uuid, text). KiCAD-written schematics always have them; the guard
    covers hand-crafted or malformed fixtures without promoting them to
    corruption-level errors.
    """
    uuid = _child_atom_text(node, "uuid")
    # Label text is positional: the second element after the head.
    text = _atom_at_index(node, 1)
    if uuid is None or text is None:
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

    shape: str | None = None
    if kind != "local":
        # (shape <name>) — only meaningful for global + hierarchical.
        # Local labels may not carry a shape; if a fixture does, we
        # still ignore it to stay honest to the kind's semantics.
        shape = _child_atom_text(node, "shape")

    return LabelInstance(
        uuid=uuid,
        kind=kind,
        text=text,
        shape=shape,
        at_x=at_x,
        at_y=at_y,
        angle=angle,
    )


__all__ = [
    "LabelInstance",
    "LabelKind",
    "SchListLabelsInput",
    "SchListLabelsOutput",
    "SchListLabelsTool",
]

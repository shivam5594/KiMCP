"""pcb_list_tracks — enumerate routed copper on a .kicad_pcb.

The PCB-routing counterpart to ``pcb_list_footprints``. Walks the
top-level ``(segment ...)``, ``(via ...)``, and ``(arc ...)`` children
of a ``.kicad_pcb`` s-expression and returns one entry per routing
object — the bits the netlister treats as signal-carrying copper.

What counts as "a track" here:

* **segment** — a straight copper track (``(segment (start ...) (end ...)
  (width ...) (layer ...) (net N) (uuid ...))``). The bread-and-butter
  of routing.
* **via** — a through-hole or blind/buried via (``(via (at ...) (size ...)
  (drill ...) (layers "F.Cu" "B.Cu") (net N) (uuid ...))``). Pads that
  pierce one or more copper layers to join nets.
* **arc** — a curved track (``(arc (start ...) (mid ...) (end ...)
  (width ...) (layer ...) (net N) (uuid ...))``). Less common than
  segments but occasionally used for high-speed diff-pair bends.

What does NOT count: graphic items on copper layers (``(gr_line ...)``,
``(gr_circle ...)``), zone fills (``(zone ...)``), pads (those live
inside footprints). Those are geometry, not routed copper — the
netlister doesn't flow signals through ``gr_line``.

Net resolution: KiCAD stores a net *number* on each track/via and the
human-readable net *names* in the separate top-level
``(net <num> "<name>")`` declarations. This tool builds the lookup
once and populates ``net_name`` on each emitted item. Net 0 is the
empty-string "no-net" sentinel — we surface it as ``net_name=""``
rather than inventing a fake name.

Why a single ``TrackItem`` model rather than a per-kind union:
LLMs consume the JSON and a flat schema with a ``kind`` discriminator
reads more cleanly than having to branch on Python types. Fields
that don't apply to a given kind stay at zero / empty — the ``kind``
field tells the caller which fields are semantically meaningful.

Filters compose (AND semantics): ``kinds=["segment"]`` + ``layer="F.Cu"``
+ ``net_contains="VCC"`` returns top-side VCC segment tracks.

Status enum:

* **ok**             — tracks enumerated (list may be empty).
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


# Layer alias — same vocabulary as pcb_list_footprints. "top"/"bottom"
# expand to F.Cu/B.Cu; other strings pass through so inner-copper
# filters (In1.Cu, In2.Cu, ...) still work.
_LAYER_ALIASES: dict[str, str] = {"top": "F.Cu", "bottom": "B.Cu"}


# Canonical kind values — pins the literal set for the filter + the
# TrackItem.kind field. Alphabetical for dict-ordering determinism
# elsewhere (tools/list advertising, etc).
_KINDS: tuple[str, ...] = ("arc", "segment", "via")


TrackKind = Literal["segment", "via", "arc"]


# -- envelope sub-models ---------------------------------------------------


class TrackItem(BaseModel):
    """One routed-copper instance: segment, via, or arc.

    Flat schema with a ``kind`` discriminator. Fields that don't apply
    to the instance's kind stay at their default (0.0 for floats, []
    for lists) — the ``kind`` tells the caller which fields are
    meaningful. Example: a ``via`` has ``at_x``/``at_y`` + ``layers``
    populated, but ``start_x``/``end_x`` stay at 0.0 and ``layer``
    stays empty.
    """

    model_config = ConfigDict(extra="allow")

    kind: TrackKind = Field(
        ...,
        description=(
            "Discriminator — ``'segment'``, ``'via'``, or ``'arc'``. Determines "
            "which geometry + layer fields are meaningful."
        ),
    )
    uuid: str = Field(..., description="Track UUID (stable across edits).")
    net_num: int = Field(
        ...,
        description=(
            "KiCAD net number. Net 0 is the sentinel for 'no net assigned'."
        ),
    )
    net_name: str = Field(
        default="",
        description=(
            "Resolved net name from the board's ``(net <num> \"<name>\")`` "
            "declarations. Empty string when net 0 or unresolved."
        ),
    )
    # Geometry fields — populated by kind:
    #   segment: start + end
    #   arc:     start + mid + end
    #   via:     at only
    start_x: float = Field(default=0.0)
    start_y: float = Field(default=0.0)
    end_x: float = Field(default=0.0)
    end_y: float = Field(default=0.0)
    mid_x: float = Field(
        default=0.0, description="Arc midpoint X (arcs only; 0 otherwise)."
    )
    mid_y: float = Field(
        default=0.0, description="Arc midpoint Y (arcs only; 0 otherwise)."
    )
    at_x: float = Field(
        default=0.0, description="Via center X (vias only; 0 otherwise)."
    )
    at_y: float = Field(
        default=0.0, description="Via center Y (vias only; 0 otherwise)."
    )
    # Layer fields:
    #   segment/arc: layer populated, layers empty.
    #   via:         layers populated (2-element), layer empty.
    layer: str = Field(
        default="",
        description="Copper layer for segments/arcs. Empty for vias (see ``layers``).",
    )
    layers: list[str] = Field(
        default_factory=list,
        description=(
            "Span layers for vias — typically ``['F.Cu', 'B.Cu']`` for through "
            "vias. Empty for segments/arcs (see ``layer``)."
        ),
    )
    # Dimensions:
    #   segment/arc: width (track width in mm).
    #   via:         width = via pad size (size), drill = drill diameter.
    width: float = Field(
        default=0.0,
        description=(
            "Track width in mm for segments/arcs. For vias, the pad "
            "(``size``) diameter."
        ),
    )
    drill: float = Field(
        default=0.0,
        description="Via drill diameter in mm. Zero for segments/arcs.",
    )


# -- input / output --------------------------------------------------------


class PcbListTracksInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    kinds: list[TrackKind] | None = Field(
        default=None,
        description=(
            "Filter to these kinds of routed copper (``'segment'``, ``'via'``, "
            "``'arc'``). Null includes all three."
        ),
    )
    layer: str | None = Field(
        default=None,
        description=(
            "Filter by copper layer. Accepts 'top' / 'bottom' aliases or any "
            "explicit KiCAD layer name. For vias, matches if the layer is in "
            "the via's ``layers`` span. Null returns every layer."
        ),
    )
    net_contains: str | None = Field(
        default=None,
        description=(
            "Filter to tracks whose resolved net name contains this substring "
            "(case-sensitive). Null matches every net. An empty string matches "
            "tracks on net 0 / unresolved only when the net_name is empty."
        ),
    )


class PcbListTracksOutput(ToolOutput):
    status: Literal[
        "ok",
        "pcb_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    pcb_path: str | None = Field(default=None)
    tracks: list[TrackItem] = Field(
        default_factory=list,
        description="Tracks after filtering, sorted by (kind, uuid) for determinism.",
    )
    total: int = Field(
        default=0,
        description="Count of tracks after filtering.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class PcbListTracksTool(Tool[PcbListTracksInput, PcbListTracksOutput]):
    """Enumerate routed copper (segments, vias, arcs) on a .kicad_pcb."""

    name = "pcb_list_tracks"
    version = "0.1.0"
    description = (
        "Enumerate routed copper on a .kicad_pcb: segments, vias, and arcs. "
        "Resolves net names from the board's net declarations. Supports "
        "filters on kind, layer ('top' / 'bottom' / explicit), and net-name "
        "substring. Does NOT include zone fills or graphic items — use the "
        "full DRC / export tools for those."
    )
    input_model = PcbListTracksInput
    output_model = PcbListTracksOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(self, input: PcbListTracksInput) -> PcbListTracksOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists() or not pcb_path.is_file():
            return PcbListTracksOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbListTracksOutput(
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
            return PcbListTracksOutput(
                status="parse_failed",
                pcb_path=str(pcb_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_pcb":
            return PcbListTracksOutput(
                status="invalid_schema",
                pcb_path=str(pcb_path),
                note=(
                    f"expected top-level '(kicad_pcb ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Resolve filters once — cheap, readability win inside the loop.
        layer_filter = (
            _LAYER_ALIASES.get(input.layer, input.layer) if input.layer else None
        )
        kinds_filter: frozenset[str] | None = (
            frozenset(input.kinds) if input.kinds is not None else None
        )

        # Build net_num → net_name map from top-level (net N "name") decls.
        # Keep the whole walk; cheap and avoids a second pass.
        net_names: dict[int, str] = _build_net_name_map(doc.root)

        tracks: list[TrackItem] = []
        for child in doc.root.items:
            if not isinstance(child, SList):
                continue
            head = child.head or ""
            if head == "segment":
                item = _parse_segment(child, net_names)
            elif head == "via":
                item = _parse_via(child, net_names)
            elif head == "arc":
                item = _parse_arc(child, net_names)
            else:
                continue
            if item is None:
                continue
            if kinds_filter is not None and item.kind not in kinds_filter:
                continue
            if layer_filter is not None and not _matches_layer(item, layer_filter):
                continue
            if (
                input.net_contains is not None
                and input.net_contains not in item.net_name
            ):
                continue
            tracks.append(item)

        # Sort by (kind, uuid) for stable output. Kind first so a caller
        # eyeballing the list sees grouping; uuid within kind gives
        # deterministic intra-group order.
        tracks.sort(key=lambda t: (t.kind, t.uuid))

        return PcbListTracksOutput(
            status="ok",
            pcb_path=str(pcb_path),
            tracks=tracks,
            total=len(tracks),
        )


# -- layer matching --------------------------------------------------------


def _matches_layer(item: TrackItem, layer: str) -> bool:
    """Layer filter semantics: segments/arcs match on ``item.layer``,
    vias match if ``layer`` is in ``item.layers``.

    A via on F.Cu ↔ B.Cu legitimately satisfies a ``layer="F.Cu"``
    filter — it IS on F.Cu from the end user's perspective, even
    though it also lives on B.Cu.
    """
    if item.kind == "via":
        return layer in item.layers
    return item.layer == layer


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


def _float_at(node: SList, idx: int, default: float = 0.0) -> float:
    """Read a positional float atom with a default on missing/malformed.

    Defensive because hand-crafted fixtures occasionally omit fields
    or stick non-numeric text where a float belongs. A bad number
    shouldn't blow up the whole listing — default and keep going."""
    txt = _atom_at_index(node, idx)
    if txt is None:
        return default
    try:
        return float(txt)
    except ValueError:
        return default


def _int_at(node: SList, idx: int, default: int = 0) -> int:
    txt = _atom_at_index(node, idx)
    if txt is None:
        return default
    try:
        return int(txt)
    except ValueError:
        return default


def _build_net_name_map(root: SList) -> dict[int, str]:
    """Extract net number → net name from ``(net N "name")`` decls.

    KiCAD writes these as top-level children of ``kicad_pcb``. Each has
    shape ``(net <int> "<name>")``; malformed entries are skipped
    rather than raised because fixtures in tests often omit net 0's
    empty name entirely."""
    out: dict[int, str] = {}
    for child in root.items:
        if not isinstance(child, SList) or child.head != "net":
            continue
        if len(child.items) < 3:
            continue
        num_atom = child.items[1]
        name_atom = child.items[2]
        if not isinstance(num_atom, SAtom) or not isinstance(name_atom, SAtom):
            continue
        try:
            num = int(num_atom.text)
        except ValueError:
            continue
        out[num] = name_atom.text
    return out


def _parse_segment(node: SList, net_names: dict[int, str]) -> TrackItem | None:
    """Extract a segment ``TrackItem`` or return None if required
    fields (uuid, layer) are absent. Segments without a net are
    unusual but valid — KiCAD just assigns net 0."""
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None
    layer = _child_atom_text(node, "layer") or ""
    if not layer:
        return None

    start = node.find("start")
    end = node.find("end")
    start_x = _float_at(start, 1) if start is not None else 0.0
    start_y = _float_at(start, 2) if start is not None else 0.0
    end_x = _float_at(end, 1) if end is not None else 0.0
    end_y = _float_at(end, 2) if end is not None else 0.0

    width = 0.0
    width_node = node.find("width")
    if width_node is not None:
        width = _float_at(width_node, 1)

    net_num = 0
    net_node = node.find("net")
    if net_node is not None:
        net_num = _int_at(net_node, 1)

    return TrackItem(
        kind="segment",
        uuid=uuid,
        net_num=net_num,
        net_name=net_names.get(net_num, ""),
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        layer=layer,
        width=width,
    )


def _parse_via(node: SList, net_names: dict[int, str]) -> TrackItem | None:
    """Extract a via ``TrackItem``. Requires uuid + layers; missing
    either means the node can't be introspected meaningfully."""
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None

    # (layers "F.Cu" "B.Cu") — positional list of quoted atoms. Collect
    # every atom after the head; blind/buried vias can span inner
    # layers with more than two entries.
    layers: list[str] = []
    layers_node = node.find("layers")
    if layers_node is not None:
        for item in layers_node.items[1:]:
            if isinstance(item, SAtom):
                layers.append(item.text)
    if not layers:
        return None

    at_node = node.find("at")
    at_x = _float_at(at_node, 1) if at_node is not None else 0.0
    at_y = _float_at(at_node, 2) if at_node is not None else 0.0

    size = 0.0
    size_node = node.find("size")
    if size_node is not None:
        size = _float_at(size_node, 1)

    drill = 0.0
    drill_node = node.find("drill")
    if drill_node is not None:
        drill = _float_at(drill_node, 1)

    net_num = 0
    net_node = node.find("net")
    if net_node is not None:
        net_num = _int_at(net_node, 1)

    return TrackItem(
        kind="via",
        uuid=uuid,
        net_num=net_num,
        net_name=net_names.get(net_num, ""),
        at_x=at_x,
        at_y=at_y,
        layers=layers,
        width=size,
        drill=drill,
    )


def _parse_arc(node: SList, net_names: dict[int, str]) -> TrackItem | None:
    """Extract an arc ``TrackItem``. Same required-field contract as a
    segment plus a midpoint that distinguishes it from a straight
    track."""
    uuid = _child_atom_text(node, "uuid")
    if uuid is None:
        return None
    layer = _child_atom_text(node, "layer") or ""
    if not layer:
        return None

    start = node.find("start")
    mid = node.find("mid")
    end = node.find("end")
    start_x = _float_at(start, 1) if start is not None else 0.0
    start_y = _float_at(start, 2) if start is not None else 0.0
    mid_x = _float_at(mid, 1) if mid is not None else 0.0
    mid_y = _float_at(mid, 2) if mid is not None else 0.0
    end_x = _float_at(end, 1) if end is not None else 0.0
    end_y = _float_at(end, 2) if end is not None else 0.0

    width = 0.0
    width_node = node.find("width")
    if width_node is not None:
        width = _float_at(width_node, 1)

    net_num = 0
    net_node = node.find("net")
    if net_node is not None:
        net_num = _int_at(net_node, 1)

    return TrackItem(
        kind="arc",
        uuid=uuid,
        net_num=net_num,
        net_name=net_names.get(net_num, ""),
        start_x=start_x,
        start_y=start_y,
        mid_x=mid_x,
        mid_y=mid_y,
        end_x=end_x,
        end_y=end_y,
        layer=layer,
        width=width,
    )


__all__ = [
    "PcbListTracksInput",
    "PcbListTracksOutput",
    "PcbListTracksTool",
    "TrackItem",
    "TrackKind",
]

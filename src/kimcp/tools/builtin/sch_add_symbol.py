"""sch_add_symbol — append a symbol instance to a .kicad_sch (M14).

The **first schematic-creation mutator**. M12 proved we can edit leaves
(title_block scalars); this tool proves we can synthesize structural
nodes. Combined with the M13 resources layer it's the primitive a
prompt-driven schematic design flow stands on:

    LLM reads schematic via resources/read
       → decides to place a resistor
       → calls sch_add_symbol(lib_id="Device:R_Small", reference="R1", ...)
       → reads the schematic back, sees the new symbol, iterates.

Scope of the first ship
-----------------------

Deliberately narrow — keeps the blast radius small while the pattern
beds in:

* **Library resolution is out of scope.** The caller must choose a
  ``lib_id`` that's already present in the schematic's ``lib_symbols``
  section. Attempting to add a symbol whose definition isn't already
  embedded returns ``lib_symbol_not_found``. Lib-table lookup +
  embedding lives in a later tool (``sch_embed_lib_symbol``).
* **Single unit only.** ``unit=1`` is the realistic default; higher
  units are accepted and written through, but we don't introspect the
  lib_symbol's unit count to reject mismatches. KiCAD catches that on
  open.
* **No net/wire connection.** This tool places the symbol; connectivity
  is added separately via ``sch_add_wire`` / ``sch_add_label`` in later
  milestones.
* **Auto-placed properties.** Reference is emitted 2.54 mm above the
  anchor and Value 2.54 mm below. Footprint and Datasheet go at the
  anchor, hidden. KiCAD will re-auto-place these the next time the
  schematic is opened — our positioning is a bootstrapping concern only.

Status enum
-----------

* **ok**                    — instance appended and written.
* **dry_run**               — caller passed ``dry_run=True``; would
                              have added the instance.
* **sch_not_found**         — path missing, not a file, or wrong suffix.
* **invalid_schema**        — parseable but top_head isn't
                              ``kicad_sch``, or the root is missing its
                              ``(uuid ...)`` (needed for the instances
                              block's path).
* **parse_failed**          — the SEXPR parser rejected the file bytes.
* **lib_symbol_not_found**  — the requested ``lib_id`` isn't present
                              in this schematic's ``lib_symbols``.
                              Caller must embed the library entry first.
* **write_failed**          — atomic save / round-trip validation raised.

Backend rationale is the same as M12: IPC on KiCAD 9.x has no schematic
mutation writer, so the SEXPR backend owns this. ``required_backends``
pins SEXPR so the dispatcher surfaces BACKEND_UNAVAILABLE cleanly if the
server never probed.
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
    find_scalar_string,
    flag_node,
    int_node,
    load_sexpr_doc,
    slist,
    uuid_node,
)

log = logging.getLogger(__name__)


# Vertical offset used to place Reference above and Value below the
# symbol anchor. 2.54 mm is one 100-mil grid step — KiCAD's native
# snap.
_PROPERTY_OFFSET_MM = 2.54


# -- input / output --------------------------------------------------------


class SchAddSymbolInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    lib_id: str = Field(
        ...,
        description=(
            "Library-qualified symbol name, e.g. 'Device:R_Small'. "
            "MUST already be embedded in this schematic's lib_symbols "
            "section. If it isn't, the call returns "
            "lib_symbol_not_found — embed the lib entry first (a "
            "future sch_embed_lib_symbol tool will automate that)."
        ),
    )
    reference: str = Field(
        ...,
        description=(
            "Reference designator (e.g. 'R1', 'U3'). Conventionally "
            "'?' for unannotated placements, but we don't enforce — the "
            "caller owns annotation logic."
        ),
    )
    value: str = Field(
        ...,
        description=(
            "Component value string (e.g. '10k', '100nF', 'LM7805'). "
            "Empty string is allowed."
        ),
    )
    at_x: float = Field(
        ...,
        description="X coordinate of the symbol anchor in millimetres.",
    )
    at_y: float = Field(
        ...,
        description="Y coordinate of the symbol anchor in millimetres.",
    )
    angle: float = Field(
        default=0.0,
        description=(
            "Rotation angle in degrees. KiCAD snaps to multiples of 90 "
            "internally; non-multiples are written through verbatim."
        ),
    )
    footprint: str = Field(
        default="",
        description=(
            "Footprint lib path (e.g. 'Resistor_SMD:R_0603_1608Metric'). "
            "Empty string = leave blank (assign via assign_footprint or "
            "the CvPcb flow later)."
        ),
    )
    unit: int = Field(
        default=1,
        ge=1,
        description=(
            "Unit number for multi-unit symbols. 1 for single-unit "
            "symbols (resistors, capacitors, power flags)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, report the instance that would be added without "
            "writing. Per ADR-0008, every mutating tool supports dry-run."
        ),
    )


class SchAddSymbolOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "lib_symbol_not_found",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path to the .kicad_sch. Null only when the "
            "file couldn't be located at all."
        ),
    )
    reference: str | None = Field(
        default=None,
        description="Echo of the reference designator as written.",
    )
    instance_uuid: str | None = Field(
        default=None,
        description=(
            "UUID assigned to the new symbol instance. Populated on "
            "status=ok. Null for dry_run (the UUID only exists once the "
            "write lands) and for all error statuses."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses (reason + actionable hint).",
    )


# -- tool ------------------------------------------------------------------


class SchAddSymbolTool(Tool[SchAddSymbolInput, SchAddSymbolOutput]):
    """Append a symbol instance to a .kicad_sch via the SEXPR backend."""

    name = "sch_add_symbol"
    version = "0.2.0"
    description = (
        "Place a symbol instance on a .kicad_sch schematic. "
        "IMPORTANT: When creating a new circuit or subcircuit, present the "
        "full design proposal (topology, BOM table, connectivity) to the "
        "user and get explicit approval BEFORE calling this tool. Use the "
        "'circuit-proposal' prompt format. Individual component tweaks or "
        "replacements do not need a full proposal. "
        "Requires the symbol's lib_id to be already embedded in "
        "lib_symbols. Generates UUIDs, synthesizes default properties "
        "(Reference/Value/Footprint/Datasheet), and emits one pin entry "
        "per pin in the lib symbol. Plan coordinates on KiCAD's 100-mil "
        "schematic grid (2.54 mm default; see safety.grid_snap_mm). "
        "Supports dry_run; snapshots the project before writing per "
        "ADR-0008."
    )
    input_model = SchAddSymbolInput
    output_model = SchAddSymbolOutput
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

    async def run(self, input: SchAddSymbolInput) -> SchAddSymbolOutput:
        # 1. Preflight path validation. Same shape as M12.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddSymbolOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddSymbolOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddSymbolOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_add_symbol runs on a "
                    "schematic file, not a project or board."
                ),
            )

        # 2. Parse + shape check.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchAddSymbolOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddSymbolOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'. Is this really a "
                    "schematic file?"
                ),
            )

        # 3. The instance's (instances ...) block encodes a sheet path
        # rooted at the schematic's top-level UUID. Without it we can't
        # produce a valid instance entry; reject rather than guess.
        top_uuid = find_scalar_string(doc.root, "uuid")
        if top_uuid is None:
            return SchAddSymbolOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    "schematic has no (uuid \"...\") at root; the instances "
                    "block can't be constructed without it. Open the file "
                    "in KiCAD once — it will assign a top-level UUID."
                ),
            )

        # 4. Find the lib_symbol the caller asked for. First ship requires
        # it to already be embedded; a future sch_embed_lib_symbol tool
        # will lift an entry out of the global/project lib tables.
        lib_symbols = doc.root.find("lib_symbols")
        lib_symbol = _find_lib_symbol(lib_symbols, input.lib_id) if lib_symbols else None
        if lib_symbol is None:
            return SchAddSymbolOutput(
                status="lib_symbol_not_found",
                sch_path=str(sch_path),
                note=(
                    f"lib_id {input.lib_id!r} is not present in this "
                    "schematic's lib_symbols block. Embed the library "
                    "entry first (KiCAD does this automatically when you "
                    "place the symbol via the GUI). A future "
                    "sch_embed_lib_symbol tool will automate this from "
                    "the lib table."
                ),
            )

        # 5. Enumerate the lib_symbol's pin numbers. Deduplicated in
        # declaration order — multi-unit symbols repeat pins across
        # nested unit sub-symbols; we want one instance pin per unique
        # number.
        pin_numbers = _extract_pin_numbers(lib_symbol)

        # 6. Apply grid snap per safety.grid_snap_mm.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {"at_x": input.at_x, "at_y": input.at_y}, grid_snap_mm
        )
        at_x, at_y = snapped["at_x"], snapped["at_y"]

        # 6b. Anti-crowding check (cites KICAD-317). Origin-to-origin
        # distance is a fast first-pass heuristic; truly bbox-aware
        # collision needs lib_symbol geometry parsing and lives in a
        # later iteration. Non-blocking — the LLM can still place a tight
        # cluster if it really wants to, but the warning surfaces the
        # smell so reviewers and follow-up calls can react.
        spacing_warning: str | None = None
        spacing_threshold = (
            self._config.safety.symbol_spacing_warn_mm
            if self._config is not None
            else 5.08
        )
        if spacing_threshold is not None:
            nearby = _find_crowding_symbols(
                doc.root,
                at_x=at_x,
                at_y=at_y,
                threshold_mm=spacing_threshold,
            )
            if nearby:
                ref, nx, ny, dist = nearby[0]
                spacing_warning = (
                    f"symbol crowding: new placement at ({at_x}, {at_y}) is "
                    f"{dist:.2f} mm from existing symbol {ref!r} at "
                    f"({nx}, {ny}) — below threshold "
                    f"{spacing_threshold} mm ({len(nearby)} symbol(s) too "
                    "close total). Component bodies and reference/value labels "
                    "are likely to overlap; spread placements out — target "
                    "~7-10 mm minimum between adjacent components, more "
                    "between functional blocks. Cites KICAD-317."
                )

        # 7. Plan the instance. On dry_run we bail out with enough info
        # to describe what would be added; no UUIDs are allocated so
        # successive dry_run calls don't drift.
        if input.dry_run:
            out_dry = SchAddSymbolOutput(
                status="dry_run",
                sch_path=str(sch_path),
                reference=input.reference,
                instance_uuid=None,
                note=(
                    f"dry_run=True; would add instance of {input.lib_id!r} "
                    f"as reference {input.reference!r} at "
                    f"({at_x}, {at_y}, angle={input.angle}) "
                    f"with {len(pin_numbers)} pin(s). Re-run with "
                    "dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            if spacing_warning is not None:
                out_dry.meta.warnings.append(spacing_warning)
            return out_dry

        # 8. Generate UUIDs for the instance and each pin. Doing this
        # only on the write path keeps dry_run stateless.
        instance_uuid = str(uuid_mod.uuid4())
        pin_uuids = {num: str(uuid_mod.uuid4()) for num in pin_numbers}

        # 9. Derive the project name for the instances block. KiCAD
        # writes the stem of the neighboring .kicad_pro; empty string
        # for a standalone schematic. KiCAD rewrites this on next open
        # if it's wrong, so being conservative here is fine.
        project_name = _derive_project_name(sch_path)

        symbol_node = _build_symbol_instance(
            lib_id=input.lib_id,
            reference=input.reference,
            value=input.value,
            at_x=at_x,
            at_y=at_y,
            angle=input.angle,
            footprint=input.footprint,
            unit=input.unit,
            instance_uuid=instance_uuid,
            pin_uuids=pin_uuids,
            project_name=project_name,
            top_uuid=top_uuid,
        )

        # Append at the end of the root — KiCAD re-orders on open if it
        # cares. The schematic's structural nodes (lib_symbols, symbol
        # instances, wires, junctions, …) sit at the top level in no
        # particular required order.
        doc.root.append(symbol_node)

        # 9. Snapshot before write. Same semantics as M12.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_symbol:{sch_path.name}:{input.reference}",
            )
        except SnapshotError as exc:
            return SchAddSymbolOutput(
                status="write_failed",
                sch_path=str(sch_path),
                reference=input.reference,
                note=(
                    f"snapshot failed before write: {exc}. No mutation "
                    "was applied. Fix the snapshot path or set "
                    "safety.snapshot_mode='off' to skip."
                ),
            )

        # 10. Atomic save. Round-trip validation is load-bearing here —
        # the synthesized tree is fully dirty, so every byte goes
        # through canonical serialization.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddSymbolOutput(
                status="write_failed",
                sch_path=str(sch_path),
                reference=input.reference,
                note=(
                    f"save failed after snapshot: {exc}. The snapshot "
                    "captures the pre-mutation state; restore from there "
                    "if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddSymbolOutput(
            status="ok",
            sch_path=str(sch_path),
            reference=input.reference,
            instance_uuid=instance_uuid,
        )
        out.meta.snapshot_ref = snapshot_ref
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        if spacing_warning is not None:
            out.meta.warnings.append(spacing_warning)
        return out


# -- helpers ---------------------------------------------------------------


def _find_crowding_symbols(
    root: SList,
    *,
    at_x: float,
    at_y: float,
    threshold_mm: float,
) -> list[tuple[str, float, float, float]]:
    """Return symbol instances whose origin is within ``threshold_mm`` of
    ``(at_x, at_y)``.

    Each tuple is ``(reference, x, y, distance_mm)``. Only top-level
    ``(symbol …)`` children of the schematic root are scanned (instances).
    The ``lib_symbols`` block sits at the same depth but its children
    are wrapped inside ``(lib_symbols …)`` so they don't appear here.
    Results sorted nearest-first; reference is read from each instance's
    ``(property "Reference" "…")`` entry, falling back to ``"?"`` when
    absent or malformed.
    """
    hits: list[tuple[str, float, float, float]] = []
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
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
        if distance >= threshold_mm:
            continue
        # Pull the Reference for a more useful warning message.
        ref = "?"
        for prop in child.items:
            if (
                isinstance(prop, SList)
                and prop.head == "property"
                and len(prop.items) >= 3
            ):
                key = prop.items[1]
                val = prop.items[2]
                if (
                    isinstance(key, SAtom)
                    and isinstance(val, SAtom)
                    and key.text == "Reference"
                ):
                    ref = val.text
                    break
        hits.append((ref, x, y, distance))
    hits.sort(key=lambda t: t[3])
    return hits


def _find_lib_symbol(lib_symbols: SList, lib_id: str) -> SList | None:
    """Return the ``(symbol "lib_id" ...)`` child matching ``lib_id``.

    The lib_id lives as the first non-head atom of a nested symbol
    entry inside the schematic's ``lib_symbols`` section — i.e. the
    atom at index 1 of each ``(symbol "..." ...)`` child. We compare
    against it directly; no library-table resolution happens here.
    """
    for child in lib_symbols.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) < 2:
            continue
        name_atom = child.items[1]
        if isinstance(name_atom, SAtom) and name_atom.text == lib_id:
            return child
    return None


def _extract_pin_numbers(lib_symbol: SList) -> list[str]:
    """Return pin numbers declared in ``lib_symbol``, deduplicated.

    Pins live inside nested unit sub-symbols (``(symbol "X_0_1" (pin ...))``
    etc.) as ``(pin <type> <line_style> (at ...) (length ...) (name ...)
    (number "N" ...))``. We walk the whole subtree and pull the
    ``(number "..." ...)`` payload from each ``(pin ...)`` we find.

    Multi-unit + demorgan-alternate symbols repeat the same pin number
    across nested blocks; we keep the first sighting and discard later
    duplicates so the instance gets one ``(pin "N" (uuid ...))`` per
    unique number.
    """
    seen: set[str] = set()
    numbers: list[str] = []
    for node in lib_symbol.walk():
        if not isinstance(node, SList) or node.head != "pin":
            continue
        number_node = node.find("number")
        if number_node is None or len(number_node.items) < 2:
            continue
        number_atom = number_node.items[1]
        if not isinstance(number_atom, SAtom):
            continue
        if number_atom.text in seen:
            continue
        seen.add(number_atom.text)
        numbers.append(number_atom.text)
    return numbers


def _derive_project_name(sch_path: Path) -> str:
    """Return the project stem (``.kicad_pro`` neighbor) or empty string.

    KiCAD writes this as the first child of ``(instances (project ...))``.
    When it's wrong, KiCAD rewrites it on the next open, so a
    best-effort discovery here is fine. We check same-directory-only —
    walking upward would risk picking a parent project by accident.
    """
    parent = sch_path.parent
    try:
        candidates = sorted(parent.glob("*.kicad_pro"))
    except OSError:
        return ""
    if not candidates:
        return ""
    return candidates[0].stem


def _property_node(
    name: str,
    value: str,
    *,
    at_x: float,
    at_y: float,
    angle: float = 0.0,
    hidden: bool = False,
) -> SList:
    """``(property "Name" "Value" (at X Y angle) (effects ...))``.

    Uses the 3-atom ``(at X Y angle)`` form via ``at_node_explicit``
    because KiCAD 10's strict parser requires the angle atom even when
    it is zero for property nodes inside a ``(symbol ...)`` block —
    omitting it blows up with ``need a number for 'text angle'`` at
    schematic load.
    """
    return slist(
        atom("property"),
        atom(name, quoted=True),
        atom(value, quoted=True),
        at_node_explicit(at_x, at_y, angle),
        effects_node(hidden=hidden),
    )


def _build_symbol_instance(
    *,
    lib_id: str,
    reference: str,
    value: str,
    at_x: float,
    at_y: float,
    angle: float,
    footprint: str,
    unit: int,
    instance_uuid: str,
    pin_uuids: dict[str, str],
    project_name: str,
    top_uuid: str,
) -> SList:
    """Assemble the top-level ``(symbol ...)`` block for a new instance.

    Layout mirrors what eeschema emits in KiCAD 9.x, trimmed to the
    fields we actually need. KiCAD tolerates extra properties being
    absent — Description, ki_keywords, etc. are only needed if the
    caller wants them searchable in the component browser.
    """
    # Property positions: Reference above, Value below, Footprint +
    # Datasheet at the anchor (hidden). These are placeholders; KiCAD
    # repositions them on next open if the user enables field
    # auto-placement. Kept simple deliberately — no geometric reasoning
    # about pin bounds here.
    ref_y = at_y - _PROPERTY_OFFSET_MM
    val_y = at_y + _PROPERTY_OFFSET_MM

    properties: list[SList] = [
        _property_node("Reference", reference, at_x=at_x, at_y=ref_y),
        _property_node("Value", value, at_x=at_x, at_y=val_y),
        _property_node(
            "Footprint", footprint, at_x=at_x, at_y=at_y, hidden=True
        ),
        _property_node("Datasheet", "~", at_x=at_x, at_y=at_y, hidden=True),
    ]

    # One (pin "N" (uuid "...")) per unique pin number in the lib
    # symbol. Ordering matches declaration order in the lib.
    pin_nodes: list[SList] = []
    for number, pin_uuid in pin_uuids.items():
        pin_nodes.append(
            slist(
                atom("pin"),
                atom(number, quoted=True),
                uuid_node(pin_uuid),
            )
        )

    # (instances (project "<name>" (path "/<top_uuid>" (reference R1) (unit 1))))
    # Reference here is the *authoritative* one for the sheet path;
    # the (property "Reference" ...) above is the visible label. They
    # should agree — we emit them together.
    instances_node = slist(
        atom("instances"),
        slist(
            atom("project"),
            atom(project_name, quoted=True),
            slist(
                atom("path"),
                atom(f"/{top_uuid}", quoted=True),
                slist(atom("reference"), atom(reference, quoted=True)),
                int_node("unit", unit),
            ),
        ),
    )

    items: list[SAtom | SList] = [
        atom("symbol"),
        slist(atom("lib_id"), atom(lib_id, quoted=True)),
        # Schematic-instance symbol positions require the 3-atom form
        # in KiCAD 10 — ``at_node`` would elide the zero-angle case and
        # break load-time parsing.
        at_node_explicit(at_x, at_y, angle),
        int_node("unit", unit),
        flag_node("exclude_from_sim", False),
        flag_node("in_bom", True),
        flag_node("on_board", True),
        flag_node("dnp", False),
        uuid_node(instance_uuid),
    ]
    items.extend(properties)
    items.extend(pin_nodes)
    items.append(instances_node)
    return SList(items=items)


__all__ = [
    "SchAddSymbolInput",
    "SchAddSymbolOutput",
    "SchAddSymbolTool",
    "_find_crowding_symbols",
]

"""lib_add_symbol — create or extend a .kicad_sym symbol library (M44).

The first **library authoring** mutator. Prior milestones shipped
``lib_list_symbols`` (M30) and ``lib_search_symbol`` (M31) that *read*
library contents; this is the counterpart that lets an LLM *add* a
new symbol when a chip isn't in any installed library yet.

Why this exists
---------------

Custom boards frequently use parts without a ready-made KiCAD symbol —
a niche regulator, a new MCU variant, a specialty sensor. Until now
our flow dead-ended at that point: the schematic tools assume the
lib_id you want is already on disk somewhere. This tool closes the
loop so a prompt-driven flow can read a datasheet, synthesize a
symbol, embed it, and place instances without leaving MCP.

Scope of the first ship
-----------------------

* **Single symbol per call.** Batch authoring can wait; the common
  case is one part at a time.
* **One body graphic: a rectangle.** A rectangle is enough to anchor
  the pins for 80% of real parts (ICs, connectors, modules). Curves,
  pie slices, complex shapes are not supported — eeschema is still
  the right tool for fine-tuning appearance.
* **No BOM/simulation sugar.** We stamp the three mandatory KiCAD 10
  attribute flags (``exclude_from_sim``, ``in_bom``, ``on_board``) on
  every new symbol but don't expose them in the input model. Users
  who need simulation models or exotic BOM flags can edit in eeschema
  after ``lib_add_symbol`` creates the shell.
* **Standard five properties + optional keywords/fp_filters.** The
  KiCAD-canonical ``Reference`` / ``Value`` / ``Footprint`` /
  ``Datasheet`` / ``Description`` are always emitted; ``ki_keywords``
  and ``ki_fp_filters`` land only when the caller supplies them.

File bootstrap
--------------

If ``lib_path`` does not exist, the tool writes a fresh
``(kicad_symbol_lib ...)`` with the KiCAD 10 version stamp
(``20241209``), ``kicad_symbol_editor`` generator, and ``9.0``
generator version. That matches what the KiCAD 10 GUI emits when you
save a newly-created library. If the file exists but has the wrong
top-head, we refuse (``invalid_schema``) rather than clobber data.

Conflict policy
---------------

If a symbol with the same ``symbol_name`` already exists, we return
``symbol_exists`` without mutating. Overwrites require an explicit
``overwrite=True`` flag (opt-in; no silent replacement).

Status enum
-----------

* **ok**                   — symbol appended (or overwritten) and written.
* **dry_run**              — caller passed ``dry_run=True``.
* **invalid_input**        — empty name, bad pin table, bad file suffix.
* **invalid_schema**       — existing file's top-head isn't
                             ``kicad_symbol_lib``.
* **parse_failed**         — SEXPR parser rejected existing file bytes.
* **symbol_exists**        — symbol with ``symbol_name`` already present
                             and ``overwrite`` is False.
* **write_failed**         — snapshot / atomic save / round-trip raised.

Backend: SEXPR, required. No IPC writer exists for library authoring
on KiCAD 9.x or 10.x.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import (
    at_node_explicit,
    atom,
    effects_node,
    flag_node,
    fmt_mm,
    slist,
)

log = logging.getLogger(__name__)


# KiCAD 10 format stamps for a fresh .kicad_sym. Mirror what the GUI
# writes on "File → New Library" so round-trip validation against a
# subsequent GUI save is a no-op. Bump in lockstep when upstream moves.
_KICAD_SYM_VERSION = "20241209"
_KICAD_SYM_GENERATOR = "kicad_symbol_editor"
_KICAD_SYM_GENERATOR_VERSION = "9.0"

# Accepted pin electrical types (KiCAD 10's enumeration). Unknown
# values will be rejected at input-validation time with a pointer back
# to the valid set so callers can fix without hunting through docs.
_VALID_PIN_ELECTRICAL = frozenset(
    {
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
)

# Accepted pin graphic shapes. ``line`` is the default — a straight
# pin stub without decoration.
_VALID_PIN_SHAPES = frozenset(
    {
        "line",
        "inverted",
        "clock",
        "inverted_clock",
        "input_low",
        "clock_low",
        "output_low",
        "edge_clock_high",
        "non_logic",
    }
)


# -- input sub-models ------------------------------------------------------


class LibSymbolPin(BaseModel):
    """One pin on the new symbol.

    ``x`` / ``y`` is the pin's *anchor* — the tip that connects to the
    schematic wire, not the pin-label endpoint. ``angle`` in KiCAD lib
    symbols encodes which way the pin extends: 0 right, 90 up, 180
    left, 270 down. ``length`` is the graphical stub length in mm.
    """

    model_config = ConfigDict(extra="forbid")

    number: str = Field(
        ...,
        description=(
            "Pin number as it appears in the footprint (``'1'``, "
            "``'A3'``, ``'VDD'``). Stored verbatim; KiCAD compares "
            "textually against footprint pad numbers."
        ),
    )
    name: str = Field(
        ...,
        description=(
            "Pin name (``'VCC'``, ``'SCK'``, ``'~'`` for a nameless pin). "
            "Shown as the pin's label inside the symbol body."
        ),
    )
    x: float = Field(..., description="Anchor X in mm, relative to the symbol origin.")
    y: float = Field(..., description="Anchor Y in mm, relative to the symbol origin.")
    length: float = Field(
        default=2.54,
        ge=0.0,
        description=(
            "Pin stub length in mm. 2.54 (= 100 mil) is the eeschema "
            "default and matches KiCAD's grid. Length 0 is valid and "
            "used for power-symbol implicit pins that should not show."
        ),
    )
    angle: float = Field(
        default=0.0,
        description=(
            "Which way the pin extends from the anchor, in degrees: "
            "0 = right, 90 = up, 180 = left, 270 = down. Non-multiples "
            "of 90 are accepted but unusual."
        ),
    )
    electrical_type: str = Field(
        default="passive",
        description=(
            "KiCAD electrical type. Drives ERC: an 'input' connected to "
            "another 'input' raises a warning, 'power_in' must see a "
            "'power_out' driver, etc. One of: "
            + ", ".join(sorted(_VALID_PIN_ELECTRICAL))
            + "."
        ),
    )
    shape: str = Field(
        default="line",
        description=(
            "Graphical shape of the pin. 'line' is the default straight "
            "stub. 'inverted' adds a bubble (active-low). One of: "
            + ", ".join(sorted(_VALID_PIN_SHAPES))
            + "."
        ),
    )
    hide: bool = Field(
        default=False,
        description=(
            "Hide the pin (mainly used for power symbols' implicit pin)."
        ),
    )

    @field_validator("electrical_type")
    @classmethod
    def _check_electrical(cls, v: str) -> str:
        if v not in _VALID_PIN_ELECTRICAL:
            raise ValueError(
                f"unknown pin electrical_type {v!r}; must be one of "
                f"{sorted(_VALID_PIN_ELECTRICAL)}"
            )
        return v

    @field_validator("shape")
    @classmethod
    def _check_shape(cls, v: str) -> str:
        if v not in _VALID_PIN_SHAPES:
            raise ValueError(
                f"unknown pin shape {v!r}; must be one of "
                f"{sorted(_VALID_PIN_SHAPES)}"
            )
        return v


class LibSymbolBodyRect(BaseModel):
    """Optional rectangle body graphic.

    A rectangle from ``(start_x, start_y)`` to ``(end_x, end_y)``.
    KiCAD convention: ``start`` is usually the top-left in screen
    coordinates (i.e. ``end_y < start_y`` since Y is flipped), but we
    don't enforce — any pair of opposing corners is valid.
    """

    model_config = ConfigDict(extra="forbid")

    start_x: float = Field(..., description="Rectangle corner X in mm.")
    start_y: float = Field(..., description="Rectangle corner Y in mm.")
    end_x: float = Field(..., description="Opposite corner X in mm.")
    end_y: float = Field(..., description="Opposite corner Y in mm.")


# -- input / output --------------------------------------------------------


class LibAddSymbolInput(BaseModel):
    lib_path: Path = Field(
        ...,
        description=(
            "Path to the .kicad_sym library file. Created if missing "
            "with a KiCAD 10 header; must end in .kicad_sym."
        ),
    )
    symbol_name: str = Field(
        ...,
        description=(
            "Name of the new symbol entry (e.g. 'LM7805', 'AP2112K-3.3'). "
            "Must be non-empty and unique within the library. To embed "
            "this symbol in a schematic later you'll address it as "
            "'<lib_nickname>:<symbol_name>'."
        ),
    )
    reference: str = Field(
        default="U",
        description=(
            "Default reference-designator prefix (e.g. 'R', 'C', 'U', 'Q', "
            "'#PWR' for power symbols). Becomes the Reference property "
            "value; eeschema annotates instances as '<prefix><number>'."
        ),
    )
    value: str = Field(
        default="",
        description=(
            "Default Value shown in the library browser — usually the "
            "same as ``symbol_name`` for specific parts, a generic "
            "label ('R', 'C') for templates. Empty falls back to "
            "``symbol_name``."
        ),
    )
    footprint: str = Field(
        default="",
        description=(
            "Default Footprint fully-qualified name (e.g. "
            "'Package_TO_SOT_THT:TO-220-3_Vertical'). Empty leaves the "
            "user to assign via CvPcb or footprint property edit."
        ),
    )
    datasheet: str = Field(
        default="",
        description=(
            "Datasheet URL or local path. KiCAD stores '~' as 'none "
            "available' — we accept any string verbatim."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Human-readable description shown in the library browser and "
            "component picker. One-line summary; multi-line is fine but "
            "gets clipped in the UI."
        ),
    )
    keywords: str = Field(
        default="",
        description=(
            "Space-separated keywords for symbol search "
            "('regulator ldo 3.3V'). Becomes the ``ki_keywords`` "
            "property; omitted if empty."
        ),
    )
    footprint_filters: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns KiCAD uses to narrow the footprint picker "
            "('TO-220-3*', 'R_0603_*'). Becomes the ``ki_fp_filters`` "
            "property (space-joined); omitted if the list is empty."
        ),
    )
    pins: list[LibSymbolPin] = Field(
        default_factory=list,
        description=(
            "Pin table. Each entry becomes a (pin ...) child inside "
            "``<symbol_name>_1_1``. Pin numbers must be unique. An empty "
            "list is accepted (graphics-only symbols, e.g. logos) but "
            "will fail ERC once placed."
        ),
    )
    body_rect: LibSymbolBodyRect | None = Field(
        default=None,
        description=(
            "Optional rectangle body outline. Emitted in ``<name>_0_1`` "
            "so it's drawn once regardless of unit. Leave null for "
            "power symbols or pin-only icons."
        ),
    )
    power: bool = Field(
        default=False,
        description=(
            "Mark this symbol as a power symbol (adds ``(power)`` "
            "annotation). Forces KiCAD to treat it as a global-net "
            "source rather than a placeable device."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "If True and a symbol with the same name exists, replace it "
            "in place. Defaults to False — callers must opt in to "
            "overwrites to protect against accidental clobbering."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, report the planned write without touching the "
            "file. Same contract as every other MUTATE tool per ADR-0008."
        ),
    )


class LibAddSymbolOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "invalid_input",
        "invalid_schema",
        "parse_failed",
        "symbol_exists",
        "write_failed",
    ]
    lib_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .kicad_sym library.",
    )
    symbol_name: str | None = Field(
        default=None,
        description="Echo of the symbol name written.",
    )
    pin_count: int = Field(
        default=0,
        description="Number of pins emitted in the new symbol.",
    )
    created_library: bool = Field(
        default=False,
        description=(
            "True when the .kicad_sym file did not exist and was "
            "bootstrapped by this call."
        ),
    )
    overwrote: bool = Field(
        default=False,
        description=(
            "True when an existing symbol was replaced under overwrite=True."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibAddSymbolTool(Tool[LibAddSymbolInput, LibAddSymbolOutput]):
    """Create or extend a .kicad_sym library with a new symbol."""

    name = "lib_add_symbol"
    version = "0.1.0"
    description = (
        "Author a new symbol in a KiCAD .kicad_sym library. Creates the "
        "library file if missing (KiCAD 10 format). Emits the five "
        "standard properties (Reference/Value/Footprint/Datasheet/"
        "Description) plus optional ki_keywords / ki_fp_filters, an "
        "optional rectangle body graphic, and a (pin ...) per entry in "
        "the pin table. Conflicts return symbol_exists unless "
        "overwrite=True. Supports dry_run; snapshots before write per "
        "ADR-0008."
    )
    input_model = LibAddSymbolInput
    output_model = LibAddSymbolOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    async def run(self, input: LibAddSymbolInput) -> LibAddSymbolOutput:
        # 1. Input-level sanity checks that Pydantic can't express.
        if not input.symbol_name:
            return LibAddSymbolOutput(
                status="invalid_input",
                note="symbol_name must be non-empty.",
            )
        if input.lib_path.suffix.lower() != ".kicad_sym":
            return LibAddSymbolOutput(
                status="invalid_input",
                note=(
                    f"lib_path must end in .kicad_sym; got "
                    f"{input.lib_path.suffix!r}."
                ),
            )
        # Pin-number uniqueness — KiCAD tolerates duplicates but they
        # confuse netlisting downstream and almost always reflect a
        # typo rather than a deliberate short. Catch early.
        seen_numbers: set[str] = set()
        for pin in input.pins:
            if pin.number in seen_numbers:
                return LibAddSymbolOutput(
                    status="invalid_input",
                    note=(
                        f"duplicate pin number {pin.number!r}; pin "
                        "numbers must be unique within a symbol."
                    ),
                )
            seen_numbers.add(pin.number)

        lib_path = input.lib_path.expanduser().resolve()

        # 2. Either parse the existing library or bootstrap a fresh one.
        created_library = False
        doc: SexprDocument
        if lib_path.exists():
            if not lib_path.is_file():
                return LibAddSymbolOutput(
                    status="invalid_input",
                    lib_path=str(lib_path),
                    note=f"lib_path exists but is not a regular file: {lib_path}",
                )
            try:
                doc = SexprDocument.from_path(lib_path)
            except SexprParseError as exc:
                return LibAddSymbolOutput(
                    status="parse_failed",
                    lib_path=str(lib_path),
                    note=f"SEXPR parse failed: {exc}",
                )
            if doc.top_head != "kicad_symbol_lib":
                return LibAddSymbolOutput(
                    status="invalid_schema",
                    lib_path=str(lib_path),
                    note=(
                        f"expected top-level '(kicad_symbol_lib ...)' but "
                        f"got '({doc.top_head or '?'} ...)'."
                    ),
                )
        else:
            # Synthesize a minimal library in memory. We hold off on
            # writing it to disk until after dry-run short-circuit +
            # snapshot; on error paths the file never appears.
            doc = _bootstrap_library(lib_path)
            created_library = True

        # 3. Conflict check. When overwrite=True we still locate the
        # existing symbol so we can remove it before appending the new
        # one (rather than blindly appending a duplicate — KiCAD picks
        # whichever it sees first and callers get silent ghost entries).
        existing_idx: int | None = None
        for idx, child in enumerate(doc.root.items):
            if (
                isinstance(child, SList)
                and child.head == "symbol"
                and _symbol_name_of(child) == input.symbol_name
            ):
                existing_idx = idx
                break
        if existing_idx is not None and not input.overwrite:
            return LibAddSymbolOutput(
                status="symbol_exists",
                lib_path=str(lib_path),
                symbol_name=input.symbol_name,
                note=(
                    f"symbol {input.symbol_name!r} already exists in "
                    f"{lib_path.name}. Pass overwrite=True to replace."
                ),
            )

        # 4. Dry-run short-circuit. Report enough to describe the write
        # without allocating IDs or writing bytes.
        if input.dry_run:
            action = "replace" if existing_idx is not None else "append"
            hint = (
                f" Would also create library file at {lib_path}."
                if created_library
                else ""
            )
            return LibAddSymbolOutput(
                status="dry_run",
                lib_path=str(lib_path),
                symbol_name=input.symbol_name,
                pin_count=len(input.pins),
                created_library=False,  # dry-run doesn't create anything
                overwrote=False,
                note=(
                    f"dry_run=True; would {action} symbol "
                    f"{input.symbol_name!r} with {len(input.pins)} pin(s)."
                    f"{hint}"
                ),
            )

        # 5. Synthesize the new symbol node.
        new_symbol = _build_symbol_node(
            symbol_name=input.symbol_name,
            reference=input.reference,
            value=input.value or input.symbol_name,
            footprint=input.footprint,
            datasheet=input.datasheet,
            description=input.description,
            keywords=input.keywords,
            footprint_filters=input.footprint_filters,
            pins=input.pins,
            body_rect=input.body_rect,
            power=input.power,
        )

        overwrote = False
        if existing_idx is not None:
            doc.root.replace(existing_idx, new_symbol)
            overwrote = True
        else:
            doc.root.append(new_symbol)

        # 6. Snapshot before any filesystem write. Match the sheet-tool
        # pattern — one unified rollback point even though the write is
        # a single file here.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode
        snapshot_ref: str | None = None
        try:
            # Snapshot the library's directory; if the file didn't exist
            # the parent is what we'd need to restore.
            snapshot_ref = snapshot(
                lib_path.parent,
                mode=snapshot_mode,
                reason=f"lib_add_symbol:{lib_path.name}:{input.symbol_name}",
            )
        except SnapshotError as exc:
            return LibAddSymbolOutput(
                status="write_failed",
                lib_path=str(lib_path),
                symbol_name=input.symbol_name,
                note=f"snapshot failed before write: {exc}.",
            )

        # 7. Write. Bootstrap path: ensure the parent directory exists
        # before SexprDocument.save() hits it; for a fresh library the
        # dir might not exist yet.
        try:
            lib_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(lib_path)
        except (OSError, RuntimeError) as exc:
            out_fail = LibAddSymbolOutput(
                status="write_failed",
                lib_path=str(lib_path),
                symbol_name=input.symbol_name,
                note=(
                    f"save failed after snapshot: {exc}. Restore from the "
                    "snapshot if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = LibAddSymbolOutput(
            status="ok",
            lib_path=str(lib_path),
            symbol_name=input.symbol_name,
            pin_count=len(input.pins),
            created_library=created_library,
            overwrote=overwrote,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _symbol_name_of(symbol: SList) -> str | None:
    """Return the first positional name atom of a ``(symbol "..." ...)``."""
    if len(symbol.items) < 2:
        return None
    first = symbol.items[1]
    return first.text if isinstance(first, SAtom) else None


def _bootstrap_library(lib_path: Path) -> SexprDocument:
    """Synthesize an empty KiCAD 10 .kicad_sym in memory.

    Mirrors what the KiCAD 10 symbol editor writes on "File → New
    Library". Parsing it through ``SexprDocument.from_bytes`` keeps the
    result on the same code path as an on-disk file — ``save()`` will
    round-trip-validate the subsequent append exactly like any other
    edit.
    """
    body = (
        "(kicad_symbol_lib\n"
        f'\t(version {_KICAD_SYM_VERSION})\n'
        f'\t(generator "{_KICAD_SYM_GENERATOR}")\n'
        f'\t(generator_version "{_KICAD_SYM_GENERATOR_VERSION}")\n'
        ")\n"
    )
    return SexprDocument.from_bytes(lib_path, body.encode("utf-8"))


def _library_property(
    *,
    name: str,
    value: str,
    at_x: float = 0.0,
    at_y: float = 0.0,
    angle: float = 0.0,
    hidden: bool = False,
) -> SList:
    """``(property "Name" "Value" (at X Y angle) (effects ...))`` for
    lib symbols.

    Uses :func:`at_node_explicit` because KiCAD 10's library-symbol
    parser expects the 3-atom at-form with an explicit angle atom even
    when the angle is zero — same gotcha as sheet properties and
    schematic instance properties, just in a different file type.
    """
    return slist(
        atom("property"),
        atom(name, quoted=True),
        atom(value, quoted=True),
        at_node_explicit(at_x, at_y, angle),
        effects_node(hidden=hidden),
    )


def _build_body_rect(rect: LibSymbolBodyRect) -> SList:
    """``(rectangle (start X Y) (end X Y) (stroke ...) (fill (type none)))``."""
    stroke = slist(
        atom("stroke"),
        slist(atom("width"), atom("0")),
        slist(atom("type"), atom("default")),
    )
    fill = slist(atom("fill"), slist(atom("type"), atom("none")))
    return slist(
        atom("rectangle"),
        slist(atom("start"), atom(fmt_mm(rect.start_x)), atom(fmt_mm(rect.start_y))),
        slist(atom("end"), atom(fmt_mm(rect.end_x)), atom(fmt_mm(rect.end_y))),
        stroke,
        fill,
    )


def _build_pin_node(pin: LibSymbolPin) -> SList:
    """Synthesize one ``(pin <elec> <shape> ...)`` child node.

    Pin text (name + number) always carries an effects block so
    KiCAD doesn't fall back to font defaults that differ per platform.
    The hide flag is positioned between ``length`` and ``name`` per
    KiCAD's own emission order — the parser is lenient but we match
    the canonical form.
    """
    at_pin = at_node_explicit(pin.x, pin.y, pin.angle)
    length_node = slist(atom("length"), atom(fmt_mm(pin.length)))

    name_node = slist(
        atom("name"),
        atom(pin.name, quoted=True),
        effects_node(),
    )
    number_node = slist(
        atom("number"),
        atom(pin.number, quoted=True),
        effects_node(),
    )

    items: list[SAtom | SList] = [
        atom("pin"),
        atom(pin.electrical_type),
        atom(pin.shape),
        at_pin,
        length_node,
    ]
    if pin.hide:
        items.append(flag_node("hide", True))
    items.extend([name_node, number_node])
    return SList(items=items)


def _build_symbol_node(
    *,
    symbol_name: str,
    reference: str,
    value: str,
    footprint: str,
    datasheet: str,
    description: str,
    keywords: str,
    footprint_filters: list[str],
    pins: list[LibSymbolPin],
    body_rect: LibSymbolBodyRect | None,
    power: bool,
) -> SList:
    """Assemble the full ``(symbol "Name" ...)`` node.

    Layout mirrors what the KiCAD 10 symbol editor emits: attribute
    flags → properties → body sub-symbol → pin sub-symbol →
    ``(embedded_fonts no)`` tail. Body graphics live in ``<name>_0_1``
    so they share across all units; pins live in ``<name>_1_1`` — we
    only ship single-unit symbols so there's no ``_2_1``, ``_3_1``, …
    """
    items: list[SAtom | SList] = [atom("symbol"), atom(symbol_name, quoted=True)]
    if power:
        items.append(slist(atom("power")))

    # KiCAD 10 mandatory attribute flags. Same three-of-four as the
    # sheet-node load-bearing flags (no ``dnp`` here — that one's a
    # schematic-instance attribute, not a library-entry attribute).
    items.extend(
        [
            flag_node("exclude_from_sim", False),
            flag_node("in_bom", True),
            flag_node("on_board", True),
        ]
    )

    # Canonical five properties. All at (0, 0, 0) — KiCAD repositions
    # when the user places the symbol anyway. Description is hidden
    # by convention; the other four are visible.
    items.extend(
        [
            _library_property(name="Reference", value=reference),
            _library_property(name="Value", value=value),
            _library_property(name="Footprint", value=footprint),
            _library_property(name="Datasheet", value=datasheet),
            _library_property(name="Description", value=description, hidden=True),
        ]
    )

    if keywords:
        items.append(
            _library_property(name="ki_keywords", value=keywords, hidden=True)
        )
    if footprint_filters:
        items.append(
            _library_property(
                name="ki_fp_filters",
                value=" ".join(footprint_filters),
                hidden=True,
            )
        )

    # Body graphics sub-symbol. KiCAD needs this block to exist even
    # when empty if we have pins — but an empty graphics sub-symbol
    # looks odd on disk, so skip it entirely when there's nothing to
    # draw. KiCAD tolerates missing ``_0_1``.
    if body_rect is not None:
        body_items: list[SAtom | SList] = [
            atom("symbol"),
            atom(f"{symbol_name}_0_1", quoted=True),
            _build_body_rect(body_rect),
        ]
        items.append(SList(items=body_items))

    # Pins sub-symbol. KiCAD places pins inside ``<name>_1_1`` (or
    # ``_0_0`` / ``_0_1`` for power symbols whose pin is implicit on
    # unit 0). Keep it simple — single-unit symbols always use ``_1_1``.
    if pins:
        pin_items: list[SAtom | SList] = [
            atom("symbol"),
            atom(f"{symbol_name}_1_1", quoted=True),
        ]
        for pin in pins:
            pin_items.append(_build_pin_node(pin))
        items.append(SList(items=pin_items))

    # Trailing ``(embedded_fonts no)`` — KiCAD 9+ always writes this;
    # missing it triggers a warning in the library editor.
    items.append(flag_node("embedded_fonts", False))

    return SList(items=items)


__all__ = [
    "LibAddSymbolInput",
    "LibAddSymbolOutput",
    "LibAddSymbolTool",
    "LibSymbolBodyRect",
    "LibSymbolPin",
]

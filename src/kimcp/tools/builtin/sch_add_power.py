"""sch_add_power — place a power port (GND / VCC / +3V3 / …) on a .kicad_sch (M18).

Power ports in KiCAD are a specialization of the generic symbol instance:

* The ``lib_id`` points into the ``power:`` library (e.g. ``power:GND``).
* The lib_symbol carries a ``(power)`` marker so eeschema treats matching
  ports across the project as a single virtual net.
* The instance's reference is ``#PWR?`` (conventionally ``#PWR###`` after
  annotation). That reference is **hidden** — the visible label is the
  Value property, which is the net name.
* ``in_bom`` is **no** (power ports are schematic annotations, not BOM
  items).

KiCAD's standard install ships ``power:GND``, ``power:VCC``, ``power:+3V3``,
etc. A schematic pulls the needed lib_symbols into its own ``lib_symbols``
block the first time one is placed.

Embed preference (canonical > synthesis)
----------------------------------------

When the schematic doesn't already have a ``power:<NET>`` entry, we
**prefer embedding the canonical lib_symbol** from KiCAD's installed
``power.kicad_sym`` over synthesizing a stand-in. Resolution order:

1. ``kimcp.cli.paths.resolve_system_symbol_lib("power")`` locates the
   installer-dropped bundled library (``.../SharedSupport/symbols`` on
   macOS, ``/usr/share/kicad/symbols`` on Linux, etc.).
2. If that library has a ``(symbol "<NET>" ...)`` entry, we deep-clone
   it, rename to ``power:<NET>``, and append to ``lib_symbols``. The
   resulting embedded symbol matches exactly what KiCAD's library
   browser would place — **no ``lib_symbol_mismatch`` ERC warning**.
3. If the bundled library is missing (e.g. KiCAD not installed) or it
   doesn't contain the requested net name (custom rails like
   ``+VIN_IN``), we fall back to synthesizing a minimal stand-in —
   same behavior as before the canonical path landed. A warning is
   surfaced on ``meta.warnings`` so callers know they got a synthetic
   entry and why.

The synthetic stand-in has:

* The ``(power)`` marker (essential — without it, eeschema treats the
  instance as a regular component).
* A single pin of type ``power_in`` so ERC sees the connection.
* A small up-arrow polyline so the symbol is visible in eeschema.
  Users can always swap the lib_symbol out later via
  ``sch_embed_lib_symbol`` once they wire up a project-local power
  library — net connectivity is unaffected by the graphic choice.

Status enum
-----------

* **ok**             — power port appended and written.
* **dry_run**        — caller passed ``dry_run=True``.
* **sch_not_found**  — path missing / not a file / wrong suffix.
* **invalid_schema** — top_head isn't ``kicad_sch``, or the root is
                        missing its top-level ``(uuid ...)``.
* **parse_failed**   — the SEXPR parser rejected the file bytes.
* **invalid_input**  — net_name is empty.
* **write_failed**   — snapshot or atomic save raised.

Backend: SEXPR, required. Same rationale as M14/M15/M16/M17.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.cli.paths import resolve_system_symbol_lib
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
    at_node,
    at_node_explicit,
    atom,
    effects_node,
    find_scalar_string,
    flag_node,
    int_node,
    load_sexpr_doc,
    slist,
    stroke_default_node,
    uuid_node,
)
from kimcp.tools.builtin.sch_add_symbol import (
    _derive_project_name,
    _find_lib_symbol,
)
from kimcp.tools.builtin.sch_embed_lib_symbol import (
    _clone_and_qualify,
    _find_symbol_in_lib,
)

log = logging.getLogger(__name__)


# Same anchoring convention as M14: Reference above, Value below. Power
# ports conventionally render the Value (net name) *above* the anchor
# so the label reads naturally next to the arrow tip. We mirror that.
_VALUE_OFFSET_MM = 3.81


# -- input / output --------------------------------------------------------


class SchAddPowerInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    net_name: str = Field(
        ...,
        description=(
            "Net name. Conventional examples: 'GND', 'VCC', '+3V3', '+5V', "
            "'VDD', 'VSS', '-12V'. Must be non-empty. KiCAD merges all "
            "power ports that share this Value project-wide into a single "
            "virtual net — spelling matters."
        ),
    )
    at_x: float = Field(..., description="Anchor X in millimetres.")
    at_y: float = Field(..., description="Anchor Y in millimetres.")
    angle: float = Field(
        default=0.0,
        description=(
            "Rotation angle in degrees. KiCAD snaps to 0/90/180/270; "
            "non-multiples are written through verbatim."
        ),
    )
    reference: str = Field(
        default="#PWR?",
        description=(
            "Reference designator. '#PWR?' is the unannotated convention; "
            "annotation rewrites it to '#PWR01', '#PWR02', .... The "
            "reference is hidden on power symbols — only the Value (net "
            "name) renders visibly in eeschema."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the power port that would be added without writing.",
    )


class SchAddPowerOutput(ToolOutput):
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
    net_name: str | None = Field(
        default=None, description="Echo of the net name as written."
    )
    lib_id: str | None = Field(
        default=None,
        description="Lib id used — always ``power:<net_name>``.",
    )
    instance_uuid: str | None = Field(
        default=None,
        description="UUID of the new power-port instance (populated on status=ok only).",
    )
    lib_symbol_embedded: bool | None = Field(
        default=None,
        description=(
            "True iff a ``power:<net>`` lib_symbol was appended to the "
            "schematic's lib_symbols block by this call. False if the "
            "lib_symbol was already present (reuse). Null on error / "
            "dry_run."
        ),
    )
    lib_symbol_source: Literal["canonical", "synthesized", "preexisting"] | None = Field(
        default=None,
        description=(
            "Where the lib_symbol came from:\n"
            "* ``canonical`` — cloned from KiCAD's installed "
            "``power.kicad_sym`` (no ERC lib_symbol_mismatch warning).\n"
            "* ``synthesized`` — bundled lib missing or missing this net "
            "(e.g. custom rails like ``+VIN``); we fell back to the "
            "minimal stand-in. Check ``meta.warnings`` for the reason.\n"
            "* ``preexisting`` — the schematic already had ``power:<net>``; "
            "we reused it without inspecting its origin.\n"
            "Null on error / dry_run."
        ),
    )
    note: str | None = Field(
        default=None, description="Diagnostic string for non-ok statuses."
    )


# -- tool ------------------------------------------------------------------


class SchAddPowerTool(Tool[SchAddPowerInput, SchAddPowerOutput]):
    """Place a power port (GND / VCC / +3V3 / …) on a .kicad_sch."""

    name = "sch_add_power"
    version = "0.1.0"
    description = (
        "Place a power port for a named net on a .kicad_sch. Prefers "
        "embedding the canonical ``power:<net>`` lib_symbol from KiCAD's "
        "installed ``power.kicad_sym`` (eliminates lib_symbol_mismatch ERC "
        "warnings); falls back to a minimal synthetic stand-in for nets "
        "absent from the bundled library. Reference hidden, Value visible, "
        "in_bom=no per KiCAD power-port convention. Plan the placement "
        "coordinate on the 100-mil schematic grid (2.54 mm default; see "
        "safety.grid_snap_mm) so the power-port pin lands on the rail wire "
        "endpoint; off-grid inputs are snapped (cites KICAD-318) and a "
        "meta.warnings entry is emitted. Supports dry_run; snapshots "
        "before write per ADR-0008."
    )
    input_model = SchAddPowerInput
    output_model = SchAddPowerOutput
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

    async def run(self, input: SchAddPowerInput) -> SchAddPowerOutput:
        # 1. net_name must be non-empty. Empty net names break ERC and
        # produce opaque downstream errors in KiCAD.
        if not input.net_name:
            return SchAddPowerOutput(
                status="invalid_input",
                sch_path=None,
                note="net_name must be non-empty.",
            )

        # 2. Preflight path validation.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchAddPowerOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchAddPowerOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchAddPowerOutput(
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
            return SchAddPowerOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchAddPowerOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Top-level UUID is required for the instances block's sheet path.
        top_uuid = find_scalar_string(doc.root, "uuid")
        if top_uuid is None:
            return SchAddPowerOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    "schematic has no (uuid \"...\") at root; the instances "
                    "block can't be constructed without it. Open the file "
                    "in KiCAD once — it will assign a top-level UUID."
                ),
            )

        lib_id = f"power:{input.net_name}"

        # Apply grid snap per safety.grid_snap_mm.
        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )
        snapped, snap_warning = apply_grid_snap(
            {"at_x": input.at_x, "at_y": input.at_y}, grid_snap_mm
        )
        at_x, at_y = snapped["at_x"], snapped["at_y"]

        # 4. Dry-run short-circuit. No UUIDs, no lib_symbol embedding.
        if input.dry_run:
            # Surface the probable source so callers can preview whether
            # a canonical embed or a synthesis fallback would happen.
            probed_source = _probe_lib_symbol_source(input.net_name)
            out_dry = SchAddPowerOutput(
                status="dry_run",
                sch_path=str(sch_path),
                net_name=input.net_name,
                lib_id=lib_id,
                instance_uuid=None,
                lib_symbol_embedded=None,
                lib_symbol_source=None,
                note=(
                    f"dry_run=True; would add power port {input.net_name!r} "
                    f"({lib_id}) at ({at_x}, {at_y}) "
                    f"using a {probed_source} lib_symbol. "
                    "Re-run with dry_run=False to apply."
                ),
            )
            if snap_warning is not None:
                out_dry.meta.warnings.append(snap_warning)
            return out_dry

        # 5. Ensure lib_symbols block exists, then find-or-embed
        # power:<NET>. Preference order: reuse existing → clone canonical
        # from the bundled power.kicad_sym → synthesize a minimal stand-in.
        lib_symbols_node = doc.root.find("lib_symbols")
        if lib_symbols_node is None:
            lib_symbols_node = slist(atom("lib_symbols"))
            doc.root.append(lib_symbols_node)

        existing = _find_lib_symbol(lib_symbols_node, lib_id)
        lib_symbol_embedded = False
        lib_symbol_source: Literal["canonical", "synthesized", "preexisting"]
        embed_warning: str | None = None
        if existing is not None:
            # Schematic already has this power entry — reuse it verbatim.
            # We don't try to "upgrade" an existing synthetic to canonical;
            # that would be silent churn on disk and may conflict with a
            # user who intentionally embedded a custom variant.
            lib_symbol_source = "preexisting"
        else:
            canonical, canonical_warning = _try_load_canonical_power_lib_symbol(
                input.net_name
            )
            if canonical is not None:
                lib_symbols_node.append(canonical)
                lib_symbol_embedded = True
                lib_symbol_source = "canonical"
            else:
                lib_symbols_node.append(_build_power_lib_symbol(input.net_name))
                lib_symbol_embedded = True
                lib_symbol_source = "synthesized"
                embed_warning = canonical_warning

        # 6. Synthesize the instance.
        instance_uuid = str(uuid_mod.uuid4())
        pin_uuid = str(uuid_mod.uuid4())
        project_name = _derive_project_name(sch_path)

        instance_node = _build_power_instance(
            net_name=input.net_name,
            reference=input.reference,
            at_x=at_x,
            at_y=at_y,
            angle=input.angle,
            instance_uuid=instance_uuid,
            pin_uuid=pin_uuid,
            project_name=project_name,
            top_uuid=top_uuid,
        )
        doc.root.append(instance_node)

        # 7. Snapshot before write.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_add_power:{sch_path.name}:{input.net_name}",
            )
        except SnapshotError as exc:
            return SchAddPowerOutput(
                status="write_failed",
                sch_path=str(sch_path),
                net_name=input.net_name,
                lib_id=lib_id,
                note=f"snapshot failed before write: {exc}.",
            )

        # 8. Save.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchAddPowerOutput(
                status="write_failed",
                sch_path=str(sch_path),
                net_name=input.net_name,
                lib_id=lib_id,
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchAddPowerOutput(
            status="ok",
            sch_path=str(sch_path),
            net_name=input.net_name,
            lib_id=lib_id,
            instance_uuid=instance_uuid,
            lib_symbol_embedded=lib_symbol_embedded,
            lib_symbol_source=lib_symbol_source,
        )
        out.meta.snapshot_ref = snapshot_ref
        if embed_warning is not None:
            out.meta.warnings.append(embed_warning)
        if snap_warning is not None:
            out.meta.warnings.append(snap_warning)
        return out


# -- helpers ---------------------------------------------------------------


def _power_property_lib(
    name: str,
    value: str,
    *,
    at_x: float = 0.0,
    at_y: float = 0.0,
    hidden: bool = False,
) -> SList:
    """Property node for the embedded power ``(symbol ...)`` lib entry.

    Separate from M14's instance-level ``_property_node`` because the
    default anchor is (0,0) and the hide semantics are simpler — the
    embedded lib_symbol properties are templates that KiCAD overrides
    per instance.
    """
    # KiCAD 10's lib-symbol parser requires the 3-atom ``(at X Y 0)``
    # form for every property inside a ``(symbol ...)`` block that sits
    # under ``lib_symbols`` — omitting the angle atom blows up with
    # ``need a number for 'text angle'`` at schematic load time. Use
    # ``at_node_explicit`` to force the trailing zero.
    return slist(
        atom("property"),
        atom(name, quoted=True),
        atom(value, quoted=True),
        at_node_explicit(at_x, at_y, 0.0),
        effects_node(hidden=hidden),
    )


def _try_load_canonical_power_lib_symbol(
    net_name: str,
) -> tuple[SList | None, str | None]:
    """Attempt to clone the canonical ``power:<net_name>`` from KiCAD's
    installed ``power.kicad_sym``.

    Returns ``(cloned_symbol, None)`` on success and
    ``(None, warning_reason)`` when synthesis must be used instead.

    This is tolerant by design — any failure (no bundled library, parse
    error, net not in library) returns None and lets the caller fall
    back to synthesis. The returned warning string is short and
    user-actionable so it can be appended verbatim to
    ``out.meta.warnings``.

    The cloned tree is lib-qualified (``power:GND``) ready for insertion
    into a schematic's ``lib_symbols``. Shares helpers with
    ``sch_embed_lib_symbol`` so the bytes produced match exactly what
    that tool would produce for the same ``(power, <net>)`` pair.
    """
    bundled = resolve_system_symbol_lib("power")
    if bundled is None:
        return None, (
            "KiCAD's bundled power.kicad_sym was not found; embedded a "
            "synthetic power lib_symbol. Install KiCAD or set a user-"
            "library path to get the canonical GND/VCC/+3V3/... graphics."
        )

    try:
        lib_doc = SexprDocument.from_path(bundled)
    except SexprParseError as exc:
        return None, (
            f"Parsing bundled power.kicad_sym at {bundled} failed ({exc}); "
            "embedded a synthetic power lib_symbol instead. Report as a "
            "KiMCP bug if this recurs."
        )

    if lib_doc.top_head != "kicad_symbol_lib":
        return None, (
            f"Bundled power.kicad_sym at {bundled} has unexpected top "
            f"head {lib_doc.top_head!r}; embedded a synthetic power "
            "lib_symbol instead."
        )

    entry = _find_symbol_in_lib(lib_doc.root, net_name)
    if entry is None:
        return None, (
            f"Net {net_name!r} is not in KiCAD's bundled power library "
            "(common for project-specific rails like +VIN_IN, +V3P3); "
            "embedded a synthetic power lib_symbol. Net connectivity is "
            "unaffected — only the visual graphic is the minimal stand-in."
        )

    return _clone_and_qualify(entry, f"power:{net_name}"), None


def _probe_lib_symbol_source(net_name: str) -> str:
    """Cheap pre-check for dry_run reporting.

    Returns one of ``"canonical"``, ``"synthesized"``. Parses the
    bundled library if found — an acceptable cost for dry-run
    previews; the file is cached by ``SexprDocument.from_path``'s
    underlying ``ParseCache`` so a subsequent real call doesn't pay
    twice.
    """
    bundled = resolve_system_symbol_lib("power")
    if bundled is None:
        return "synthesized"
    try:
        lib_doc = SexprDocument.from_path(bundled)
    except SexprParseError:
        return "synthesized"
    if lib_doc.top_head != "kicad_symbol_lib":
        return "synthesized"
    return "canonical" if _find_symbol_in_lib(lib_doc.root, net_name) else "synthesized"


def _build_power_lib_symbol(net_name: str) -> SList:
    """Synthesize ``(symbol "power:NET" (power) ...)`` for embedding.

    Minimum viable power lib_symbol:

    * ``(power)`` marker — essential; without it, eeschema treats the
      instance as a regular component and ERC stops merging the net.
    * ``(pin_names (offset 0))`` — standard power-symbol pin-name offset.
    * ``(in_bom no)`` / ``(on_board yes)`` — canonical power defaults.
    * One pin of type ``power_in``, hidden (the pin graphic is noise for
      a power port; the symbol body provides the visual).
    * A simple up-arrow polyline as the visible shape. Users wanting the
      canonical KiCAD graphic (GND triangle, VCC circle) can swap the
      lib_symbol out via sch_embed_lib_symbol (M19).

    Property offsets mirror KiCAD's ``power:VCC`` lib entry at the time
    of writing: Reference hidden at (0, -3.81), Value visible at
    (0, 3.81). KiCAD overrides these per instance anyway.
    """
    # Unit 0_1 holds the shared graphics; unit 1_1 holds the pin.
    # Matches the ``<name>_<unit>_<variant>`` convention KiCAD uses.
    graphic_unit = slist(
        atom("symbol"),
        atom(f"{net_name}_0_1", quoted=True),
        slist(
            atom("polyline"),
            slist(
                atom("pts"),
                slist(atom("xy"), atom("-0.762"), atom("1.27")),
                slist(atom("xy"), atom("0"), atom("2.54")),
                slist(atom("xy"), atom("0.762"), atom("1.27")),
                slist(atom("xy"), atom("-0.762"), atom("1.27")),
            ),
            stroke_default_node(),
            slist(atom("fill"), slist(atom("type"), atom("none"))),
        ),
    )

    pin_unit = slist(
        atom("symbol"),
        atom(f"{net_name}_1_1", quoted=True),
        slist(
            atom("pin"),
            atom("power_in"),
            atom("line"),
            at_node(0.0, 0.0, 90.0),
            int_node("length", 0),
            flag_node("hide", True),
            slist(
                atom("name"),
                atom(net_name, quoted=True),
                effects_node(),
            ),
            slist(
                atom("number"),
                atom("1", quoted=True),
                effects_node(),
            ),
        ),
    )

    return slist(
        atom("symbol"),
        atom(f"power:{net_name}", quoted=True),
        slist(atom("power")),
        slist(atom("pin_names"), slist(atom("offset"), atom("0"))),
        flag_node("exclude_from_sim", False),
        flag_node("in_bom", False),
        flag_node("on_board", True),
        _power_property_lib("Reference", "#PWR", at_y=-3.81, hidden=True),
        _power_property_lib("Value", net_name, at_y=3.81),
        _power_property_lib("Footprint", "", hidden=True),
        _power_property_lib("Datasheet", "", hidden=True),
        _power_property_lib(
            "Description",
            "Power symbol synthesized by kimcp sch_add_power",
            hidden=True,
        ),
        graphic_unit,
        pin_unit,
    )


def _build_power_instance(
    *,
    net_name: str,
    reference: str,
    at_x: float,
    at_y: float,
    angle: float,
    instance_uuid: str,
    pin_uuid: str,
    project_name: str,
    top_uuid: str,
) -> SList:
    """Assemble the top-level ``(symbol ...)`` for a power-port instance.

    Differs from M14's generic symbol instance in three ways:

    * ``in_bom`` is **no** — power ports never appear in BOMs.
    * The Reference property is **hidden** — power symbols show the net
      name (Value), not the reference designator, on the canvas.
    * Single pin hard-coded to number ``"1"`` matching the synthetic
      lib_symbol. Real power lib entries are always single-pin too.
    """
    # Instance-level property nodes also need the 3-atom at-form — same
    # strict-parser rule as the lib-symbol properties above. ``at_node``
    # would elide the zero angle here and KiCAD 10 would reject the
    # schematic at load time.
    ref_prop = slist(
        atom("property"),
        atom("Reference", quoted=True),
        atom(reference, quoted=True),
        at_node_explicit(at_x, at_y + _VALUE_OFFSET_MM + 1.27, 0.0),
        effects_node(hidden=True),
    )
    value_prop = slist(
        atom("property"),
        atom("Value", quoted=True),
        atom(net_name, quoted=True),
        at_node_explicit(at_x, at_y + _VALUE_OFFSET_MM, 0.0),
        effects_node(),
    )
    footprint_prop = slist(
        atom("property"),
        atom("Footprint", quoted=True),
        atom("", quoted=True),
        at_node_explicit(at_x, at_y, 0.0),
        effects_node(hidden=True),
    )
    datasheet_prop = slist(
        atom("property"),
        atom("Datasheet", quoted=True),
        atom("", quoted=True),
        at_node_explicit(at_x, at_y, 0.0),
        effects_node(hidden=True),
    )

    instances_node = slist(
        atom("instances"),
        slist(
            atom("project"),
            atom(project_name, quoted=True),
            slist(
                atom("path"),
                atom(f"/{top_uuid}", quoted=True),
                slist(atom("reference"), atom(reference, quoted=True)),
                int_node("unit", 1),
            ),
        ),
    )

    items: list[SAtom | SList] = [
        atom("symbol"),
        slist(atom("lib_id"), atom(f"power:{net_name}", quoted=True)),
        # Schematic-instance symbol positions always use the 3-atom
        # ``(at X Y angle)`` form in KiCAD 10 — elision with angle=0
        # breaks the load parser.
        at_node_explicit(at_x, at_y, angle),
        int_node("unit", 1),
        flag_node("exclude_from_sim", False),
        flag_node("in_bom", False),  # power != BOM
        flag_node("on_board", True),
        flag_node("dnp", False),
        uuid_node(instance_uuid),
        ref_prop,
        value_prop,
        footprint_prop,
        datasheet_prop,
        slist(
            atom("pin"),
            atom("1", quoted=True),
            uuid_node(pin_uuid),
        ),
        instances_node,
    ]
    return SList(items=items)


def _find_power_instance_by_uuid(root: SList, instance_uuid: str) -> SList | None:
    """Return the ``(symbol ...)`` matching ``instance_uuid``, or None.

    Used by tests to walk the tree back to the freshly-placed instance.
    Matches the head ``symbol`` only — lib_symbols live under a nested
    ``(lib_symbols (symbol ...))`` so we filter by root-level siblings.
    """
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        uuid_node_child = child.find("uuid")
        if uuid_node_child is None or len(uuid_node_child.items) < 2:
            continue
        payload = uuid_node_child.items[1]
        if isinstance(payload, SAtom) and payload.text == instance_uuid:
            return child
    return None


__all__ = [
    "SchAddPowerInput",
    "SchAddPowerOutput",
    "SchAddPowerTool",
    "_build_power_instance",
    "_build_power_lib_symbol",
    "_find_power_instance_by_uuid",
]

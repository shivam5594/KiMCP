"""Shared s-expression construction helpers for schematic-mutation tools.

Every schematic-creation tool (M14 ``sch_add_symbol``, M15 ``sch_add_wire``,
M16 ``sch_add_junction``, M17 ``sch_add_label``, ...) synthesizes KiCAD
s-expression nodes. The atomic constructors live here so the format
gotchas are encoded once:

* ``(at X Y [angle])`` omits the angle atom when it's zero — what KiCAD
  writes, and what round-trip validation expects.
* ``(hide yes)`` is a child list, not a bare ``hide`` atom. Writing the
  latter produces a file that parses but renders oddly in the GUI.
* Integer-valued floats (``39.0``) serialize as integer strings (``39``).
  KiCAD round-trips that without a diff.
* Quoted vs unquoted atoms matter: reference designators, project names,
  UUIDs and property values are quoted; keywords (``yes``/``no``), integer
  flags, and coordinate numerals are unquoted.

Public (no-underscore) names so each mutator tool can import these without
crossing into each other's private surface. The module name itself keeps a
leading underscore to mark it package-private — external callers should go
through the tool registry, not import builder primitives directly.
"""

from __future__ import annotations

import math
from pathlib import Path

from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList


def load_sexpr_doc(parse_cache: ParseCache | None, path: Path) -> SexprDocument:
    """Load any KiCAD s-expression document, reusing the parse cache when set.

    Works for ``.kicad_sch``, ``.kicad_sym``, ``.kicad_pcb``, ``.kicad_mod``
    — anything ``SexprDocument`` parses. The cache is keyed on
    ``(path, mtime_ns, size)``. After a successful mutating
    ``doc.save()`` the on-disk mtime/size change, so the next
    ``cache.get(path)`` naturally misses and re-parses fresh — callers do
    NOT need to explicitly invalidate after a save. When ``parse_cache``
    is ``None`` (e.g. a tool instantiated outside a Server, in a unit
    test), fall back to a direct from_path parse so behaviour is
    unchanged. Raises ``SexprParseError`` on parse failure in either
    path — error model preserved.
    """
    if parse_cache is None:
        return SexprDocument.from_path(path)
    return parse_cache.get(path)

# KiCAD's default property / label font size in mm. Matches eeschema.
DEFAULT_FONT_SIZE_MM = 1.27


def snap_coord(value: float, grid_mm: float | None) -> float:
    """Snap a schematic coordinate to the nearest multiple of ``grid_mm``.

    ``grid_mm`` is the canonical schematic grid (default 2.54 mm = 100
    mil per the ``safety.grid_snap_mm`` config knob). Passing ``None``
    returns the input verbatim — callers opting out of snap get exact
    pass-through so they can land sub-grid coordinates if they really
    must (rare; eeschema snaps on display anyway).

    Rounds half to even via ``round()`` to avoid systematic drift on
    fractions that sit exactly between grid ticks. Handles negative
    coordinates naturally (``round()`` rounds toward zero on ``-x.5``
    cases, same as positive).

    We don't re-snap already-on-grid values: this function is meant to be
    called unconditionally at tool entry, and ``snap_coord(5.08, 2.54)``
    must return exactly ``5.08`` (not ``5.0800000000001``) so idempotent
    callers produce stable on-disk bytes. Floating-point hazard is
    explicit: we compute ``round(value / grid_mm) * grid_mm`` and
    accept the inherent FP rounding of that composition; tests pin a
    few representative values.
    """
    if grid_mm is None:
        return value
    if math.isnan(value) or math.isinf(value):
        # Degenerate input — caller has bigger problems. Return verbatim
        # so the validation error surfaces at a more specific layer
        # (pydantic range check, KiCAD ERC, etc.) rather than masking
        # inside the snap.
        return value
    return round(value / grid_mm) * grid_mm


def snap_moved(original: float, snapped: float, epsilon_mm: float = 1e-6) -> bool:
    """Return True iff the snap actually moved the coordinate beyond
    floating-point noise. Callers use this to decide whether to emit a
    warning on ``meta.warnings`` — a no-op snap shouldn't generate
    noise.

    ``epsilon_mm`` is below any grid KiCAD supports (smallest is 0.00001
    mm = 10 nm in some project settings) so we won't false-positive on
    coords that were already on grid modulo FP error.
    """
    return abs(original - snapped) > epsilon_mm


def apply_grid_snap(
    coords: dict[str, float], grid_mm: float | None
) -> tuple[dict[str, float], str | None]:
    """Snap every value in ``coords`` to ``grid_mm`` and return both the
    snapped values and a single user-actionable warning describing the
    moves (or ``None`` if no coordinate shifted).

    ``coords`` is a ``{field_name: value}`` map so the warning can cite
    which fields moved — critical for debugging when a tool silently
    relocates ``end_x`` but not ``start_x``. The order of keys in the
    returned dict matches the input order (dict insertion order since
    Python 3.7), so tools that need positional consistency get stable
    iteration.

    Design notes:
    * ``grid_mm is None`` is the opt-out — coords pass through verbatim
      and no warning is produced.
    * A snap whose delta is smaller than ``snap_moved``'s epsilon is
      treated as a no-op. This matters when callers already pass
      on-grid values: we'd rather emit ``{}``-warnings than trip the
      warning path on bit-level noise from ``round() * grid_mm``.
    * Warning text names the config knob so operators can audit / opt
      out without reading source.
    """
    if grid_mm is None:
        return dict(coords), None

    snapped = {k: snap_coord(v, grid_mm) for k, v in coords.items()}
    moved = [(k, coords[k], snapped[k]) for k in coords if snap_moved(coords[k], snapped[k])]
    if not moved:
        return snapped, None

    parts = [f"{k} {orig:g}→{sn:g}" for k, orig, sn in moved]
    warning = (
        f"Grid-snapped {len(moved)} coordinate(s) to the {grid_mm} mm "
        f"safety.grid_snap_mm grid: {', '.join(parts)}. Off-grid inputs "
        f"cause KiCAD endpoint_off_grid / label_dangling ERC warnings; "
        f"set safety.grid_snap_mm = null to opt out."
    )
    return snapped, warning


def fmt_mm(value: float) -> str:
    """Format a millimetre scalar for a KiCAD atom.

    KiCAD writes whole numbers without a decimal point (``39``), other
    numbers in plain decimal (``39.37``). Python's ``str(float)`` does
    that naturally except for the ``"1.0" vs "1"`` split — normalize so
    integer-valued floats round-trip as integers.
    """
    if value == int(value):
        return str(int(value))
    return repr(value)


def yesno(flag: bool) -> str:
    return "yes" if flag else "no"


def atom(text: str, *, quoted: bool = False) -> SAtom:
    """Shorthand for constructing a synthesized atom."""
    return SAtom(text=text, quoted=quoted)


def slist(*items: SAtom | SList) -> SList:
    """Shorthand for constructing a synthesized SList."""
    return SList(items=list(items))


def flag_node(head: str, value: bool) -> SList:
    """``(head yes|no)`` as an unquoted boolean flag."""
    return slist(atom(head), atom(yesno(value)))


def int_node(head: str, value: int) -> SList:
    """``(head N)`` as an unquoted integer."""
    return slist(atom(head), atom(str(value)))


def at_node(x: float, y: float, angle: float = 0.0) -> SList:
    """``(at X Y [angle])`` — omits the angle atom when it is zero.

    **Safe only for contexts where KiCAD itself emits the 2-atom form**:
    ``(junction (at X Y))``, ``(no_connect (at X Y))`` and
    ``(sheet (at X Y))``. For property nodes, symbol instances, labels,
    and anything under a ``lib_symbols`` block, use
    :func:`at_node_explicit` instead — KiCAD 10's strict parser rejects
    the 2-atom form there with a terse ``need a number for 'text
    angle'`` error at load time, even when the angle value is zero.

    If the round-trip validation ever trips on an ``(at ...)`` node,
    check this first — spurious or missing angle atoms are the usual
    culprit.
    """
    items: list[SAtom | SList] = [
        atom("at"),
        atom(fmt_mm(x)),
        atom(fmt_mm(y)),
    ]
    if angle != 0:
        items.append(atom(fmt_mm(angle)))
    return SList(items=items)


def at_node_explicit(x: float, y: float, angle: float = 0.0) -> SList:
    """``(at X Y angle)`` — always emits the 3-atom form, even at zero.

    The mirror of :func:`at_node`, for contexts where KiCAD 10's parser
    is strict about the angle atom being present:

    * property nodes inside ``(symbol ...)``, ``(sheet ...)`` and any
      library-symbol block (including symbols nested in the schematic's
      top-level ``(lib_symbols ...)`` section);
    * schematic-instance symbol positions (top-level ``(symbol ...)``
      inside a ``.kicad_sch``);
    * labels — local, global, and hierarchical (``(label ...)``,
      ``(global_label ...)``, ``(hierarchical_label ...)``).

    KiCAD's own emission uses the 3-atom form in these contexts even
    when the angle is zero, so round-trip validation also needs it.
    Omitting the angle atom is what causes the ``need a number for
    'text angle'`` load failure (was line 2226 in the MK-II Controller
    Board issue).
    """
    return SList(
        items=[
            atom("at"),
            atom(fmt_mm(x)),
            atom(fmt_mm(y)),
            atom(fmt_mm(angle)),
        ]
    )


def effects_node(
    *,
    hidden: bool = False,
    justify: tuple[str, ...] = (),
    font_size_mm: float = DEFAULT_FONT_SIZE_MM,
) -> SList:
    """``(effects (font (size S S)) [(justify ...)] [(hide yes)])``.

    KiCAD stores the hide flag as ``(hide yes)`` in the 9.x format — not
    a bare ``hide`` atom. The ``justify`` tuple, when non-empty, emits a
    ``(justify <items>)`` child (labels and hierarchical labels use this;
    plain component properties don't).
    """
    font = slist(
        atom("font"),
        slist(
            atom("size"),
            atom(fmt_mm(font_size_mm)),
            atom(fmt_mm(font_size_mm)),
        ),
    )
    items: list[SAtom | SList] = [atom("effects"), font]
    if justify:
        items.append(slist(atom("justify"), *(atom(j) for j in justify)))
    if hidden:
        items.append(flag_node("hide", True))
    return SList(items=items)


def uuid_node(value: str) -> SList:
    """``(uuid "<value>")`` — the canonical quoted UUID form."""
    return slist(atom("uuid"), atom(value, quoted=True))


def stroke_default_node() -> SList:
    """``(stroke (width 0) (type default))`` — the default wire/line stroke.

    KiCAD writes this block on every wire, every graphic line, every
    shape outline. Matching the canonical form keeps round-trip
    validation happy.
    """
    return slist(
        atom("stroke"),
        int_node("width", 0),
        slist(atom("type"), atom("default")),
    )


def find_scalar_string(parent: SList, head: str) -> str | None:
    """Return the quoted-string payload of ``(head "value")`` under parent.

    Returns ``None`` if the node is missing or malformed. Used for the
    top-level ``(uuid "...")`` lookup and similar scalar-string extracts
    across schematic-mutation tools.
    """
    node = parent.find(head)
    if node is None or len(node.items) < 2:
        return None
    payload = node.items[1]
    if not isinstance(payload, SAtom):
        return None
    return payload.text


__all__ = [
    "DEFAULT_FONT_SIZE_MM",
    "apply_grid_snap",
    "at_node",
    "at_node_explicit",
    "atom",
    "effects_node",
    "find_scalar_string",
    "flag_node",
    "fmt_mm",
    "int_node",
    "load_sexpr_doc",
    "slist",
    "snap_coord",
    "snap_moved",
    "stroke_default_node",
    "uuid_node",
    "yesno",
]

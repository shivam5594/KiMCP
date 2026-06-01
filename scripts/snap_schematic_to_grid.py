"""One-shot migration: snap every off-grid coordinate in a schematic
to the nearest multiple of ``safety.grid_snap_mm`` (default 2.54 mm).

Why this exists
---------------

Fix B added a grid-snap guardrail on every ``sch_add_*`` tool so future
placements land on KiCAD's 100-mil eeschema grid automatically. This
script is the companion for the installed base — schematics whose
coordinates were written off-grid by earlier tool versions. Those
off-grid coordinates surface as ``endpoint_off_grid`` and
``label_dangling`` ERC warnings and prevent clean wire-pin junctions.

What gets snapped
-----------------

For every occurrence in a ``.kicad_sch`` file:

* ``(at X Y [angle])`` anchors on symbol instances, labels,
  junctions, no_connect markers, and sheet positions.
* ``(xy X Y)`` points inside ``(pts ...)`` — used by wires,
  polylines, sheet outlines, etc.
* ``(size W H)`` on sheets — both dimensions snap so outlines land on
  grid on all four edges.

The angle atom on 3-atom ``(at X Y angle)`` forms is left alone —
angles are rotational (degrees), not grid-snappable coordinates.

What does NOT get snapped
-------------------------

* Values inside ``lib_symbols`` — lib symbol internals have their own
  coordinate system (pin positions relative to symbol origin). Snapping
  those would corrupt the library graphic.
* The top-level ``(paper "A4")`` or any non-coordinate atom.
* Files whose top-head is not ``kicad_sch`` — library files
  (``.kicad_sym``) are a separate coordinate domain and out of scope.

Usage
-----

::

    # Preview (no writes)
    .venv/bin/python scripts/snap_schematic_to_grid.py --dry-run \\
        path/to/board.kicad_sch

    # Apply (writes .bak next to each touched file)
    .venv/bin/python scripts/snap_schematic_to_grid.py \\
        path/to/board.kicad_sch

    # Custom grid (e.g. 1.27 mm = 50 mil)
    .venv/bin/python scripts/snap_schematic_to_grid.py --grid 1.27 \\
        path/to/project/
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Add src/ to path so this runs without installing.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from kimcp.sexpr.document import SexprDocument  # noqa: E402
from kimcp.sexpr.errors import SexprParseError  # noqa: E402
from kimcp.sexpr.nodes import SAtom, SList  # noqa: E402
from kimcp.tools.builtin._sexpr_build import fmt_mm, snap_coord, snap_moved  # noqa: E402

_WALK_SUFFIXES = {".kicad_sch"}


def _snap_atom_pair(
    node: SList,
    idx_x: int,
    idx_y: int,
    grid_mm: float,
    counters: dict[str, int],
) -> None:
    """Snap two numeric atoms in a list node (e.g. items[1] and items[2]
    of an ``(at ...)``). Updates both atoms in place when either moves."""
    a_x = node.items[idx_x]
    a_y = node.items[idx_y]
    if not isinstance(a_x, SAtom) or not isinstance(a_y, SAtom):
        counters["skipped_non_numeric"] += 1
        return
    try:
        x_val = float(a_x.text)
        y_val = float(a_y.text)
    except ValueError:
        counters["skipped_non_numeric"] += 1
        return

    x_snap = snap_coord(x_val, grid_mm)
    y_snap = snap_coord(y_val, grid_mm)
    if not (snap_moved(x_val, x_snap) or snap_moved(y_val, y_snap)):
        counters["already_on_grid"] += 1
        return

    # Replace atoms with freshly-minted SAtoms using the same ``fmt_mm``
    # formatter the tool writers use — preserves integer-vs-float
    # rendering conventions (``5`` stays ``5``, ``2.54`` stays ``2.54``).
    node.items[idx_x] = SAtom(text=fmt_mm(x_snap), quoted=False)
    node.items[idx_y] = SAtom(text=fmt_mm(y_snap), quoted=False)
    node._dirty = True
    counters["snapped"] += 1


def _walk_and_snap(
    node: SList,
    grid_mm: float,
    counters: dict[str, int],
    *,
    inside_lib_symbols: bool = False,
) -> None:
    """Recursively visit every node and snap coordinates.

    ``inside_lib_symbols`` gates us out of the lib symbol coordinate
    domain — pin positions relative to a symbol origin, which are
    already on whatever grid the library author chose and are not
    user-editable in the schematic editor. Snapping those would
    corrupt the embedded library graphics.
    """
    if node.head == "lib_symbols":
        inside_lib_symbols = True

    # Patch this node itself if it's a coordinate-bearing form.
    # ``(at X Y [angle])``, ``(xy X Y)``, and ``(size W H)`` all carry
    # snappable numeric pairs at items[1] and items[2]. Angles on 3-atom
    # ``(at ...)`` forms are rotational (degrees) and left alone.
    if not inside_lib_symbols and node.head in ("at", "xy", "size") and len(node.items) >= 3:
        _snap_atom_pair(node, 1, 2, grid_mm, counters)

    for child in node.items:
        if isinstance(child, SList):
            _walk_and_snap(child, grid_mm, counters, inside_lib_symbols=inside_lib_symbols)


def snap_schematic(
    sch_path: Path, *, grid_mm: float, dry_run: bool = False
) -> dict[str, int]:
    counters = {
        "snapped": 0,
        "already_on_grid": 0,
        "skipped_non_numeric": 0,
    }

    try:
        doc = SexprDocument.from_path(sch_path)
    except SexprParseError as exc:
        print(f"{sch_path}: parse failed ({exc}) — skipping.")
        return counters

    if doc.top_head != "kicad_sch":
        print(
            f"{sch_path}: top-head is {doc.top_head!r}, expected 'kicad_sch' "
            "— skipping."
        )
        return counters

    _walk_and_snap(doc.root, grid_mm, counters)

    if counters["snapped"] == 0:
        print(
            f"{sch_path}: already on the {grid_mm} mm grid "
            f"(checked {counters['already_on_grid']} coord pair(s))."
        )
        return counters

    if dry_run:
        print(
            f"{sch_path}: [dry-run] would snap {counters['snapped']} "
            f"coord pair(s) to the {grid_mm} mm grid "
            f"(already-on-grid: {counters['already_on_grid']})."
        )
        return counters

    backup = sch_path.with_suffix(sch_path.suffix + ".bak")
    shutil.copy2(sch_path, backup)
    print(f"  backup: {backup}")

    doc.save(sch_path)
    print(
        f"{sch_path}: snapped {counters['snapped']} coord pair(s); "
        f"left {counters['already_on_grid']} already-on-grid untouched."
    )
    return counters


def _discover_targets(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if root.is_dir():
        return sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix in _WALK_SUFFIXES
            and ".bak" not in p.suffixes
        )
    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Snap off-grid coordinates in a .kicad_sch (or a directory of "
            "them) to the nearest multiple of --grid millimetres. "
            "Eliminates endpoint_off_grid / label_dangling ERC warnings "
            "from schematics written by earlier KiMCP versions."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="A .kicad_sch file or a directory (walked recursively).",
    )
    parser.add_argument(
        "--grid",
        type=float,
        default=2.54,
        help="Grid size in millimetres. Default: 2.54 (= 100 mil, KiCAD's native).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be snapped without writing anything.",
    )
    args = parser.parse_args()

    if args.grid <= 0:
        print(f"--grid must be positive, got {args.grid!r}", file=sys.stderr)
        return 2

    target = args.path.expanduser().resolve()
    files = _discover_targets(target)
    if not files:
        print(f"no .kicad_sch files found at {target}", file=sys.stderr)
        return 2

    totals = {"snapped": 0, "already_on_grid": 0, "skipped_non_numeric": 0}
    for f in files:
        c = snap_schematic(f, grid_mm=args.grid, dry_run=args.dry_run)
        for k in totals:
            totals[k] += c[k]

    if len(files) > 1:
        mode = "[dry-run] " if args.dry_run else ""
        print(
            f"\n{mode}totals across {len(files)} file(s): "
            f"snapped={totals['snapped']} "
            f"already-on-grid={totals['already_on_grid']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

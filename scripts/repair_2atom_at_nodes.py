"""One-shot repair: upgrade 2-atom ``(at X Y)`` to 3-atom ``(at X Y 0)``
in the contexts where KiCAD 10's strict parser requires it.

Why this exists
---------------

Up through the fix landed 2026-04-16, the KiMCP schematic-builder tools
(``sch_add_power``, ``sch_add_symbol``, ``sch_add_label``, plus the
``lib_symbols``-embedding path in ``sch_add_power``) and the library
builder (``lib_add_symbol``) emitted the 2-atom ``(at X Y)`` form via
the shared ``at_node`` helper, which elides the angle atom when it's
zero. That's the correct emission for ``junction`` / ``no_connect`` /
``sheet`` positions, but KiCAD 10's strict parser rejects the 2-atom
form for:

* ``(property ...)`` at-nodes inside ``(symbol ...)``, ``(sheet ...)``,
  or any lib-symbol block
* schematic-instance ``(symbol ...)`` positions
* ``(label ...)`` / ``(global_label ...)`` / ``(hierarchical_label ...)``
  positions
* lib-symbol ``(pin ...)`` positions

The symptom is the load-time error::

    need a number for 'text angle' in '<schematic>.kicad_sch',
    line NNNN, offset NN

This script walks a ``.kicad_sch`` or ``.kicad_sym`` file (or a whole
directory tree), finds every 2-atom at-node, and rewrites those that
sit in a context that requires the explicit angle — leaving the
legitimately-2-atom ones (junction/no_connect/sheet outer anchor)
alone. It uses KiMCP's own SEXPR machinery so round-trip validation
catches any mis-edit before it touches disk.

Usage
-----

::

    # Single file
    .venv/bin/python scripts/repair_2atom_at_nodes.py path/to/board.kicad_sch

    # Whole project directory (walks .kicad_sch + .kicad_sym)
    .venv/bin/python scripts/repair_2atom_at_nodes.py path/to/project/

    # Preview without mutating anything
    .venv/bin/python scripts/repair_2atom_at_nodes.py --dry-run path/to/project/

A ``.bak`` copy of each rewritten file is written alongside the
original. ``--dry-run`` skips the backup and the write, reporting
only the counters.
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
from kimcp.sexpr.nodes import SAtom, SList  # noqa: E402

# Parent-head names where the 2-atom form is correct — do NOT patch.
_TWO_ATOM_CONTEXTS = {"junction", "no_connect", "sheet"}

# File top-heads we recognize. kicad_sch covers schematics;
# kicad_symbol_lib covers .kicad_sym library files.
_ACCEPTED_TOP_HEADS = {"kicad_sch", "kicad_symbol_lib"}

# File suffixes we walk when given a directory.
_WALK_SUFFIXES = {".kicad_sch", ".kicad_sym"}


def _is_at_two_atom(node: SList) -> bool:
    """Return True iff this is ``(at X Y)`` — head + 2 coord atoms, no angle."""
    if node.head != "at":
        return False
    if len(node.items) != 3:
        return False
    return all(isinstance(item, SAtom) for item in node.items[1:])


def _patch_at_to_three_atom(at_node: SList) -> None:
    """In-place: turn ``(at X Y)`` into ``(at X Y 0)``.

    Uses ``set_items`` so the ``_dirty`` flag is flipped and the writer
    re-serializes from the node tree rather than byte-splicing the
    original (which wouldn't include the appended angle atom).
    """
    at_node.set_items([*at_node.items, SAtom(text="0", quoted=False)])


def _walk_and_patch(node: SList, counters: dict[str, int]) -> None:
    """Recurse into the tree; patch 2-atom at-nodes in property / symbol /
    label / pin contexts; leave junction/sheet/no_connect alone.

    ``counters`` tallies ``patched`` vs ``preserved`` so the script can
    report what it did.
    """
    # Whether this node's direct at-child needs the 3-atom form.
    is_property = node.head == "property"
    # Schematic-instance and lib_symbols both use the ``symbol`` head;
    # both require explicit-angle positions. Top-level (kicad_sch (symbol
    # ...)) and nested (lib_symbols (symbol ...)) are indistinguishable
    # by head alone, but the rule is the same for both so we don't need
    # to disambiguate.
    is_symbol = node.head == "symbol"
    is_label = node.head in ("label", "global_label", "hierarchical_label")
    # Lib-symbol pins also need the explicit angle form — KiCAD 10 reads
    # the pin's (at ...) with the same strict parser.
    is_pin = node.head == "pin"

    patched_here = False
    for child in node.items:
        if not isinstance(child, SList):
            continue

        if _is_at_two_atom(child):
            if node.head in _TWO_ATOM_CONTEXTS:
                counters["preserved"] += 1
            elif is_property or is_symbol or is_label or is_pin:
                _patch_at_to_three_atom(child)
                counters["patched"] += 1
                patched_here = True
            else:
                # Unknown context — be conservative and leave the node
                # as-is. If this fires on a real case we'll see it in
                # the counter and can decide case by case.
                counters["skipped_unknown"] += 1
        else:
            _walk_and_patch(child, counters)

    if patched_here:
        # The parent of a patched at-node also needs to serialize
        # fresh — otherwise the writer would splice the old bytes of
        # this node (which still contain the 2-atom form) verbatim.
        node._dirty = True


def repair(path: Path, *, dry_run: bool = False) -> dict[str, int]:
    doc = SexprDocument.from_path(path)
    if doc.top_head not in _ACCEPTED_TOP_HEADS:
        print(
            f"{path}: top-head is {doc.top_head!r}, expected one of "
            f"{sorted(_ACCEPTED_TOP_HEADS)} — skipping."
        )
        return {"patched": 0, "preserved": 0, "skipped_unknown": 0}

    counters = {"patched": 0, "preserved": 0, "skipped_unknown": 0}
    _walk_and_patch(doc.root, counters)

    if counters["patched"] == 0:
        print(
            f"{path}: no 2-atom at-nodes in property/symbol/label/pin context "
            f"— nothing to do."
        )
        return counters

    if dry_run:
        print(
            f"{path}: [dry-run] would patch {counters['patched']} at-nodes; "
            f"{counters['preserved']} junction/sheet/no_connect cases would "
            f"be preserved; {counters['skipped_unknown']} unknown contexts "
            f"would be skipped."
        )
        return counters

    # Back up the original before writing.
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    print(f"  backup: {backup}")

    doc.save(path)
    print(
        f"{path}: patched {counters['patched']} at-nodes; "
        f"preserved {counters['preserved']} junction/sheet/no_connect cases; "
        f"skipped {counters['skipped_unknown']} unknown contexts."
    )
    return counters


def _discover_targets(root: Path) -> list[Path]:
    """Expand a path argument into the list of files to repair.

    A file is returned as a single-element list. A directory is walked
    recursively for any file whose suffix is in ``_WALK_SUFFIXES``.
    Files with backup suffix ``.bak`` are skipped so the script is
    re-runnable without touching prior backups.
    """
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
            "Repair 2-atom (at X Y) nodes in KiCAD 10 schematic / symbol-"
            "library files that KiCAD refuses to load."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help=(
            "A .kicad_sch / .kicad_sym file, OR a directory — in which case "
            "every matching file under it is walked recursively."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be patched without writing anything.",
    )
    args = parser.parse_args()

    target = args.path.expanduser().resolve()
    files = _discover_targets(target)
    if not files:
        print(
            f"no .kicad_sch / .kicad_sym files found at {target}",
            file=sys.stderr,
        )
        return 2

    totals = {"patched": 0, "preserved": 0, "skipped_unknown": 0}
    for f in files:
        c = repair(f, dry_run=args.dry_run)
        for k in totals:
            totals[k] += c[k]

    if len(files) > 1:
        mode = "[dry-run] " if args.dry_run else ""
        print(
            f"\n{mode}totals across {len(files)} file(s): "
            f"patched={totals['patched']} preserved={totals['preserved']} "
            f"skipped_unknown={totals['skipped_unknown']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

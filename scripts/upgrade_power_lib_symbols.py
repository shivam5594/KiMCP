"""One-shot migration: upgrade synthetic ``power:<net>`` lib_symbols to
the canonical entries from KiCAD's bundled ``power.kicad_sym``.

Why this exists
---------------

Up through the fix landed 2026-04-17, ``sch_add_power`` auto-synthesized
a minimal ``power:<net>`` lib_symbol every time it placed a port. The
synthesized stand-in was functionally complete (``(power)`` marker,
single ``power_in`` pin, ``(in_bom no)``) but diverged visually and
structurally from KiCAD's canonical bundled entry — KiCAD's library
browser renders the triangle/circle graphic, and ERC emits
``lib_symbol_mismatch`` warnings for every synthesized entry whose net
name matches a canonical rail (``GND``, ``VCC``, ``+3V3``, …).

Fix A in ``sch_add_power`` now prefers the canonical on every NEW
placement. This script handles the installed base — schematics whose
``lib_symbols`` block already contains synthesized entries.

What gets upgraded
------------------

For every top-level ``(symbol "power:<NET>" ...)`` inside the
``lib_symbols`` block whose ``<NET>`` exists in KiCAD's installed
``power.kicad_sym``, the entry is replaced with a deep-clone of the
canonical definition, lib-qualified to ``power:<NET>``. Entries for
custom rails not in the canonical library (``+VIN_IN``, ``+V3P3``,
etc. on some projects) are **left alone** — there's no upgrade
target for them.

The instance blocks themselves — the ``(symbol (lib_id "power:NET") ...)``
scattered through the schematic — are NOT touched. They reference the
lib_symbol by id; swapping out the lib_symbol definition is all it
takes for KiCAD to render the canonical graphic on reload.

Usage
-----

::

    # Single schematic
    .venv/bin/python scripts/upgrade_power_lib_symbols.py path/to/board.kicad_sch

    # Whole project directory
    .venv/bin/python scripts/upgrade_power_lib_symbols.py path/to/project/

    # Preview without mutating
    .venv/bin/python scripts/upgrade_power_lib_symbols.py --dry-run path/to/project/

A ``.bak`` copy is written alongside each rewritten file. ``--dry-run``
skips the backup and the write, reporting only what it would do.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Add src/ to path so this runs without installing.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from kimcp.cli.paths import resolve_system_symbol_lib  # noqa: E402
from kimcp.sexpr.document import SexprDocument  # noqa: E402
from kimcp.sexpr.errors import SexprParseError  # noqa: E402
from kimcp.sexpr.nodes import SAtom, SList  # noqa: E402
from kimcp.tools.builtin.sch_embed_lib_symbol import (  # noqa: E402
    _clone_and_qualify,
    _find_symbol_in_lib,
)

_WALK_SUFFIXES = {".kicad_sch"}


def _iter_power_entries(lib_symbols: SList) -> list[tuple[int, SList, str]]:
    """Return ``(index, symbol_node, net_name)`` tuples for every
    ``(symbol "power:<NET>" ...)`` inside ``lib_symbols``.

    ``index`` is the position inside ``lib_symbols.items`` so we can do
    an in-place replacement. ``net_name`` is the bare net (``"GND"``,
    not ``"power:GND"``) — the name the canonical lookup needs.
    """
    out: list[tuple[int, SList, str]] = []
    for i, child in enumerate(lib_symbols.items):
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) < 2:
            continue
        name_atom = child.items[1]
        if not isinstance(name_atom, SAtom):
            continue
        if not name_atom.text.startswith("power:"):
            continue
        net = name_atom.text[len("power:"):]
        if not net:
            continue
        out.append((i, child, net))
    return out


def upgrade(
    sch_path: Path,
    canonical_lib_root: SList,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    counters = {"upgraded": 0, "custom_rail_preserved": 0, "already_canonical_signature": 0}

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

    lib_symbols = doc.root.find("lib_symbols")
    if lib_symbols is None:
        print(f"{sch_path}: no (lib_symbols ...) block — nothing to do.")
        return counters

    entries = _iter_power_entries(lib_symbols)
    if not entries:
        print(f"{sch_path}: no power:<net> entries in lib_symbols — nothing to do.")
        return counters

    for idx, old_node, net in entries:
        canonical = _find_symbol_in_lib(canonical_lib_root, net)
        if canonical is None:
            # Custom rail like +VIN_IN — not in KiCAD's bundled library.
            # Leave the synthetic entry alone; there's no canonical to
            # upgrade to.
            counters["custom_rail_preserved"] += 1
            continue

        # Heuristic: if the existing entry's shape already matches the
        # canonical (same number of direct children with same heads),
        # skip — prevents double-upgrades if the script is re-run on a
        # file where a previous run already swapped everything out.
        # Not perfect (counts can coincide), but a cheap guard against
        # re-processing already-upgraded files.
        if _looks_like_canonical_signature(old_node, canonical):
            counters["already_canonical_signature"] += 1
            continue

        replacement = _clone_and_qualify(canonical, f"power:{net}")
        lib_symbols.items[idx] = replacement
        lib_symbols._dirty = True
        counters["upgraded"] += 1

    if counters["upgraded"] == 0:
        print(
            f"{sch_path}: no upgradeable entries "
            f"(custom-rail-preserved={counters['custom_rail_preserved']}, "
            f"already-canonical={counters['already_canonical_signature']})."
        )
        return counters

    if dry_run:
        print(
            f"{sch_path}: [dry-run] would upgrade {counters['upgraded']} "
            f"power:<net> entries; preserve {counters['custom_rail_preserved']} "
            f"custom rail(s); skip {counters['already_canonical_signature']} "
            f"already-canonical."
        )
        return counters

    # Back up the original.
    backup = sch_path.with_suffix(sch_path.suffix + ".bak")
    shutil.copy2(sch_path, backup)
    print(f"  backup: {backup}")

    doc.save(sch_path)
    print(
        f"{sch_path}: upgraded {counters['upgraded']}; "
        f"preserved {counters['custom_rail_preserved']} custom rail(s); "
        f"skipped {counters['already_canonical_signature']} already-canonical."
    )
    return counters


def _looks_like_canonical_signature(existing: SList, canonical: SList) -> bool:
    """Cheap re-entrancy guard: if the direct-child head sequence matches
    the canonical's, assume the entry has already been upgraded. This is
    a structural heuristic, not a full equality check — we accept a
    small false-positive rate (skipping entries that happen to share
    head shapes but differ in content) because an accidental skip is
    safer than a double-upgrade that churns bytes unnecessarily."""

    def head_shape(node: SList) -> tuple[str, ...]:
        out = []
        for child in node.items:
            if isinstance(child, SList):
                out.append(child.head)
            elif isinstance(child, SAtom):
                out.append("#atom")
        return tuple(out)

    return head_shape(existing) == head_shape(canonical)


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
            "Upgrade synthesized power:<net> lib_symbol entries in a "
            "schematic to KiCAD's canonical bundled definitions, eliminating "
            "lib_symbol_mismatch ERC warnings."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="A .kicad_sch file or a directory (walked recursively).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be upgraded without writing anything.",
    )
    args = parser.parse_args()

    bundled = resolve_system_symbol_lib("power")
    if bundled is None:
        print(
            "KiCAD's bundled power.kicad_sym was not found on this host — "
            "cannot upgrade. Install KiCAD first.",
            file=sys.stderr,
        )
        return 2
    print(f"canonical source: {bundled}")

    try:
        lib_doc = SexprDocument.from_path(bundled)
    except SexprParseError as exc:
        print(f"failed to parse {bundled}: {exc}", file=sys.stderr)
        return 2
    if lib_doc.top_head != "kicad_symbol_lib":
        print(
            f"{bundled} has unexpected top-head {lib_doc.top_head!r}; aborting.",
            file=sys.stderr,
        )
        return 2

    target = args.path.expanduser().resolve()
    files = _discover_targets(target)
    if not files:
        print(f"no .kicad_sch files found at {target}", file=sys.stderr)
        return 2

    totals = {"upgraded": 0, "custom_rail_preserved": 0, "already_canonical_signature": 0}
    for f in files:
        c = upgrade(f, lib_doc.root, dry_run=args.dry_run)
        for k in totals:
            totals[k] += c[k]

    if len(files) > 1:
        mode = "[dry-run] " if args.dry_run else ""
        print(
            f"\n{mode}totals across {len(files)} file(s): "
            f"upgraded={totals['upgraded']} "
            f"custom-rail-preserved={totals['custom_rail_preserved']} "
            f"already-canonical={totals['already_canonical_signature']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

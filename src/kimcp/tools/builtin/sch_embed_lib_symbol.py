"""sch_embed_lib_symbol — copy a symbol definition into a schematic (M19).

Before ``sch_add_symbol`` (M14) can place a component instance, the
component's library definition must already live inside the schematic's
``lib_symbols`` block. KiCAD's GUI does this automatically when you pick
a symbol from the library browser. This tool is the MCP equivalent.

Flow
----

1. Parse the ``.kicad_sym`` library file.
2. Find the requested ``symbol_name`` inside it.
3. Clone the ``(symbol ...)`` subtree.
4. Rename the top-level atom from ``"R_Small"`` to
   ``"<lib_prefix>:R_Small"`` — the lib-qualified form KiCAD uses
   inside schematic files.
5. Insert into the schematic's ``lib_symbols`` block.

Sub-symbols (``R_Small_0_1``, ``R_Small_1_1``, …) keep their original
unqualified names — that's what KiCAD writes.

The ``lib_prefix`` defaults to the stem of the ``.kicad_sym`` file
path, matching KiCAD's library-table nickname → file mapping.

Idempotent: if the qualified ``lib_id`` already exists in the
schematic's ``lib_symbols``, the tool returns ``already_embedded``
without mutating.

Status enum
-----------

* **ok**                — lib symbol copied and written.
* **dry_run**           — caller passed ``dry_run=True``.
* **sch_not_found**     — schematic path missing / wrong suffix.
* **lib_not_found**     — library path missing / wrong suffix.
* **invalid_schema**    — schematic top_head isn't ``kicad_sch``.
* **invalid_lib**       — library top_head isn't ``kicad_symbol_lib``.
* **parse_failed**      — the SEXPR parser rejected one of the files.
* **symbol_not_found**  — ``symbol_name`` not in the library.
* **already_embedded**  — ``lib_id`` already present in ``lib_symbols``.
* **write_failed**      — snapshot or atomic save raised.

Backend: SEXPR, required. Same rationale as M14-M18.
"""

from __future__ import annotations

import copy
import logging
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
from kimcp.tools.builtin._sexpr_build import atom, load_sexpr_doc, slist
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol

log = logging.getLogger(__name__)


# -- input / output --------------------------------------------------------


class SchEmbedLibSymbolInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    lib_path: Path = Field(
        ...,
        description=(
            "Path to the .kicad_sym symbol library file. E.g. "
            "'/usr/share/kicad/symbols/Device.kicad_sym'."
        ),
    )
    symbol_name: str = Field(
        ...,
        description=(
            "Unqualified symbol name inside the library. E.g. 'R_Small', "
            "'C_Small', 'LM7805_TO220'. Must match a top-level (symbol "
            "\"...\") entry in the library file."
        ),
    )
    lib_prefix: str | None = Field(
        default=None,
        description=(
            "Library prefix for the qualified lib_id. Defaults to the stem "
            "of lib_path (e.g., 'Device.kicad_sym' → 'Device'). The "
            "final lib_id becomes '<prefix>:<symbol_name>'."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report what would be embedded without writing.",
    )


class SchEmbedLibSymbolOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "lib_not_found",
        "invalid_schema",
        "invalid_lib",
        "parse_failed",
        "symbol_not_found",
        "already_embedded",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None, description="Resolved absolute path to the .kicad_sch."
    )
    lib_id: str | None = Field(
        default=None,
        description="Qualified lib id (e.g. 'Device:R_Small').",
    )
    note: str | None = Field(
        default=None, description="Diagnostic string for non-ok statuses."
    )


# -- tool ------------------------------------------------------------------


class SchEmbedLibSymbolTool(Tool[SchEmbedLibSymbolInput, SchEmbedLibSymbolOutput]):
    """Copy a symbol definition from a .kicad_sym into a .kicad_sch."""

    name = "sch_embed_lib_symbol"
    version = "0.2.0"
    description = (
        "Embed a symbol library definition into a schematic's lib_symbols "
        "block so that sch_add_symbol can place instances. Reads from a "
        ".kicad_sym file, renames the top-level symbol to lib-qualified "
        "form, and inserts into the schematic. CALL ONCE PER UNIQUE "
        "(lib_id, sch_path) PAIR per session — the tool is idempotent "
        "(re-calls short-circuit with status='already_embedded' without "
        "mutating), but each repeated call still costs a schematic + "
        "library parse and an MCP round-trip. If you've already embedded "
        "a lib_id earlier in this conversation, skip ahead to "
        "sch_add_symbol directly. Use sch_list_symbols to query what's "
        "already embedded if uncertain. Supports dry_run; snapshots "
        "before write per ADR-0008."
    )
    input_model = SchEmbedLibSymbolInput
    output_model = SchEmbedLibSymbolOutput
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

    async def run(self, input: SchEmbedLibSymbolInput) -> SchEmbedLibSymbolOutput:
        # 1. Preflight — schematic path.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchEmbedLibSymbolOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchEmbedLibSymbolOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchEmbedLibSymbolOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 2. Preflight — library path.
        lib_path = input.lib_path.expanduser().resolve()
        if not lib_path.exists():
            return SchEmbedLibSymbolOutput(
                status="lib_not_found",
                sch_path=str(sch_path),
                note=f"no such library file: {lib_path}",
            )
        if not lib_path.is_file():
            return SchEmbedLibSymbolOutput(
                status="lib_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {lib_path}",
            )
        if lib_path.suffix.lower() != ".kicad_sym":
            return SchEmbedLibSymbolOutput(
                status="lib_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sym file: {lib_path} (got suffix "
                    f"{lib_path.suffix!r})."
                ),
            )

        # 3. Parse schematic.
        try:
            sch_doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchEmbedLibSymbolOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"schematic SEXPR parse failed: {exc}",
            )

        if sch_doc.top_head != "kicad_sch":
            return SchEmbedLibSymbolOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({sch_doc.top_head or '?'} ...)'."
                ),
            )

        # 4. Parse library. Routed through the same cache as the
        # schematic — library files aren't mutated, so cache hits across
        # repeated embed calls (e.g., embedding multiple symbols from
        # the same Device.kicad_sym) are pure wins.
        try:
            lib_doc = load_sexpr_doc(self._parse_cache, lib_path)
        except SexprParseError as exc:
            return SchEmbedLibSymbolOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"library SEXPR parse failed: {exc}",
            )

        if lib_doc.top_head != "kicad_symbol_lib":
            return SchEmbedLibSymbolOutput(
                status="invalid_lib",
                sch_path=str(sch_path),
                note=(
                    f"expected library top-level '(kicad_symbol_lib ...)' "
                    f"but got '({lib_doc.top_head or '?'} ...)'."
                ),
            )

        # 5. Find the requested symbol in the library.
        lib_prefix = input.lib_prefix or lib_path.stem
        lib_id = f"{lib_prefix}:{input.symbol_name}"

        lib_entry = _find_symbol_in_lib(lib_doc.root, input.symbol_name)
        if lib_entry is None:
            return SchEmbedLibSymbolOutput(
                status="symbol_not_found",
                sch_path=str(sch_path),
                lib_id=lib_id,
                note=(
                    f"symbol {input.symbol_name!r} not found in library "
                    f"{lib_path}. Available symbols: "
                    f"{', '.join(_list_lib_symbols(lib_doc.root)) or '(none)'}."
                ),
            )

        # 6. Ensure lib_symbols exists; check for duplicate.
        lib_symbols_node = sch_doc.root.find("lib_symbols")
        if lib_symbols_node is None:
            lib_symbols_node = slist(atom("lib_symbols"))
            sch_doc.root.append(lib_symbols_node)

        existing = _find_lib_symbol(lib_symbols_node, lib_id)
        if existing is not None:
            return SchEmbedLibSymbolOutput(
                status="already_embedded",
                sch_path=str(sch_path),
                lib_id=lib_id,
                note=(
                    f"{lib_id!r} is already present in the schematic's "
                    "lib_symbols block. No mutation needed."
                ),
            )

        # 7. Dry-run.
        if input.dry_run:
            return SchEmbedLibSymbolOutput(
                status="dry_run",
                sch_path=str(sch_path),
                lib_id=lib_id,
                note=(
                    f"dry_run=True; would embed {lib_id!r} from "
                    f"{lib_path}. Re-run with dry_run=False to apply."
                ),
            )

        # 8. Deep-clone the library entry and rename.
        cloned = _clone_and_qualify(lib_entry, lib_id)
        lib_symbols_node.append(cloned)

        # 9. Snapshot before write.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_embed_lib_symbol:{sch_path.name}:{lib_id}",
            )
        except SnapshotError as exc:
            return SchEmbedLibSymbolOutput(
                status="write_failed",
                sch_path=str(sch_path),
                lib_id=lib_id,
                note=f"snapshot failed before write: {exc}.",
            )

        # 10. Save.
        try:
            sch_doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchEmbedLibSymbolOutput(
                status="write_failed",
                sch_path=str(sch_path),
                lib_id=lib_id,
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchEmbedLibSymbolOutput(
            status="ok",
            sch_path=str(sch_path),
            lib_id=lib_id,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _find_symbol_in_lib(lib_root: SList, symbol_name: str) -> SList | None:
    """Return the ``(symbol "NAME" ...)`` child matching ``symbol_name``.

    Library files use unqualified names: ``(symbol "R_Small" ...)``.
    """
    for child in lib_root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) < 2:
            continue
        name_atom = child.items[1]
        if isinstance(name_atom, SAtom) and name_atom.text == symbol_name:
            return child
    return None


def _list_lib_symbols(lib_root: SList) -> list[str]:
    """Return names of all top-level ``(symbol ...)`` entries in a library."""
    names: list[str] = []
    for child in lib_root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) >= 2 and isinstance(child.items[1], SAtom):
            names.append(child.items[1].text)
    return names


def _clone_and_qualify(lib_entry: SList, lib_id: str) -> SList:
    """Deep-clone ``lib_entry`` and rename the top-level atom to ``lib_id``.

    Sub-symbols (``R_Small_0_1``, ``R_Small_1_1``) keep their original
    unqualified names — that's what KiCAD writes in schematic files.

    The cloned tree has all source spans cleared so the writer emits
    canonical form instead of trying to splice from the (wrong) source
    buffer. Without this, the serializer reads byte offsets that point
    into the library file's bytes while writing from the schematic's
    source — producing garbage.
    """
    cloned: SList = copy.deepcopy(lib_entry)
    _clear_spans(cloned)
    # items[0] is atom("symbol"), items[1] is the name atom.
    if len(cloned.items) >= 2 and isinstance(cloned.items[1], SAtom):
        cloned.items[1] = SAtom(text=lib_id, quoted=True)
    return cloned


def _clear_spans(node: SAtom | SList) -> None:
    """Recursively clear source spans so the writer treats nodes as dirty."""
    node.start = -1
    node.end = -1
    if isinstance(node, SList):
        for child in node.items:
            _clear_spans(child)


__all__ = [
    "SchEmbedLibSymbolInput",
    "SchEmbedLibSymbolOutput",
    "SchEmbedLibSymbolTool",
    "_clone_and_qualify",
    "_find_symbol_in_lib",
    "_list_lib_symbols",
]

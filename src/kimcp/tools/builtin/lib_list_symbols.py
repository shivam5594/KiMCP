"""lib_list_symbols — enumerate a .kicad_sym symbol library (M30).

The **first library-introspection READ tool**. M26-M28 look inside a
schematic; this one looks inside the *library files schematics pull
symbols from*. An LLM building a schematic needs to answer:

    "Does this lib contain the part I want?"
    "What's the Reference designator for it?"
    "Is there a description / keywords block I can cite?"

before calling ``sch_embed_lib_symbol`` + ``sch_add_symbol``. This
tool surfaces that metadata.

File shape (KiCAD 9.x)::

    (kicad_symbol_lib
      (version 20240108)
      (generator "kicad_symbol_editor")
      (generator_version "9.0")
      (symbol "R_Small"
        (pin_numbers hide)
        (pin_names (offset 0.254))
        (exclude_from_sim no)
        (in_bom yes)
        (on_board yes)
        (property "Reference" "R" ...)
        (property "Value" "R_Small" ...)
        (property "Footprint" "" ...)
        (property "Datasheet" "~" ...)
        (property "Description" "Resistor" ...)
        (property "ki_keywords" "R resistor" ...)
        (property "ki_fp_filters" "R_*" ...)
        ...
        (symbol "R_Small_0_1" ... graphics ...)
        (symbol "R_Small_1_1" ... pins ...)
      )
      (symbol "C_Small" ... )
    )

Returned fields per symbol entry: ``name``, ``reference``, ``value``,
``description``, ``keywords``, ``datasheet``, ``footprint_filters``
(list), ``pin_count``. The pin count is counted from the nested
``(pin ...)`` nodes across all unit-bodies (``symbol "X_0_1"``,
``symbol "X_1_1"``, …) — a common LLM question ("how many pins?") that
would otherwise require a second tool.

Filter: ``name_contains`` for quick narrowing. Deeper keyword search
lives in M31 (``lib_search_symbol``).

Status enum:

* **ok**             — entries enumerated (may be empty).
* **lib_not_found**  — path missing / wrong suffix / not a file.
* **parse_failed**   — SEXPR parser rejected the file.
* **invalid_schema** — parseable but top_head isn't ``kicad_symbol_lib``.

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


# -- envelope sub-models ---------------------------------------------------


class LibSymbolEntry(BaseModel):
    """One symbol definition from a .kicad_sym library."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(
        ..., description="Symbol name (the first positional atom after 'symbol')."
    )
    reference: str = Field(
        ...,
        description=(
            "Default reference designator prefix (e.g. 'R', 'C', 'U'). "
            "Empty when the lib entry has no Reference property."
        ),
    )
    value: str = Field(
        ...,
        description=(
            "Default value shown in the library browser (often the "
            "symbol name itself for generics, a part number for specific "
            "entries). Empty when absent."
        ),
    )
    description: str = Field(
        ..., description="Human-readable description. Empty when absent."
    )
    keywords: str = Field(
        ...,
        description=(
            "Space-separated keyword string (from the ki_keywords "
            "property). Empty when absent."
        ),
    )
    datasheet: str = Field(
        ...,
        description=(
            "Datasheet URL / path. '~' is the KiCAD convention for "
            "'none available' and is preserved verbatim. Empty when absent."
        ),
    )
    footprint_filters: list[str] = Field(
        default_factory=list,
        description=(
            "Space-separated patterns from the ki_fp_filters property, "
            "split into a list (e.g. ['R_*', 'Resistor_SMD:*']). The "
            "footprint picker uses these to narrow suggestions."
        ),
    )
    pin_count: int = Field(
        ...,
        description=(
            "Total number of pins across all units. Counted from (pin ...) "
            "nodes in the symbol's nested body sub-symbols."
        ),
    )


# -- input / output --------------------------------------------------------


class LibListSymbolsInput(BaseModel):
    lib_path: Path = Field(
        ...,
        description=(
            "Path to a .kicad_sym library file. Relative paths resolve "
            "against CWD."
        ),
    )
    name_contains: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring filter on the symbol name. "
            "Null returns every entry."
        ),
    )


class LibListSymbolsOutput(ToolOutput):
    status: Literal[
        "ok",
        "lib_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    lib_path: str | None = Field(default=None)
    symbols: list[LibSymbolEntry] = Field(
        default_factory=list,
        description="Library entries matching the filter, in document order.",
    )
    total: int = Field(default=0, description="Length of symbols after filtering.")
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibListSymbolsTool(Tool[LibListSymbolsInput, LibListSymbolsOutput]):
    """Enumerate the symbols defined in a .kicad_sym library."""

    name = "lib_list_symbols"
    version = "0.1.0"
    description = (
        "List the symbols defined in a .kicad_sym library file. Returns "
        "one entry per top-level (symbol \"name\" ...) node with name, "
        "reference-designator prefix, value, description, keywords, "
        "datasheet, footprint filters, and pin count. Supports "
        "name_contains substring filter (case-insensitive)."
    )
    input_model = LibListSymbolsInput
    output_model = LibListSymbolsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(self, input: LibListSymbolsInput) -> LibListSymbolsOutput:
        lib_path = input.lib_path.expanduser().resolve()
        if not lib_path.exists() or not lib_path.is_file():
            return LibListSymbolsOutput(
                status="lib_not_found",
                lib_path=None,
                note=f"no such file: {lib_path}",
            )
        if lib_path.suffix.lower() != ".kicad_sym":
            return LibListSymbolsOutput(
                status="lib_not_found",
                lib_path=str(lib_path),
                note=(
                    f"not a .kicad_sym file: {lib_path} (got suffix "
                    f"{lib_path.suffix!r})."
                ),
            )

        try:
            doc = SexprDocument.from_path(lib_path)
        except SexprParseError as exc:
            return LibListSymbolsOutput(
                status="parse_failed",
                lib_path=str(lib_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_symbol_lib":
            return LibListSymbolsOutput(
                status="invalid_schema",
                lib_path=str(lib_path),
                note=(
                    f"expected top-level '(kicad_symbol_lib ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        entries: list[LibSymbolEntry] = []
        needle = input.name_contains.lower() if input.name_contains else None
        for child in doc.root.items:
            if not isinstance(child, SList) or child.head != "symbol":
                continue
            entry = _parse_lib_symbol(child)
            if entry is None:
                continue
            if needle is not None and needle not in entry.name.lower():
                continue
            entries.append(entry)

        return LibListSymbolsOutput(
            status="ok",
            lib_path=str(lib_path),
            symbols=entries,
            total=len(entries),
        )


# -- parse helpers ---------------------------------------------------------


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _atom_at_index(node: SList, idx: int) -> str | None:
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


def _property_value(symbol: SList, name: str) -> str | None:
    """Return the (property "name" "value" ...) value string, or None."""
    for child in symbol.items:
        if not isinstance(child, SList) or child.head != "property":
            continue
        if len(child.items) < 3:
            continue
        key = _atom_text(child.items[1])
        if key != name:
            continue
        return _atom_text(child.items[2])
    return None


def _count_pins(symbol: SList) -> int:
    """Count ``(pin ...)`` nodes nested inside unit-body sub-symbols.

    A KiCAD lib symbol's body is spread across nested ``(symbol
    "<name>_<unit>_<bodystyle>" ...)`` children. The pins live inside
    those. Top-level pin nodes aren't emitted by KiCAD but we count
    them if they appear (defensive).
    """
    count = 0
    for child in symbol.items:
        if not isinstance(child, SList):
            continue
        if child.head == "pin":
            count += 1
        elif child.head == "symbol":
            # Nested unit body — recurse one level.
            for sub in child.items:
                if isinstance(sub, SList) and sub.head == "pin":
                    count += 1
    return count


def _parse_lib_symbol(symbol: SList) -> LibSymbolEntry | None:
    """Extract a ``LibSymbolEntry`` from one ``(symbol "name" ...)`` node.

    Returns None when the symbol has no positional name atom — that's
    structurally invalid and likely a malformed fixture rather than a
    real entry.
    """
    name = _atom_at_index(symbol, 1)
    if name is None:
        return None

    reference = _property_value(symbol, "Reference") or ""
    value = _property_value(symbol, "Value") or ""
    description = _property_value(symbol, "Description") or ""
    keywords = _property_value(symbol, "ki_keywords") or ""
    datasheet = _property_value(symbol, "Datasheet") or ""
    fp_filters_raw = _property_value(symbol, "ki_fp_filters") or ""
    fp_filters = fp_filters_raw.split() if fp_filters_raw else []

    return LibSymbolEntry(
        name=name,
        reference=reference,
        value=value,
        description=description,
        keywords=keywords,
        datasheet=datasheet,
        footprint_filters=fp_filters,
        pin_count=_count_pins(symbol),
    )


__all__ = [
    "LibListSymbolsInput",
    "LibListSymbolsOutput",
    "LibListSymbolsTool",
    "LibSymbolEntry",
]

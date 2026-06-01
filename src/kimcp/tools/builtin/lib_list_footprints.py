"""lib_list_footprints — enumerate a KiCAD footprint library directory.

The footprint-library counterpart to ``lib_list_symbols``. One key
structural difference: KiCAD stores footprints as *one ``.kicad_mod``
file per footprint* inside a ``.pretty/`` directory, not as a single
file containing many entries. So ``lib_path`` here is a *directory*,
not a file.

A ``.pretty`` library directory looks like::

    Resistor_SMD.pretty/
      R_0603_1608Metric.kicad_mod
      R_0805_2012Metric.kicad_mod
      R_1206_3216Metric.kicad_mod
      ...

Each ``.kicad_mod`` is a standalone s-expression with ``footprint``
as the top-level head::

    (footprint "R_0603_1608Metric"
      (version 20240108)
      (generator "pcbnew")
      (layer "F.Cu")
      (descr "Resistor SMD 0603 ...")
      (tags "resistor SMD 0603")
      (property "Reference" "REF**" ...)
      (property "Value" "R_0603_1608Metric" ...)
      (attr smd)
      (pad "1" smd ...)
      (pad "2" smd ...)
      ...
    )

Returned fields per entry: ``name``, ``description``, ``tags``,
``attributes`` (list — ``'smd'``, ``'through_hole'``, …),
``pad_count``. Plus the full relative filename so a caller can
round-trip to the source file.

Robustness: a single malformed ``.kicad_mod`` shouldn't tank the
whole listing. Broken files are dropped from ``entries`` and their
paths + reasons land in ``skipped`` so the caller can triage.

Filters compose (AND): ``name_contains="0603"`` + ``tag_contains="SMD"``
narrows to 0603 SMD parts.

Status enum:

* **ok**             — directory scanned (may be empty).
* **lib_not_found**  — path missing or not a directory.

Parse/invalid-schema live per-file in ``skipped``; they never fail
the whole tool call. That matches how a human with a broken
footprint feels the pain: one footprint is dud, the rest still work.

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


class LibFootprintEntry(BaseModel):
    """One footprint from a .pretty library directory."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(
        ...,
        description=(
            "Footprint name from the ``(footprint \"<name>\" ...)`` head. Usually "
            "matches the filename stem but we take the head-atom as authoritative."
        ),
    )
    file: str = Field(
        ...,
        description="Filename (basename only) — e.g. ``'R_0603_1608Metric.kicad_mod'``.",
    )
    description: str = Field(
        default="",
        description=(
            "Human-readable description — ``(descr ...)`` child or the "
            "``Description`` property, whichever is present."
        ),
    )
    tags: str = Field(
        default="",
        description=(
            "Space-separated keyword string from the ``(tags ...)`` child. "
            "Used by the footprint picker for fuzzy search."
        ),
    )
    attributes: list[str] = Field(
        default_factory=list,
        description=(
            "Footprint attributes from ``(attr ...)`` — typically "
            "``'smd'``, ``'through_hole'``, ``'exclude_from_pos_files'``, "
            "``'exclude_from_bom'``, ``'allow_missing_courtyard'``, etc."
        ),
    )
    pad_count: int = Field(
        default=0,
        description="Count of ``(pad ...)`` children — the physical pad count.",
    )


class LibFootprintSkip(BaseModel):
    """One .kicad_mod file the tool could not introspect."""

    model_config = ConfigDict(extra="allow")

    file: str = Field(..., description="Filename (basename) that failed.")
    reason: str = Field(
        ...,
        description=(
            "One-line explanation — ``'parse_failed'``, ``'invalid_schema'``, "
            "or ``'read_error'`` followed by the underlying detail."
        ),
    )


# -- input / output --------------------------------------------------------


class LibListFootprintsInput(BaseModel):
    lib_path: Path = Field(
        ...,
        description=(
            "Path to a footprint library directory (typically ending in "
            "``.pretty``). Relative paths resolve against CWD."
        ),
    )
    name_contains: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring filter on the footprint name. "
            "Null returns every footprint."
        ),
    )
    tag_contains: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring filter on the tags string. "
            "Useful for narrowing to a package family ('0603', 'SOIC', …)."
        ),
    )
    description_contains: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring filter on the description field."
        ),
    )


class LibListFootprintsOutput(ToolOutput):
    status: Literal[
        "ok",
        "lib_not_found",
    ]
    lib_path: str | None = Field(default=None)
    footprints: list[LibFootprintEntry] = Field(
        default_factory=list,
        description="Footprints that parsed cleanly, sorted by name.",
    )
    skipped: list[LibFootprintSkip] = Field(
        default_factory=list,
        description=(
            "Files that could not be introspected — malformed s-expression, "
            "wrong top-head, or read error. Sorted by file name."
        ),
    )
    total: int = Field(
        default=0,
        description="Count of footprints after filtering.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibListFootprintsTool(Tool[LibListFootprintsInput, LibListFootprintsOutput]):
    """List every footprint in a .pretty library directory."""

    name = "lib_list_footprints"
    version = "0.1.0"
    description = (
        "Enumerate footprints in a KiCAD footprint library directory (.pretty/). "
        "Returns name, description, tags, attributes, and pad count per "
        "footprint. Supports name / tag / description substring filters. "
        "Malformed .kicad_mod files surface in ``skipped`` rather than "
        "failing the whole call."
    )
    input_model = LibListFootprintsInput
    output_model = LibListFootprintsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(
        self, input: LibListFootprintsInput
    ) -> LibListFootprintsOutput:
        lib_path = input.lib_path.expanduser().resolve()
        if not lib_path.exists() or not lib_path.is_dir():
            return LibListFootprintsOutput(
                status="lib_not_found",
                lib_path=None,
                note=(
                    f"not a directory: {lib_path} "
                    "(footprint libraries are directories, typically named "
                    "'<libname>.pretty')."
                ),
            )

        # Case-normalize filter needles once.
        name_needle = (
            input.name_contains.lower() if input.name_contains else None
        )
        tag_needle = (
            input.tag_contains.lower() if input.tag_contains else None
        )
        desc_needle = (
            input.description_contains.lower()
            if input.description_contains
            else None
        )

        entries: list[LibFootprintEntry] = []
        skipped: list[LibFootprintSkip] = []

        # .pretty libraries are flat — no subdirectories. glob (not
        # rglob) keeps it that way and avoids descending into weird
        # nested structures if a user points at the wrong dir.
        for file in sorted(lib_path.glob("*.kicad_mod")):
            parsed = _parse_footprint_file(file)
            if isinstance(parsed, LibFootprintSkip):
                skipped.append(parsed)
                continue
            if name_needle is not None and name_needle not in parsed.name.lower():
                continue
            if tag_needle is not None and tag_needle not in parsed.tags.lower():
                continue
            if (
                desc_needle is not None
                and desc_needle not in parsed.description.lower()
            ):
                continue
            entries.append(parsed)

        entries.sort(key=lambda e: e.name)
        skipped.sort(key=lambda s: s.file)

        return LibListFootprintsOutput(
            status="ok",
            lib_path=str(lib_path),
            footprints=entries,
            skipped=skipped,
            total=len(entries),
        )


# -- parse helpers ---------------------------------------------------------


def _parse_footprint_file(file: Path) -> LibFootprintEntry | LibFootprintSkip:
    """Parse one ``.kicad_mod`` file into either an entry or a skip row.

    Errors never propagate — a skip row carries the reason so the
    caller can triage without the whole listing failing.
    """
    try:
        doc = SexprDocument.from_path(file)
    except SexprParseError as exc:
        return LibFootprintSkip(
            file=file.name, reason=f"parse_failed: {exc}"
        )
    except OSError as exc:
        return LibFootprintSkip(
            file=file.name, reason=f"read_error: {exc}"
        )

    if doc.top_head != "footprint":
        return LibFootprintSkip(
            file=file.name,
            reason=(
                f"invalid_schema: expected 'footprint' head, got "
                f"{doc.top_head or '?'!r}"
            ),
        )

    node = doc.root
    name = _atom_at_index(node, 1) or ""
    if not name:
        return LibFootprintSkip(
            file=file.name,
            reason="invalid_schema: footprint has no positional name atom",
        )

    # Description: prefer (descr ...) child; fall back to Description
    # property for fixtures that omit the top-level descr.
    description = _child_atom_text(node, "descr") or _property_value(
        node, "Description"
    ) or ""
    tags = _child_atom_text(node, "tags") or ""

    attributes: list[str] = []
    attr_node = node.find("attr")
    if attr_node is not None:
        for item in attr_node.items[1:]:
            if isinstance(item, SAtom):
                attributes.append(item.text)

    pad_count = sum(
        1
        for child in node.items
        if isinstance(child, SList) and child.head == "pad"
    )

    return LibFootprintEntry(
        name=name,
        file=file.name,
        description=description,
        tags=tags,
        attributes=attributes,
        pad_count=pad_count,
    )


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


def _property_value(node: SList, key: str) -> str | None:
    for prop in node.find_all("property"):
        if len(prop.items) < 3:
            continue
        key_atom = prop.items[1]
        if not isinstance(key_atom, SAtom) or key_atom.text != key:
            continue
        value_atom = prop.items[2]
        if isinstance(value_atom, SAtom):
            return value_atom.text
    return None


__all__ = [
    "LibFootprintEntry",
    "LibFootprintSkip",
    "LibListFootprintsInput",
    "LibListFootprintsOutput",
    "LibListFootprintsTool",
]

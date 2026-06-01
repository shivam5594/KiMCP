"""sch_list_nets — enumerate declared net names on a .kicad_sch.

The schematic-introspection companion to ``sch_list_labels`` and
``sch_list_symbols``. Where ``sch_list_labels`` lists every individual
label node and ``sch_list_symbols`` lists components, this tool
collapses by *net name* and reports what declares each.

What counts as a "declared net name":

* Every local label's text — names a touching net on this sheet.
* Every global label's text — names a project-wide net.
* Every hierarchical label's text — names a subsheet port.
* Every power-port symbol's ``Value`` property — e.g. a ``power:GND``
  instance declares the net ``GND``.

What does NOT count (and why the tool is named ``_declared``-flavored
rather than ``_all``): **unnamed wire-connected nets**. KiCAD's
netlister infers a net for any connected component even without a
label, synthesizing names like ``Net-(U1-Pad3)``. Deriving those
requires the full connectivity graph (wires + junctions + pin
positions) which is the netlister's job — invoke
``sch_export_netlist`` when you need that view.

Why this split exists: the common LLM-driven questions — "what power
rails are on this board?", "is ``VCC_5V`` used or dead-named?", "how
many sheets reference ``CLK_100M``?" — are answered entirely by the
declared-names subset. Running the full netlister for every such
question burns a subprocess when the answer is sitting in the
s-expression we already parsed.

Output shape: one entry per unique net name, carrying per-source
counts and a total. Sorted by name for deterministic output. Callers
that want individual label rows already have ``sch_list_labels``.

Status enum:

* **ok**             — nets enumerated (list may be empty).
* **sch_not_found**  — path missing / wrong suffix.
* **parse_failed**   — SEXPR parser rejected the file.
* **invalid_schema** — parseable but top_head isn't ``kicad_sch``.

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
from kimcp.sexpr.cache import ParseCache
from kimcp.tools.builtin._sexpr_build import load_sexpr_doc
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)


# Top-level head names that carry a net name at position [1]. Keeping
# the set here instead of a string literal in the main loop means
# adding a new source (e.g. a netclass directive in a future KiCAD
# version) is a single-line change.
_LABEL_HEADS: frozenset[str] = frozenset(
    {"label", "global_label", "hierarchical_label"}
)


# -- envelope sub-models ---------------------------------------------------


class NetDeclaration(BaseModel):
    """One unique net name and the count of declarations per source."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Net name as declared.")
    local_label_count: int = Field(
        default=0,
        description="Count of ``(label <name> ...)`` nodes with this name.",
    )
    global_label_count: int = Field(
        default=0,
        description="Count of ``(global_label <name> ...)`` nodes with this name.",
    )
    hierarchical_label_count: int = Field(
        default=0,
        description=(
            "Count of ``(hierarchical_label <name> ...)`` nodes with this name."
        ),
    )
    power_count: int = Field(
        default=0,
        description=(
            "Count of power-port symbol instances whose ``Value`` property "
            "equals this name (i.e., how many ``power:<name>`` symbols sit "
            "on the sheet)."
        ),
    )
    total: int = Field(
        default=0,
        description="Sum of all per-source counts.",
    )


# -- input / output --------------------------------------------------------


class SchListNetsInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    include_labels: bool = Field(
        default=True,
        description=(
            "Include net names declared via local / global / hierarchical labels."
        ),
    )
    include_power: bool = Field(
        default=True,
        description=(
            "Include net names declared via power-port symbols (lib_id starts "
            "with 'power:')."
        ),
    )
    name_contains: str | None = Field(
        default=None,
        description=(
            "Filter to net names containing this substring (case-sensitive). "
            "Null returns every declared net."
        ),
    )


class SchListNetsOutput(ToolOutput):
    status: Literal[
        "ok",
        "sch_not_found",
        "parse_failed",
        "invalid_schema",
    ]
    sch_path: str | None = Field(default=None)
    nets: list[NetDeclaration] = Field(
        default_factory=list,
        description="Declared nets after filtering, sorted by name.",
    )
    total: int = Field(
        default=0,
        description="Number of unique net names after filtering.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchListNetsTool(Tool[SchListNetsInput, SchListNetsOutput]):
    """Enumerate declared net names on a .kicad_sch."""

    name = "sch_list_nets"
    version = "0.1.0"
    description = (
        "Enumerate declared net names on a .kicad_sch with per-source counts "
        "(local / global / hierarchical labels + power symbols). Does NOT "
        "include wire-connected unnamed nets — use sch_export_netlist for "
        "the full netlister view."
    )
    input_model = SchListNetsInput
    output_model = SchListNetsOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    async def run(self, input: SchListNetsInput) -> SchListNetsOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or not sch_path.is_file():
            return SchListNetsOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchListNetsOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchListNetsOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchListNetsOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # Accumulate counts per net-name. Preserving insertion order
        # is not important — we sort at the end — but using dict-of-
        # dicts keeps the source-by-source increments readable.
        counts: dict[str, dict[str, int]] = {}

        def _bump(name: str, source: str) -> None:
            entry = counts.setdefault(
                name,
                {
                    "local_label": 0,
                    "global_label": 0,
                    "hierarchical_label": 0,
                    "power": 0,
                },
            )
            entry[source] += 1

        for child in doc.root.items:
            if not isinstance(child, SList):
                continue
            head = child.head or ""

            # Labels: text is positional [1].
            if input.include_labels and head in _LABEL_HEADS:
                name = _atom_at_index(child, 1)
                if name:
                    source = _LABEL_HEAD_TO_SOURCE[head]
                    _bump(name, source)
                continue

            # Power-port symbol instances: top-level (symbol ...) with
            # lib_id starting "power:". The net name is the Value
            # property's text. We don't trust the lib_id suffix
            # itself because KiCAD allows the Value to differ from
            # the lib_id's net slug in unusual cases, and the Value
            # is the canonical on-sheet rendering.
            if input.include_power and head == "symbol":
                name = _extract_power_net_name(child)
                if name:
                    _bump(name, "power")

        # Assemble output; apply name_contains filter; sort.
        nets: list[NetDeclaration] = []
        for name, per_source in counts.items():
            if (
                input.name_contains is not None
                and input.name_contains not in name
            ):
                continue
            total = sum(per_source.values())
            nets.append(
                NetDeclaration(
                    name=name,
                    local_label_count=per_source["local_label"],
                    global_label_count=per_source["global_label"],
                    hierarchical_label_count=per_source["hierarchical_label"],
                    power_count=per_source["power"],
                    total=total,
                )
            )
        nets.sort(key=lambda n: n.name)

        return SchListNetsOutput(
            status="ok",
            sch_path=str(sch_path),
            nets=nets,
            total=len(nets),
        )


# -- parse helpers ---------------------------------------------------------


_LABEL_HEAD_TO_SOURCE: dict[str, str] = {
    "label": "local_label",
    "global_label": "global_label",
    "hierarchical_label": "hierarchical_label",
}


def _atom_text(node: SAtom | SList | None) -> str | None:
    if isinstance(node, SAtom):
        return node.text
    return None


def _atom_at_index(node: SList, idx: int) -> str | None:
    if len(node.items) <= idx:
        return None
    return _atom_text(node.items[idx])


def _extract_power_net_name(symbol_node: SList) -> str | None:
    """Return the net name of a power-port symbol instance, or None.

    Non-power symbols are filtered out first by checking ``lib_id``
    prefix. The net name is taken from the Value property — see the
    docstring on ``sch_list_nets`` for why we don't trust the lib_id
    slug alone.
    """
    lib_id = _child_atom_text(symbol_node, "lib_id")
    if lib_id is None or not lib_id.startswith("power:"):
        return None

    # Properties are siblings; we want (property "Value" "<net>" ...).
    for prop in symbol_node.find_all("property"):
        if len(prop.items) < 3:
            continue
        key_atom = prop.items[1]
        if not isinstance(key_atom, SAtom) or key_atom.text != "Value":
            continue
        val_atom = prop.items[2]
        if not isinstance(val_atom, SAtom):
            continue
        # Empty Value is possible on malformed fixtures — treat as "no
        # declaration" rather than inventing an empty-name net.
        if not val_atom.text:
            return None
        return val_atom.text
    return None


def _child_atom_text(parent: SList, head: str, idx: int = 1) -> str | None:
    child = parent.find(head)
    if child is None or len(child.items) <= idx:
        return None
    return _atom_text(child.items[idx])


__all__ = [
    "NetDeclaration",
    "SchListNetsInput",
    "SchListNetsOutput",
    "SchListNetsTool",
]

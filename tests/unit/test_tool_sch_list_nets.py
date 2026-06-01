"""Unit tests for sch_list_nets.

Covers the same shape matrix as ``sch_list_labels`` (all/some/none,
document-parse errors, filter composition) plus the net-collapsing
math: two labels of the same name must land in one entry with
``local_label_count=2``, not two separate entries.

Deliberately no power-symbol fixture variation beyond "one present /
one absent / empty-Value" — the power-symbol shape itself is owned
by ``sch_add_power``'s tests. Here we only need to prove that
``sch_list_nets`` reads what ``sch_add_power`` writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.sch_list_nets import (
    SchListNetsInput,
    SchListNetsTool,
)

# Schematic with:
#  - 2 local labels named "CLK"  (same net name, different positions)
#  - 1 local label named "DATA"
#  - 1 global label named "VCC"
#  - 1 hierarchical label named "CLK_OUT"
#  - 1 power symbol with Value "GND"
#  - 1 non-power symbol (should be ignored)
_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(label "CLK"
\t\t(at 100 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t)
\t(label "CLK"
\t\t(at 105 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000002")
\t)
\t(label "DATA"
\t\t(at 110 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000003")
\t)
\t(global_label "VCC"
\t\t(shape input)
\t\t(at 120 50 0)
\t\t(fields_autoplaced yes)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "bbbbbbbb-0000-0000-0000-000000000001")
\t)
\t(hierarchical_label "CLK_OUT"
\t\t(shape output)
\t\t(at 130 50 180)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "cccccccc-0000-0000-0000-000000000001")
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 50 50 0)
\t\t(unit 1)
\t\t(in_bom no)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "dddddddd-0000-0000-0000-000000000001")
\t\t(property "Reference" "#PWR01" (at 50 48 0))
\t\t(property "Value" "GND" (at 50 52 0))
\t\t(property "Footprint" "" (at 50 50 0))
\t\t(property "Datasheet" "" (at 50 50 0))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 200 200 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "eeeeeeee-0000-0000-0000-000000000001")
\t\t(property "Reference" "R1" (at 202 198 0))
\t\t(property "Value" "10k" (at 202 202 0))
\t)
)
"""


def _write(tmp_path: Path, body: str = _SCH) -> Path:
    sch = tmp_path / "design.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = SchListNetsTool()
    assert tool.name == "sch_list_nets"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_nets_with_counts(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch))
    assert out.status == "ok"
    # 5 unique names: CLK, DATA, VCC, CLK_OUT, GND
    assert out.total == 5
    # Sorted by name — pin the order.
    names = [net.name for net in out.nets]
    assert names == ["CLK", "CLK_OUT", "DATA", "GND", "VCC"]


@pytest.mark.asyncio
async def test_duplicate_local_labels_collapse(tmp_path: Path) -> None:
    """Two ``(label "CLK" ...)`` → one entry with local_label_count=2.

    The whole point of this tool (vs ``sch_list_labels``) is the
    collapse; pin it so a regression doesn't accidentally emit two
    separate entries.
    """
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch))
    clk_entry = next(n for n in out.nets if n.name == "CLK")
    assert clk_entry.local_label_count == 2
    assert clk_entry.global_label_count == 0
    assert clk_entry.hierarchical_label_count == 0
    assert clk_entry.power_count == 0
    assert clk_entry.total == 2


@pytest.mark.asyncio
async def test_per_source_counts_are_reported(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch))
    # One entry per kind to smoke-test the attribution.
    by_name = {n.name: n for n in out.nets}
    assert by_name["VCC"].global_label_count == 1
    assert by_name["CLK_OUT"].hierarchical_label_count == 1
    assert by_name["GND"].power_count == 1
    assert by_name["DATA"].local_label_count == 1


@pytest.mark.asyncio
async def test_non_power_symbols_are_ignored(tmp_path: Path) -> None:
    """Device:R is in the fixture — its ``Value = "10k"`` must NOT show
    up as a net named "10k". Only power: lib_ids count as net
    declarations.
    """
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch))
    names = {n.name for n in out.nets}
    assert "10k" not in names
    assert "R1" not in names


# -- filters --------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_labels_false_excludes_label_sources(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch, include_labels=False))
    assert out.status == "ok"
    # Only GND (from the power symbol) should remain.
    assert [n.name for n in out.nets] == ["GND"]


@pytest.mark.asyncio
async def test_include_power_false_excludes_power_symbols(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch, include_power=False))
    assert out.status == "ok"
    names = {n.name for n in out.nets}
    assert "GND" not in names
    # The label-declared nets remain.
    assert names == {"CLK", "CLK_OUT", "DATA", "VCC"}


@pytest.mark.asyncio
async def test_name_contains_filter(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch, name_contains="CLK"))
    assert out.status == "ok"
    assert [n.name for n in out.nets] == ["CLK", "CLK_OUT"]


@pytest.mark.asyncio
async def test_name_contains_case_sensitive(tmp_path: Path) -> None:
    """``name_contains`` is documented case-sensitive — pin it so a
    refactor to ``.casefold()`` doesn't silently change semantics."""
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=sch, name_contains="clk"))
    assert out.status == "ok"
    assert out.nets == []


@pytest.mark.asyncio
async def test_both_filters_false_returns_empty(tmp_path: Path) -> None:
    """``include_labels=False, include_power=False`` is a degenerate
    but valid request — returns an empty list with status=ok.
    Surfaces the "you filtered everything out" case as an empty list,
    not an error, so a client can distinguish from
    "schematic is empty" (both are empty)."""
    sch = _write(tmp_path)
    tool = SchListNetsTool()
    out = await tool.run(
        SchListNetsInput(sch_path=sch, include_labels=False, include_power=False)
    )
    assert out.status == "ok"
    assert out.total == 0
    assert out.nets == []


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=tmp_path / "nope.kicad_sch"))
    assert out.status == "sch_not_found"
    assert out.note is not None
    assert out.nets == []


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wrong.txt"
    f.write_text(_SCH, encoding="utf-8")
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=f))
    assert out.status == "sch_not_found"
    assert out.nets == []


@pytest.mark.asyncio
async def test_parse_failure(tmp_path: Path) -> None:
    f = tmp_path / "broken.kicad_sch"
    f.write_text("(kicad_sch (unterminated ", encoding="utf-8")
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=f))
    assert out.status == "parse_failed"
    assert out.nets == []


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    f = tmp_path / "pcb_like.kicad_sch"
    f.write_text('(kicad_pcb (version 20240108) (generator "pcbnew"))', encoding="utf-8")
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=f))
    assert out.status == "invalid_schema"
    assert out.nets == []


# -- edge cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sheet(tmp_path: Path) -> None:
    """A schematic with no labels and no power symbols reports no nets."""
    body = (
        '(kicad_sch (version 20240108) (generator "eeschema")'
        ' (uuid "11111111-2222-3333-4444-555555555555") (paper "A4") (lib_symbols))'
    )
    f = tmp_path / "empty.kicad_sch"
    f.write_text(body, encoding="utf-8")
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=f))
    assert out.status == "ok"
    assert out.total == 0
    assert out.nets == []


@pytest.mark.asyncio
async def test_power_symbol_with_empty_value_is_skipped(tmp_path: Path) -> None:
    """A malformed power instance with an empty Value must not surface
    as a net named "". Treat it as a declaration that never
    happened."""
    body = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 50 50 0)
\t\t(uuid "dddddddd-0000-0000-0000-000000000001")
\t\t(property "Reference" "#PWR01" (at 50 48 0))
\t\t(property "Value" "" (at 50 52 0))
\t)
)
"""
    f = tmp_path / "malformed.kicad_sch"
    f.write_text(body, encoding="utf-8")
    tool = SchListNetsTool()
    out = await tool.run(SchListNetsInput(sch_path=f))
    assert out.status == "ok"
    assert out.nets == []

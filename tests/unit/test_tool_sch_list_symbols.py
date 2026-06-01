"""Unit tests for sch_list_symbols (M26)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.sch_list_symbols import (
    SchListSymbolsInput,
    SchListSymbolsTool,
)

# Full schematic fixture with two symbols (R1, C1) plus various edge
# cases: hidden reference, empty value, rotated symbol. Written as a
# single constant so tests read top-to-bottom without jumping around.
_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t\t(property "Reference" "R1" (at 102 48 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "10k" (at 102 52 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 100 50 0) (effects (hide yes)))
\t\t(property "Datasheet" "~" (at 100 50 0) (effects (hide yes)))
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 60 90)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp yes)
\t\t(uuid "bbbbbbbb-0000-0000-0000-000000000002")
\t\t(property "Reference" "C1" (at 122 58 0))
\t\t(property "Value" "" (at 122 62 0))
\t\t(property "Footprint" "" (at 120 60 0) (effects (hide yes)))
\t)
\t(symbol
\t\t(lib_id "Transistor_BJT:2N3904")
\t\t(at 140 70 180)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom no)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "cccccccc-0000-0000-0000-000000000003")
\t\t(property "Reference" "Q1" (at 142 68 0))
\t\t(property "Value" "2N3904" (at 142 72 0))
\t\t(property "Footprint" "Package_TO_SOT_THT:TO-92_Inline" (at 140 70 0) (effects (hide yes)))
\t)
)
"""


def _write(tmp_path: Path, body: str = _SCH) -> Path:
    sch = tmp_path / "design.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = SchListSymbolsTool()
    assert tool.name == "sch_list_symbols"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_symbols(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 3
    assert len(out.symbols) == 3
    # Document order preserved.
    refs = [s.reference for s in out.symbols]
    assert refs == ["R1", "C1", "Q1"]


@pytest.mark.asyncio
async def test_symbol_fields_are_complete(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    r1 = next(s for s in out.symbols if s.reference == "R1")
    assert r1.uuid == "aaaaaaaa-0000-0000-0000-000000000001"
    assert r1.lib_id == "Device:R"
    assert r1.value == "10k"
    assert r1.footprint == "Resistor_SMD:R_0603_1608Metric"
    assert r1.at_x == 100.0
    assert r1.at_y == 50.0
    assert r1.angle == 0.0
    assert r1.unit == 1
    assert r1.in_bom is True
    assert r1.on_board is True
    assert r1.dnp is False


@pytest.mark.asyncio
async def test_rotated_symbol_angle(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    c1 = next(s for s in out.symbols if s.reference == "C1")
    assert c1.angle == 90.0
    assert c1.dnp is True
    # Empty value preserved.
    assert c1.value == ""
    assert c1.footprint == ""


@pytest.mark.asyncio
async def test_in_bom_false_preserved(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    q1 = next(s for s in out.symbols if s.reference == "Q1")
    assert q1.in_bom is False
    assert q1.on_board is True


# -- filters ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_reference_prefix(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(
        SchListSymbolsInput(sch_path=sch, reference_prefix="R")
    )
    assert out.total == 1
    assert out.symbols[0].reference == "R1"


@pytest.mark.asyncio
async def test_filter_reference_prefix_no_match(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(
        SchListSymbolsInput(sch_path=sch, reference_prefix="Z")
    )
    assert out.status == "ok"
    assert out.total == 0
    assert out.symbols == []


@pytest.mark.asyncio
async def test_filter_lib_id_contains(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    out = await tool.run(
        SchListSymbolsInput(sch_path=sch, lib_id_contains="Transistor")
    )
    assert out.total == 1
    assert out.symbols[0].lib_id.startswith("Transistor_BJT:")


@pytest.mark.asyncio
async def test_filter_combined(tmp_path: Path) -> None:
    """Both filters AND together."""
    sch = _write(tmp_path)
    tool = SchListSymbolsTool()
    # Q starts with Q AND lib_id contains BJT → matches Q1 only.
    out = await tool.run(
        SchListSymbolsInput(
            sch_path=sch,
            reference_prefix="Q",
            lib_id_contains="BJT",
        )
    )
    assert out.total == 1
    # No overlap → empty.
    out2 = await tool.run(
        SchListSymbolsInput(
            sch_path=sch,
            reference_prefix="R",
            lib_id_contains="BJT",
        )
    )
    assert out2.total == 0


# -- empty schematic -------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_schematic(tmp_path: Path) -> None:
    empty = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "00000000-1111-2222-3333-444444444444")
\t(paper "A4")
\t(lib_symbols)
)
"""
    sch = _write(tmp_path, empty)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 0
    assert out.symbols == []


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_missing(tmp_path: Path) -> None:
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=tmp_path / "nope.kicad_sch"))
    assert out.status == "sch_not_found"
    assert out.sch_path is None


@pytest.mark.asyncio
async def test_sch_not_found_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=d))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_not_found_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=f))
    assert out.status == "sch_not_found"
    assert out.note is not None and ".kicad_sch" in out.note


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "wrong.kicad_sch"
    sch.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "invalid_schema"


# -- defensive parsing -----------------------------------------------------


@pytest.mark.asyncio
async def test_symbol_without_lib_id_is_skipped(tmp_path: Path) -> None:
    """Malformed symbol nodes are skipped, not crashed."""
    broken = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(at 0 0 0)
\t\t(uuid "dead-0000-0000-0000-000000000000")
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 10 20 0)
\t\t(uuid "cafe-0000-0000-0000-000000000000")
\t\t(property "Reference" "R1" (at 12 18 0))
\t\t(property "Value" "1k" (at 12 22 0))
\t)
)
"""
    sch = _write(tmp_path, broken)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "ok"
    # Only the well-formed one comes through.
    assert out.total == 1
    assert out.symbols[0].reference == "R1"


@pytest.mark.asyncio
async def test_missing_reference_property_defaults(tmp_path: Path) -> None:
    no_ref = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 10 20 0)
\t\t(uuid "cafe-0000-0000-0000-000000000000")
\t\t(property "Value" "1k" (at 12 22 0))
\t)
)
"""
    sch = _write(tmp_path, no_ref)
    tool = SchListSymbolsTool()
    out = await tool.run(SchListSymbolsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 1
    assert out.symbols[0].reference == "?"
    assert out.symbols[0].value == "1k"
    assert out.symbols[0].footprint == ""

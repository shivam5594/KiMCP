"""Unit tests for sch_list_wires (M28)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.sch_list_wires import (
    SchListWiresInput,
    SchListWiresTool,
)

# Mixed fixture: two wires (forming an L), one junction at the corner,
# two no_connects, plus a label and a symbol (to prove we ignore non-
# connectivity children).
_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(wire
\t\t(pts (xy 100 50) (xy 150 50))
\t\t(stroke (width 0) (type default))
\t\t(uuid "w1111111-0000-0000-0000-000000000001")
\t)
\t(wire
\t\t(pts (xy 150 50) (xy 150 80))
\t\t(stroke (width 0) (type default))
\t\t(uuid "w2222222-0000-0000-0000-000000000002")
\t)
\t(junction
\t\t(at 150 50)
\t\t(diameter 0)
\t\t(color 0 0 0 0)
\t\t(uuid "jaaaaaa-0000-0000-0000-000000000001")
\t)
\t(junction
\t\t(at 200 100)
\t\t(diameter 0.5)
\t\t(color 0 0 0 0)
\t\t(uuid "jbbbbbb-0000-0000-0000-000000000002")
\t)
\t(no_connect
\t\t(at 300 100)
\t\t(uuid "n1111111-0000-0000-0000-000000000001")
\t)
\t(no_connect
\t\t(at 310 100)
\t\t(uuid "n2222222-0000-0000-0000-000000000002")
\t)
\t(label "CLK"
\t\t(at 50 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "l1111111-0000-0000-0000-000000000001")
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 200 200 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "s1111111-0000-0000-0000-000000000001")
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
    tool = SchListWiresTool()
    assert tool.name == "sch_list_wires"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_everything_by_default(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "ok"
    assert len(out.wires) == 2
    assert len(out.junctions) == 2
    assert len(out.no_connects) == 2
    assert out.total == 6


@pytest.mark.asyncio
async def test_wire_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    w1 = out.wires[0]
    assert w1.uuid == "w1111111-0000-0000-0000-000000000001"
    assert w1.start_x == 100.0
    assert w1.start_y == 50.0
    assert w1.end_x == 150.0
    assert w1.end_y == 50.0
    w2 = out.wires[1]
    # Vertical leg of the L — distinct endpoints preserved.
    assert w2.start_x == 150.0
    assert w2.end_y == 80.0


@pytest.mark.asyncio
async def test_junction_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    j1 = out.junctions[0]
    assert j1.uuid == "jaaaaaa-0000-0000-0000-000000000001"
    assert j1.at_x == 150.0
    assert j1.at_y == 50.0
    assert j1.diameter == 0.0  # default marker
    j2 = out.junctions[1]
    assert j2.diameter == 0.5  # explicit override preserved


@pytest.mark.asyncio
async def test_no_connect_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    nc = out.no_connects[0]
    assert nc.uuid == "n1111111-0000-0000-0000-000000000001"
    assert nc.at_x == 300.0
    assert nc.at_y == 100.0


# -- filters ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_wires_only(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch, include=["wire"]))
    assert len(out.wires) == 2
    assert out.junctions == []
    assert out.no_connects == []
    assert out.total == 2


@pytest.mark.asyncio
async def test_include_junctions_only(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch, include=["junction"]))
    assert out.wires == []
    assert len(out.junctions) == 2
    assert out.no_connects == []


@pytest.mark.asyncio
async def test_include_no_connects_only(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch, include=["no_connect"]))
    assert out.wires == []
    assert out.junctions == []
    assert len(out.no_connects) == 2


@pytest.mark.asyncio
async def test_include_multiple(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListWiresTool()
    out = await tool.run(
        SchListWiresInput(sch_path=sch, include=["wire", "no_connect"])
    )
    assert len(out.wires) == 2
    assert out.junctions == []
    assert len(out.no_connects) == 2
    assert out.total == 4


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
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 0


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_missing(tmp_path: Path) -> None:
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=tmp_path / "nope.kicad_sch"))
    assert out.status == "sch_not_found"
    assert out.sch_path is None


@pytest.mark.asyncio
async def test_sch_not_found_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=f))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_not_found_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=d))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "wrong.kicad_sch"
    sch.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "invalid_schema"


# -- defensive parsing -----------------------------------------------------


@pytest.mark.asyncio
async def test_wire_without_uuid_is_skipped(tmp_path: Path) -> None:
    broken = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(wire
\t\t(pts (xy 0 0) (xy 10 0))
\t)
\t(wire
\t\t(pts (xy 0 0) (xy 10 0))
\t\t(uuid "cafe-0000-0000-0000-000000000000")
\t)
)
"""
    sch = _write(tmp_path, broken)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "ok"
    assert len(out.wires) == 1
    assert out.wires[0].uuid == "cafe-0000-0000-0000-000000000000"


@pytest.mark.asyncio
async def test_wire_without_pts_is_skipped(tmp_path: Path) -> None:
    broken = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(wire
\t\t(uuid "dead-0000-0000-0000-000000000000")
\t)
)
"""
    sch = _write(tmp_path, broken)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "ok"
    assert out.wires == []


@pytest.mark.asyncio
async def test_wire_with_single_point_is_skipped(tmp_path: Path) -> None:
    """A (pts) with fewer than two (xy) children is malformed."""
    broken = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(wire
\t\t(pts (xy 100 50))
\t\t(uuid "dead-0000-0000-0000-000000000000")
\t)
)
"""
    sch = _write(tmp_path, broken)
    tool = SchListWiresTool()
    out = await tool.run(SchListWiresInput(sch_path=sch))
    assert out.status == "ok"
    assert out.wires == []

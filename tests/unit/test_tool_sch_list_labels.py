"""Unit tests for sch_list_labels (M27)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.sch_list_labels import (
    SchListLabelsInput,
    SchListLabelsTool,
)

# A schematic mixing all three label kinds plus a symbol (to prove we
# don't accidentally pick up non-label children) and a wire (same).
# Globals carry Intersheetrefs, hierarchicals carry shape.
_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(label "CLK_A"
\t\t(at 100 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t)
\t(label "RESET"
\t\t(at 110 50 90)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000002")
\t)
\t(global_label "VCC"
\t\t(shape input)
\t\t(at 120 50 0)
\t\t(fields_autoplaced yes)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "bbbbbbbb-0000-0000-0000-000000000001")
\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}" (at 120 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes)))
\t)
\t(hierarchical_label "CLK_OUT"
\t\t(shape output)
\t\t(at 130 50 180)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "cccccccc-0000-0000-0000-000000000001")
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 200 200 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "dddddddd-0000-0000-0000-000000000001")
\t\t(property "Reference" "R1" (at 202 198 0))
\t\t(property "Value" "10k" (at 202 202 0))
\t)
\t(wire
\t\t(pts (xy 0 0) (xy 10 0))
\t\t(uuid "eeeeeeee-0000-0000-0000-000000000001")
\t)
)
"""


def _write(tmp_path: Path, body: str = _SCH) -> Path:
    sch = tmp_path / "design.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = SchListLabelsTool()
    assert tool.name == "sch_list_labels"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_labels(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 4
    assert len(out.labels) == 4
    # Document order preserved.
    texts = [label.text for label in out.labels]
    assert texts == ["CLK_A", "RESET", "VCC", "CLK_OUT"]


@pytest.mark.asyncio
async def test_local_label_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    clk = next(label for label in out.labels if label.text == "CLK_A")
    assert clk.kind == "local"
    assert clk.uuid == "aaaaaaaa-0000-0000-0000-000000000001"
    assert clk.shape is None  # local labels never carry a shape
    assert clk.at_x == 100.0
    assert clk.at_y == 50.0
    assert clk.angle == 0.0

    reset = next(label for label in out.labels if label.text == "RESET")
    assert reset.kind == "local"
    assert reset.angle == 90.0


@pytest.mark.asyncio
async def test_global_label_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    vcc = next(label for label in out.labels if label.text == "VCC")
    assert vcc.kind == "global"
    assert vcc.shape == "input"
    assert vcc.at_x == 120.0


@pytest.mark.asyncio
async def test_hierarchical_label_fields(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    h = next(label for label in out.labels if label.text == "CLK_OUT")
    assert h.kind == "hierarchical"
    assert h.shape == "output"
    assert h.angle == 180.0


# -- filters ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_kind_local(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch, kind="local"))
    assert out.total == 2
    assert {label.kind for label in out.labels} == {"local"}


@pytest.mark.asyncio
async def test_filter_kind_global(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch, kind="global"))
    assert out.total == 1
    assert out.labels[0].text == "VCC"


@pytest.mark.asyncio
async def test_filter_kind_hierarchical(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch, kind="hierarchical"))
    assert out.total == 1
    assert out.labels[0].text == "CLK_OUT"


@pytest.mark.asyncio
async def test_filter_text_contains(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch, text_contains="CLK"))
    # CLK_A (local) and CLK_OUT (hierarchical).
    assert out.total == 2
    texts = {label.text for label in out.labels}
    assert texts == {"CLK_A", "CLK_OUT"}


@pytest.mark.asyncio
async def test_filter_text_no_match(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch, text_contains="ZZZ"))
    assert out.status == "ok"
    assert out.total == 0
    assert out.labels == []


@pytest.mark.asyncio
async def test_filter_combined(tmp_path: Path) -> None:
    """kind + text_contains AND together."""
    sch = _write(tmp_path)
    tool = SchListLabelsTool()
    # CLK in local labels → CLK_A only.
    out = await tool.run(
        SchListLabelsInput(sch_path=sch, kind="local", text_contains="CLK")
    )
    assert out.total == 1
    assert out.labels[0].text == "CLK_A"
    # CLK in global labels → none.
    out2 = await tool.run(
        SchListLabelsInput(sch_path=sch, kind="global", text_contains="CLK")
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
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 0


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_missing(tmp_path: Path) -> None:
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=tmp_path / "nope.kicad_sch"))
    assert out.status == "sch_not_found"
    assert out.sch_path is None


@pytest.mark.asyncio
async def test_sch_not_found_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=f))
    assert out.status == "sch_not_found"
    assert out.note is not None and ".kicad_sch" in out.note


@pytest.mark.asyncio
async def test_sch_not_found_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=d))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "wrong.kicad_sch"
    sch.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.status == "invalid_schema"


# -- defensive parsing -----------------------------------------------------


@pytest.mark.asyncio
async def test_label_without_uuid_is_skipped(tmp_path: Path) -> None:
    """A label node without a uuid is malformed — skip, don't crash."""
    broken = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(label "NO_UUID"
\t\t(at 0 0 0)
\t)
\t(label "HAS_UUID"
\t\t(at 10 20 0)
\t\t(uuid "cafe-0000-0000-0000-000000000000")
\t)
)
"""
    sch = _write(tmp_path, broken)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total == 1
    assert out.labels[0].text == "HAS_UUID"


@pytest.mark.asyncio
async def test_shape_not_reported_for_local(tmp_path: Path) -> None:
    """Local labels never carry a shape, even if a fixture adds one."""
    odd = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(label "STRANGE"
\t\t(shape input)
\t\t(at 0 0 0)
\t\t(uuid "cafe-0000-0000-0000-000000000000")
\t)
)
"""
    sch = _write(tmp_path, odd)
    tool = SchListLabelsTool()
    out = await tool.run(SchListLabelsInput(sch_path=sch))
    assert out.total == 1
    assert out.labels[0].kind == "local"
    assert out.labels[0].shape is None

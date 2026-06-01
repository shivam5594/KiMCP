"""Unit tests for pcb_list_footprints.

Mirrors the ``sch_list_*`` matrix (all/filtered/none, parse errors,
filter composition) adapted for the .kicad_pcb shape. The load-
bearing bits are:

* layer alias resolution — "top" → F.Cu, "bottom" → B.Cu, and
  explicit layer names pass through.
* required-field guard — footprints missing lib_ref, uuid, or the
  Reference property are skipped rather than promoted to a parse
  error. KiCAD-written boards always have them; fixtures might not.
* empty Value preservation — a fiducial or silk-only part with
  ``(property "Value" "")`` should list with ``value=""`` (not be
  treated as missing).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.pcb_list_footprints import (
    PcbListFootprintsInput,
    PcbListFootprintsTool,
)

# Minimal .kicad_pcb fixture with:
#  - R1 on F.Cu (Resistor_SMD:R_0603_1608Metric, value "10k", rotated 90°)
#  - R2 on F.Cu (Resistor_SMD:R_0603_1608Metric, value "4.7k")
#  - U1 on B.Cu (Package_SO:SOIC-8_3.9x4.9mm_P1.27mm, value "NE555")
#  - C3 on F.Cu (Capacitor_SMD:C_0603_1608Metric, value "" — fiducial-like)
#
# Intentionally skips most of the real KiCAD payload (layers stanza,
# setup, net 0, etc.) — pcb_list_footprints reads top-level
# `(footprint ...)` children and nothing else, so the rest is noise
# for this tool's contract.
_PCB = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers)
\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t\t(at 100 50 90)
\t\t(property "Reference" "R1" (at 100 48 0))
\t\t(property "Value" "10k" (at 100 52 0))
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 100 50 0))
\t)
\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "11111111-0000-0000-0000-000000000002")
\t\t(at 110 50)
\t\t(property "Reference" "R2" (at 110 48 0))
\t\t(property "Value" "4.7k" (at 110 52 0))
\t)
\t(footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
\t\t(layer "B.Cu")
\t\t(uuid "22222222-0000-0000-0000-000000000001")
\t\t(at 150 75 180)
\t\t(property "Reference" "U1" (at 150 72 0))
\t\t(property "Value" "NE555" (at 150 78 0))
\t)
\t(footprint "Capacitor_SMD:C_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "33333333-0000-0000-0000-000000000001")
\t\t(at 200 100 0)
\t\t(property "Reference" "C3" (at 200 98 0))
\t\t(property "Value" "" (at 200 102 0))
\t)
)
"""


def _write(tmp_path: Path, body: str = _PCB) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(body, encoding="utf-8")
    return pcb


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = PcbListFootprintsTool()
    assert tool.name == "pcb_list_footprints"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_footprints(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.total == 4
    # Sorted by reference: C3, R1, R2, U1.
    refs = [fp.reference for fp in out.footprints]
    assert refs == ["C3", "R1", "R2", "U1"]


@pytest.mark.asyncio
async def test_fields_are_populated(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb))
    r1 = next(fp for fp in out.footprints if fp.reference == "R1")
    assert r1.uuid == "11111111-0000-0000-0000-000000000001"
    assert r1.lib_ref == "Resistor_SMD:R_0603_1608Metric"
    assert r1.value == "10k"
    assert r1.layer == "F.Cu"
    assert r1.at_x == 100.0
    assert r1.at_y == 50.0
    assert r1.angle == 90.0


@pytest.mark.asyncio
async def test_empty_value_is_preserved(tmp_path: Path) -> None:
    """Fiducial-like parts legitimately have ``Value=""`` — distinguish
    from missing property. The parser should keep them in the list
    rather than drop them as malformed."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb))
    c3 = next(fp for fp in out.footprints if fp.reference == "C3")
    assert c3.value == ""


@pytest.mark.asyncio
async def test_missing_angle_defaults_to_zero(tmp_path: Path) -> None:
    """``(at X Y)`` with no rotation is shorthand for angle=0 in the
    KiCAD format. R2's fixture omits the angle — verify the parser
    doesn't blow up or invent a nonzero default."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb))
    r2 = next(fp for fp in out.footprints if fp.reference == "R2")
    assert r2.angle == 0.0
    assert r2.at_x == 110.0
    assert r2.at_y == 50.0


# -- filters: layer --------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_filter_top_alias(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, layer="top"))
    assert out.status == "ok"
    refs = [fp.reference for fp in out.footprints]
    assert refs == ["C3", "R1", "R2"]


@pytest.mark.asyncio
async def test_layer_filter_bottom_alias(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, layer="bottom"))
    assert out.status == "ok"
    refs = [fp.reference for fp in out.footprints]
    assert refs == ["U1"]


@pytest.mark.asyncio
async def test_layer_filter_explicit_canonical(tmp_path: Path) -> None:
    """Explicit canonical names pass through unchanged — important for
    inner-copper footprints on assembly boards where "top"/"bottom"
    don't apply."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, layer="F.Cu"))
    assert out.status == "ok"
    refs = [fp.reference for fp in out.footprints]
    assert refs == ["C3", "R1", "R2"]


@pytest.mark.asyncio
async def test_layer_filter_unknown_returns_empty(tmp_path: Path) -> None:
    """An explicit layer that no footprint sits on yields an empty list,
    not an error — "nothing matched" is a valid filter outcome."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, layer="In1.Cu"))
    assert out.status == "ok"
    assert out.footprints == []
    assert out.total == 0


# -- filters: string contains ---------------------------------------------


@pytest.mark.asyncio
async def test_ref_contains_filter(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, ref_contains="R"))
    assert [fp.reference for fp in out.footprints] == ["R1", "R2"]


@pytest.mark.asyncio
async def test_ref_contains_case_sensitive(tmp_path: Path) -> None:
    """``ref_contains`` is documented case-sensitive. Pin so a refactor
    to ``.casefold()`` doesn't silently change semantics."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, ref_contains="r"))
    assert out.footprints == []


@pytest.mark.asyncio
async def test_value_contains_filter(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, value_contains="10k"))
    assert [fp.reference for fp in out.footprints] == ["R1"]


@pytest.mark.asyncio
async def test_lib_contains_filter(tmp_path: Path) -> None:
    """'all 0603 parts' style query spans both resistor and capacitor
    lib_refs — lib_contains on "0603" catches both families."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=pcb, lib_contains="0603"))
    refs = [fp.reference for fp in out.footprints]
    assert refs == ["C3", "R1", "R2"]


@pytest.mark.asyncio
async def test_filters_compose_with_and(tmp_path: Path) -> None:
    """Layer + ref_contains together: "top-side resistors" should hit
    R1 + R2 only. The U1 IC is on the bottom; C3 is a capacitor."""
    pcb = _write(tmp_path)
    tool = PcbListFootprintsTool()
    out = await tool.run(
        PcbListFootprintsInput(pcb_path=pcb, layer="top", ref_contains="R")
    )
    assert [fp.reference for fp in out.footprints] == ["R1", "R2"]


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    tool = PcbListFootprintsTool()
    out = await tool.run(
        PcbListFootprintsInput(pcb_path=tmp_path / "nope.kicad_pcb")
    )
    assert out.status == "pcb_not_found"
    assert out.note is not None
    assert out.footprints == []


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wrong.txt"
    f.write_text(_PCB, encoding="utf-8")
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=f))
    assert out.status == "pcb_not_found"
    assert out.footprints == []


@pytest.mark.asyncio
async def test_parse_failure(tmp_path: Path) -> None:
    f = tmp_path / "broken.kicad_pcb"
    f.write_text("(kicad_pcb (unterminated ", encoding="utf-8")
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=f))
    assert out.status == "parse_failed"
    assert out.footprints == []


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    """A ``.kicad_pcb`` file that's actually a schematic under the hood
    reports invalid_schema — we trust suffix for routing but still
    verify the s-expression head before iterating."""
    f = tmp_path / "sch_like.kicad_pcb"
    f.write_text(
        '(kicad_sch (version 20240108) (generator "eeschema"))', encoding="utf-8"
    )
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=f))
    assert out.status == "invalid_schema"
    assert out.footprints == []


# -- edge cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_board(tmp_path: Path) -> None:
    """A parseable .kicad_pcb with zero footprints reports ok + []."""
    body = (
        '(kicad_pcb (version 20240108) (generator "pcbnew") (paper "A4") (layers))'
    )
    f = tmp_path / "empty.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=f))
    assert out.status == "ok"
    assert out.total == 0
    assert out.footprints == []


@pytest.mark.asyncio
async def test_footprint_without_uuid_is_skipped(tmp_path: Path) -> None:
    """Hand-crafted fixture missing a uuid is not a parse error — KiCAD
    always writes one, but we guard against degenerate inputs by
    dropping the entry silently rather than aborting the listing."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers)
\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(at 100 50 0)
\t\t(property "Reference" "R99" (at 100 48 0))
\t\t(property "Value" "100R" (at 100 52 0))
\t)
)
"""
    f = tmp_path / "no_uuid.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbListFootprintsTool()
    out = await tool.run(PcbListFootprintsInput(pcb_path=f))
    assert out.status == "ok"
    assert out.footprints == []

"""Unit tests for pcb_get_stackup.

Two load-bearing shapes covered:

1. **Explicit stackup present** — the canonical ``(layers ...)``
   roster + full ``(setup (stackup ...))`` with copper thicknesses,
   FR4 core with epsilon_r / loss_tangent, solder mask colors, and
   the copper-finish string. Pins every field we surface.

2. **Layers-only fallback** — no ``(stackup ...)`` subnode. Tool
   still returns ``ok`` with ``has_explicit_stackup=False`` and an
   empty ``stackup`` list. The canonical roster populates regardless.

Error paths match the ``pcb_list_*`` family (missing / wrong-suffix /
parse-failed / invalid-schema). The copper-layer count is computed
from the canonical layer types (``signal``/``power``/``mixed``/
``jumper``) so that test is meaningful even without an explicit
stackup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.pcb_get_stackup import (
    PcbGetStackupInput,
    PcbGetStackupTool,
)

# Standard 2-layer FR4 board with a fully-populated explicit stackup.
# F.Cu / B.Cu are the copper layers (the "count to 2" for the fab);
# everything else is tech / user. The stackup lists all nine physical
# layers top-to-bottom with FR4 dielectric between the copper layers.
_PCB_EXPLICIT = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(32 "B.Adhes" user "B.Adhesive")
\t\t(33 "F.Adhes" user "F.Adhesive")
\t\t(34 "B.Paste" user)
\t\t(35 "F.Paste" user)
\t\t(36 "B.SilkS" user "B.Silkscreen")
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(38 "B.Mask" user)
\t\t(39 "F.Mask" user)
\t\t(40 "Dwgs.User" user "User.Drawings")
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))
\t\t\t(layer "F.Paste" (type "Top Solder Paste"))
\t\t\t(layer "F.Mask" (type "Top Solder Mask") (color "Green") (thickness 0.01))
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (color "Green") (thickness 0.01))
\t\t\t(layer "B.Paste" (type "Bottom Solder Paste"))
\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))
\t\t\t(copper_finish "HASL")
\t\t\t(dielectric_constraints no)
\t\t)
\t)
)
"""


# Same layer roster but no explicit stackup — simulates a board that
# never opened the stackup editor. Real and common.
_PCB_IMPLICIT = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(36 "B.SilkS" user "B.Silkscreen")
\t)
\t(setup)
)
"""


def _write(tmp_path: Path, body: str, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text(body, encoding="utf-8")
    return pcb


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = PcbGetStackupTool()
    assert tool.name == "pcb_get_stackup"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths: explicit stackup ----------------------------------------


@pytest.mark.asyncio
async def test_explicit_stackup_status_and_flags(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.has_explicit_stackup is True
    assert out.copper_finish == "HASL"
    assert out.dielectric_constraints is False


@pytest.mark.asyncio
async def test_explicit_stackup_layer_counts(tmp_path: Path) -> None:
    """Copper-layer count is the 'how many layers does this board have'
    number you quote to a fab. Only ``signal``/``power``/``mixed``/
    ``jumper`` types count — tech/user layers don't."""
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    assert out.copper_layer_count == 2  # F.Cu + B.Cu
    assert out.total_layer_count == 11  # Everything in (layers ...)


@pytest.mark.asyncio
async def test_explicit_stackup_order_preserved(tmp_path: Path) -> None:
    """Stackup is top-to-bottom in the file and must stay that way —
    the ordering is the physical build-up order. A field tool that
    reversed it would show a board built B.Cu-up to F.Cu."""
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    names = [sl.name for sl in out.stackup]
    assert names == [
        "F.SilkS",
        "F.Paste",
        "F.Mask",
        "F.Cu",
        "dielectric 1",
        "B.Cu",
        "B.Mask",
        "B.Paste",
        "B.SilkS",
    ]


@pytest.mark.asyncio
async def test_explicit_stackup_copper_fields(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    f_cu = next(sl for sl in out.stackup if sl.name == "F.Cu")
    assert f_cu.type == "copper"
    assert f_cu.thickness == 0.035
    # Copper has no material / epsilon / color.
    assert f_cu.material is None
    assert f_cu.epsilon_r is None
    assert f_cu.color is None


@pytest.mark.asyncio
async def test_explicit_stackup_dielectric_fields(tmp_path: Path) -> None:
    """Dielectric entries carry the impedance-modeling fields. Pin
    them all so a regression that dropped epsilon_r would surface
    in controlled-impedance design reviews."""
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    core = next(sl for sl in out.stackup if sl.name == "dielectric 1")
    assert core.type == "core"
    assert core.thickness == 1.51
    assert core.material == "FR4"
    assert core.epsilon_r == 4.5
    assert core.loss_tangent == 0.02


@pytest.mark.asyncio
async def test_explicit_stackup_mask_color(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    f_mask = next(sl for sl in out.stackup if sl.name == "F.Mask")
    assert f_mask.type == "Top Solder Mask"
    assert f_mask.color == "Green"
    assert f_mask.thickness == 0.01


@pytest.mark.asyncio
async def test_explicit_stackup_total_thickness(tmp_path: Path) -> None:
    """Total = sum of every stackup layer's thickness. For this
    fixture: mask(0.01) + cu(0.035) + core(1.51) + cu(0.035) +
    mask(0.01) = 1.60 mm. The silk/paste entries have no thickness
    declared so they add 0."""
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    assert out.total_thickness_mm == pytest.approx(1.6, abs=1e-9)


@pytest.mark.asyncio
async def test_canonical_layers_user_name(tmp_path: Path) -> None:
    """User-rename strings on tech layers show up in ``layers[i].user_name``.
    This is the 'Schematic → PCB' name mapping Board Setup surfaces."""
    pcb = _write(tmp_path, _PCB_EXPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    by_name = {layer.name: layer for layer in out.layers}
    assert by_name["B.SilkS"].user_name == "B.Silkscreen"
    assert by_name["F.SilkS"].user_name == "F.Silkscreen"
    # No rename → None.
    assert by_name["F.Cu"].user_name is None


# -- happy paths: implicit stackup ----------------------------------------


@pytest.mark.asyncio
async def test_implicit_stackup_is_ok_but_empty(tmp_path: Path) -> None:
    """A board with no ``(stackup ...)`` subnode is a valid config —
    KiCAD uses defaults. Status stays ``ok`` but ``stackup`` is empty
    and the flag is False so callers can distinguish from a board
    whose stackup literally has zero layers (impossible, but the
    flag is cheaper than a heuristic)."""
    pcb = _write(tmp_path, _PCB_IMPLICIT)
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.has_explicit_stackup is False
    assert out.stackup == []
    assert out.total_thickness_mm == 0.0
    assert out.copper_finish is None
    assert out.dielectric_constraints is None
    # But layers are still populated.
    assert out.copper_layer_count == 2
    assert out.total_layer_count == 4


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=tmp_path / "nope.kicad_pcb"))
    assert out.status == "pcb_not_found"
    assert out.layers == []
    assert out.stackup == []


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wrong.txt"
    f.write_text(_PCB_EXPLICIT, encoding="utf-8")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.status == "pcb_not_found"


@pytest.mark.asyncio
async def test_parse_failure(tmp_path: Path) -> None:
    f = tmp_path / "broken.kicad_pcb"
    f.write_text("(kicad_pcb (unterminated ", encoding="utf-8")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    f = tmp_path / "sch_like.kicad_pcb"
    f.write_text(
        '(kicad_sch (version 20240108) (generator "eeschema"))', encoding="utf-8"
    )
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.status == "invalid_schema"


# -- edge cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_4_layer_board_counts_copper_correctly(tmp_path: Path) -> None:
    """A 4-layer board has F.Cu, In1.Cu, In2.Cu, B.Cu — all ``signal``.
    Count must be 4, not 2. Regression guard for a 'hard-coded 2'
    mistake."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(1 "In1.Cu" signal)
\t\t(2 "In2.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(37 "F.SilkS" user)
\t)
\t(setup)
)
"""
    f = _write(tmp_path, body, "four_layer.kicad_pcb")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.copper_layer_count == 4
    assert out.total_layer_count == 5


@pytest.mark.asyncio
async def test_power_and_mixed_layer_types_count_as_copper(tmp_path: Path) -> None:
    """``power`` and ``mixed`` layer types are copper just as much as
    ``signal`` is — a power plane counts toward the fab's layer
    count."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(1 "In1.Cu" power)
\t\t(2 "In2.Cu" mixed)
\t\t(31 "B.Cu" signal)
\t)
\t(setup)
)
"""
    f = _write(tmp_path, body, "mixed_types.kicad_pcb")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.copper_layer_count == 4


@pytest.mark.asyncio
async def test_dielectric_constraints_yes_is_true(tmp_path: Path) -> None:
    """Impedance-controlled boards set ``dielectric_constraints yes`` —
    verify the bool flips on."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))
\t(setup
\t\t(stackup
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 0.8) (material "FR4") (epsilon_r 4.3))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(dielectric_constraints yes)
\t\t)
\t)
)
"""
    f = _write(tmp_path, body, "impedance.kicad_pcb")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.dielectric_constraints is True


@pytest.mark.asyncio
async def test_missing_layers_section(tmp_path: Path) -> None:
    """A .kicad_pcb without any ``(layers ...)`` section is degenerate
    but parseable — tool returns ok with empty layers list rather
    than raising. Lets test fixtures stay minimal."""
    body = (
        '(kicad_pcb (version 20240108) (generator "pcbnew") (paper "A4"))'
    )
    f = tmp_path / "minimal.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbGetStackupTool()
    out = await tool.run(PcbGetStackupInput(pcb_path=f))
    assert out.status == "ok"
    assert out.layers == []
    assert out.copper_layer_count == 0

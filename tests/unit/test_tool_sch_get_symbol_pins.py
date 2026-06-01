"""Unit tests for ``sch_get_symbol_pins`` — pin position resolver.

Covers the core contract: given a placed symbol's reference, return
absolute pin coordinates by combining the instance's anchor + rotation
with the lib_symbol's pin offsets.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kimcp.tools.builtin.sch_get_symbol_pins import (
    PinInfo,
    SchGetSymbolPinsInput,
    SchGetSymbolPinsTool,
    _extract_pins_with_positions,
    _find_instance_by_reference,
)
from kimcp.sexpr.parser import parse as sexpr_parse


# -- fixture helpers -------------------------------------------------------


def _make_schematic(tmp_path: Path, content: str) -> Path:
    """Write a minimal .kicad_sch and return the path."""
    p = tmp_path / "test.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


_MINIMAL_SCH = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "9.0")
  (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
  (lib_symbols
    (symbol "Device:R_Small"
      (pin_numbers hide)
      (symbol "Device:R_Small_0_1"
        (pin passive line (at 0 1.27 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 0 -1.27 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
    )
  )
  (symbol (lib_id "Device:R_Small")
    (at 100 50)
    (unit 1)
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (property "Reference" "R1"
      (at 100 47 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "10k"
      (at 100 53 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 100 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Datasheet" ""
      (at 100 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (pin "1" (uuid "11111111-1111-1111-1111-111111111111"))
    (pin "2" (uuid "22222222-2222-2222-2222-222222222222"))
  )
)
"""


_ROTATED_SCH = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "9.0")
  (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
  (lib_symbols
    (symbol "Device:R_Small"
      (symbol "Device:R_Small_0_1"
        (pin passive line (at 0 1.27 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 0 -1.27 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
    )
  )
  (symbol (lib_id "Device:R_Small")
    (at 100 50 90)
    (unit 1)
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (property "Reference" "R2"
      (at 100 47 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "4.7k"
      (at 100 53 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 100 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Datasheet" ""
      (at 100 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (pin "1" (uuid "11111111-1111-1111-1111-111111111111"))
    (pin "2" (uuid "22222222-2222-2222-2222-222222222222"))
  )
)
"""


# -- basic contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_returns_pin_positions(tmp_path: Path) -> None:
    """Pins of an unrotated resistor at (100,50) should be at known offsets."""
    sch = _make_schematic(tmp_path, _MINIMAL_SCH)
    tool = SchGetSymbolPinsTool()
    out = await tool.run(SchGetSymbolPinsInput(sch_path=sch, reference="R1"))
    assert out.status == "ok"
    assert out.reference == "R1"
    assert out.lib_id == "Device:R_Small"
    assert out.at_x == 100.0
    assert out.at_y == 50.0
    assert out.total == 2

    # Pin 1 is at local (0, 1.27) → absolute (100, 51.27) for 0° rotation.
    pin1 = next(p for p in out.pins if p.number == "1")
    assert pin1.x == pytest.approx(100.0, abs=0.01)
    assert pin1.y == pytest.approx(51.27, abs=0.01)
    assert pin1.electrical_type == "passive"

    # Pin 2 is at local (0, -1.27) → absolute (100, 48.73).
    pin2 = next(p for p in out.pins if p.number == "2")
    assert pin2.x == pytest.approx(100.0, abs=0.01)
    assert pin2.y == pytest.approx(48.73, abs=0.01)


@pytest.mark.asyncio
async def test_rotated_symbol_transforms_pins(tmp_path: Path) -> None:
    """A 90° rotated resistor should rotate pin offsets accordingly."""
    sch = _make_schematic(tmp_path, _ROTATED_SCH)
    tool = SchGetSymbolPinsTool()
    out = await tool.run(SchGetSymbolPinsInput(sch_path=sch, reference="R2"))
    assert out.status == "ok"
    assert out.angle == 90.0
    assert out.total == 2

    # Pin 1 local (0, 1.27) rotated 90°:
    # abs_x = 100 + 0*cos(90) - 1.27*sin(90) = 100 - 1.27 = 98.73
    # abs_y = 50  + 0*sin(90) + 1.27*cos(90) = 50 + 0     = 50.0
    pin1 = next(p for p in out.pins if p.number == "1")
    assert pin1.x == pytest.approx(98.73, abs=0.01)
    assert pin1.y == pytest.approx(50.0, abs=0.01)


# -- error cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found(tmp_path: Path) -> None:
    tool = SchGetSymbolPinsTool()
    out = await tool.run(
        SchGetSymbolPinsInput(
            sch_path=tmp_path / "missing.kicad_sch", reference="R1"
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_extension(tmp_path: Path) -> None:
    p = tmp_path / "test.kicad_pcb"
    p.write_text("(kicad_pcb)")
    tool = SchGetSymbolPinsTool()
    out = await tool.run(
        SchGetSymbolPinsInput(sch_path=p, reference="R1")
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_symbol_not_found(tmp_path: Path) -> None:
    sch = _make_schematic(tmp_path, _MINIMAL_SCH)
    tool = SchGetSymbolPinsTool()
    out = await tool.run(SchGetSymbolPinsInput(sch_path=sch, reference="U99"))
    assert out.status == "symbol_not_found"
    assert "U99" in (out.note or "")


@pytest.mark.asyncio
async def test_parse_cache_injection() -> None:
    """set_parse_cache doesn't crash when called."""
    tool = SchGetSymbolPinsTool()
    from kimcp.sexpr.cache import ParseCache

    cache = ParseCache()
    tool.set_parse_cache(cache)
    assert tool._parse_cache is cache


# -- pin extraction helper -------------------------------------------------


def test_extract_pins_no_rotation() -> None:
    """Direct test of the pin extraction + transformation function."""
    lib_sym_text = """\
(symbol "Test:IC"
  (symbol "Test:IC_0_1"
    (pin input line (at -5.08 2.54 0) (length 2.54)
      (name "VIN" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27))))
    )
    (pin output line (at 5.08 2.54 180) (length 2.54)
      (name "VOUT" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27))))
    )
    (pin power_in line (at 0 -5.08 90) (length 2.54)
      (name "GND" (effects (font (size 1.27 1.27))))
      (number "3" (effects (font (size 1.27 1.27))))
    )
  )
)
"""
    lib_sym_node = sexpr_parse(lib_sym_text.encode())
    pins = _extract_pins_with_positions(
        lib_sym_node,
        anchor_x=150.0,
        anchor_y=80.0,
        inst_angle_deg=0.0,
        mirror_x=False,
        mirror_y=False,
    )
    assert len(pins) == 3

    pin1 = next(p for p in pins if p.number == "1")
    assert pin1.name == "VIN"
    assert pin1.x == pytest.approx(144.92, abs=0.01)  # 150 + (-5.08)
    assert pin1.y == pytest.approx(82.54, abs=0.01)    # 80 + 2.54
    assert pin1.electrical_type == "input"

    pin2 = next(p for p in pins if p.number == "2")
    assert pin2.name == "VOUT"
    assert pin2.x == pytest.approx(155.08, abs=0.01)  # 150 + 5.08
    assert pin2.electrical_type == "output"

    pin3 = next(p for p in pins if p.number == "3")
    assert pin3.name == "GND"
    assert pin3.y == pytest.approx(74.92, abs=0.01)   # 80 + (-5.08)
    assert pin3.electrical_type == "power_in"

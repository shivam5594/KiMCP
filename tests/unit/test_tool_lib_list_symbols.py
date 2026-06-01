"""Unit tests for lib_list_symbols (M30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.lib_list_symbols import (
    LibListSymbolsInput,
    LibListSymbolsTool,
)

# Three symbols: R_Small (single unit, 2 pins), C_Small (single unit,
# 2 pins), and LM358 (two units, 5 pins each). Covers:
#
#   - nested unit-body sub-symbols (pin counting across units)
#   - footprint filters (R_*, Resistor_SMD:*)
#   - empty description + keywords for C_Small
#   - datasheet '~' convention (no datasheet) on R_Small
_LIB = """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(generator_version "9.0")
\t(symbol "R_Small"
\t\t(pin_numbers hide)
\t\t(pin_names (offset 0.254))
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "R_Small" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Description" "Resistor, small symbol" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "ki_keywords" "R resistor" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "ki_fp_filters" "R_* Resistor_SMD:*" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(symbol "R_Small_0_1"
\t\t\t(rectangle (start -0.5 -1.25) (end 0.5 1.25))
\t\t)
\t\t(symbol "R_Small_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2"))
\t\t)
\t)
\t(symbol "C_Small"
\t\t(pin_numbers hide)
\t\t(pin_names (offset 0.254))
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "C_Small" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(symbol "C_Small_0_1"
\t\t\t(polyline (pts (xy -2 -0.3) (xy 2 -0.3)))
\t\t)
\t\t(symbol "C_Small_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2"))
\t\t)
\t)
\t(symbol "LM358"
\t\t(pin_numbers hide)
\t\t(pin_names (offset 0.254))
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "LM358" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "Package_DIP:DIP-8_W7.62mm" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Datasheet" "http://www.ti.com/lit/ds/symlink/lm358.pdf" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Description" "Dual low-power operational amplifier" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "ki_keywords" "dual opamp" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "ki_fp_filters" "DIP*8* SOIC*8* TSSOP*8*" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(symbol "LM358_0_1"
\t\t\t(rectangle (start -7.62 7.62) (end 7.62 -7.62))
\t\t)
\t\t(symbol "LM358_1_1"
\t\t\t(pin input line (at -10.16 2.54 0) (length 2.54) (name "IN+") (number "3"))
\t\t\t(pin input line (at -10.16 -2.54 0) (length 2.54) (name "IN-") (number "2"))
\t\t\t(pin output line (at 10.16 0 180) (length 2.54) (name "OUT") (number "1"))
\t\t\t(pin power_in line (at 0 10.16 270) (length 2.54) (name "V+") (number "8"))
\t\t\t(pin power_in line (at 0 -10.16 90) (length 2.54) (name "V-") (number "4"))
\t\t)
\t\t(symbol "LM358_2_1"
\t\t\t(rectangle (start -7.62 7.62) (end 7.62 -7.62))
\t\t)
\t\t(symbol "LM358_2_2"
\t\t\t(pin input line (at -10.16 2.54 0) (length 2.54) (name "IN+") (number "5"))
\t\t\t(pin input line (at -10.16 -2.54 0) (length 2.54) (name "IN-") (number "6"))
\t\t\t(pin output line (at 10.16 0 180) (length 2.54) (name "OUT") (number "7"))
\t\t)
\t)
)
"""


def _write(tmp_path: Path, body: str = _LIB) -> Path:
    lib = tmp_path / "Test.kicad_sym"
    lib.write_text(body, encoding="utf-8")
    return lib


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibListSymbolsTool()
    assert tool.name == "lib_list_symbols"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_symbols(tmp_path: Path) -> None:
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.total == 3
    names = [s.name for s in out.symbols]
    assert names == ["R_Small", "C_Small", "LM358"]


@pytest.mark.asyncio
async def test_r_small_fields(tmp_path: Path) -> None:
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    r = next(s for s in out.symbols if s.name == "R_Small")
    assert r.reference == "R"
    assert r.value == "R_Small"
    assert r.description == "Resistor, small symbol"
    assert r.keywords == "R resistor"
    assert r.datasheet == "~"  # KiCAD 'no datasheet' convention
    assert r.footprint_filters == ["R_*", "Resistor_SMD:*"]
    assert r.pin_count == 2


@pytest.mark.asyncio
async def test_c_small_empty_description(tmp_path: Path) -> None:
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    c = next(s for s in out.symbols if s.name == "C_Small")
    assert c.description == ""
    assert c.keywords == ""
    assert c.footprint_filters == []
    assert c.datasheet == ""
    assert c.pin_count == 2


@pytest.mark.asyncio
async def test_multi_unit_symbol_counts_all_pins(tmp_path: Path) -> None:
    """LM358 has 5 pins in unit 1 (LM358_1_1) + 3 in unit 2 (LM358_2_2) = 8."""
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    u = next(s for s in out.symbols if s.name == "LM358")
    assert u.reference == "U"
    assert u.pin_count == 8
    assert u.footprint_filters == ["DIP*8*", "SOIC*8*", "TSSOP*8*"]
    assert "lm358.pdf" in u.datasheet


# -- filters ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_name_contains_case_insensitive(tmp_path: Path) -> None:
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(
        LibListSymbolsInput(lib_path=lib, name_contains="small")
    )
    # R_Small + C_Small, case-insensitive on the fixture's "_Small".
    assert out.total == 2
    assert {s.name for s in out.symbols} == {"R_Small", "C_Small"}


@pytest.mark.asyncio
async def test_filter_name_contains_no_match(tmp_path: Path) -> None:
    lib = _write(tmp_path)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib, name_contains="ZZZ"))
    assert out.status == "ok"
    assert out.total == 0
    assert out.symbols == []


# -- empty lib -------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_lib(tmp_path: Path) -> None:
    empty = """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
)
"""
    lib = _write(tmp_path, empty)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.total == 0


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_lib_not_found_missing(tmp_path: Path) -> None:
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=tmp_path / "nope.kicad_sym"))
    assert out.status == "lib_not_found"
    assert out.lib_path is None


@pytest.mark.asyncio
async def test_lib_not_found_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wrong.kicad_sch"
    f.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=f))
    assert out.status == "lib_not_found"
    assert out.note is not None and ".kicad_sym" in out.note


@pytest.mark.asyncio
async def test_lib_not_found_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sym"
    d.mkdir()
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=d))
    assert out.status == "lib_not_found"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    lib = tmp_path / "broken.kicad_sym"
    lib.write_text("(kicad_symbol_lib (oops", encoding="utf-8")
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    lib = tmp_path / "wrong.kicad_sym"
    lib.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    assert out.status == "invalid_schema"


# -- defensive parsing -----------------------------------------------------


@pytest.mark.asyncio
async def test_symbol_without_name_is_skipped(tmp_path: Path) -> None:
    """A (symbol) with no positional name atom is malformed."""
    broken = """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(symbol)
\t(symbol "OK"
\t\t(property "Reference" "X" (at 0 0 0))
\t)
)
"""
    lib = _write(tmp_path, broken)
    tool = LibListSymbolsTool()
    out = await tool.run(LibListSymbolsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.total == 1
    assert out.symbols[0].name == "OK"

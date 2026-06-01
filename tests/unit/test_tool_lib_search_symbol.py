"""Unit tests for lib_search_symbol (M31)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.lib_search_symbol import (
    LibSearchSymbolInput,
    LibSearchSymbolTool,
)


def _lib_a() -> str:
    """A passives-ish library."""
    return """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(symbol "R_Small"
\t\t(property "Reference" "R" (at 0 0 0))
\t\t(property "Value" "R_Small" (at 0 0 0))
\t\t(property "Description" "Resistor, small symbol" (at 0 0 0))
\t\t(property "ki_keywords" "R resistor" (at 0 0 0))
\t)
\t(symbol "C_Small"
\t\t(property "Reference" "C" (at 0 0 0))
\t\t(property "Value" "C_Small" (at 0 0 0))
\t\t(property "Description" "Capacitor, small symbol" (at 0 0 0))
\t\t(property "ki_keywords" "C capacitor" (at 0 0 0))
\t)
)
"""


def _lib_b() -> str:
    """An op-amp library with two entries."""
    return """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(symbol "LM358"
\t\t(property "Reference" "U" (at 0 0 0))
\t\t(property "Value" "LM358" (at 0 0 0))
\t\t(property "Description" "Dual low-power operational amplifier" (at 0 0 0))
\t\t(property "ki_keywords" "dual opamp" (at 0 0 0))
\t\t(property "ki_fp_filters" "DIP*8* SOIC*8*" (at 0 0 0))
\t)
\t(symbol "TL072"
\t\t(property "Reference" "U" (at 0 0 0))
\t\t(property "Value" "TL072" (at 0 0 0))
\t\t(property "Description" "Dual JFET operational amplifier" (at 0 0 0))
\t\t(property "ki_keywords" "dual opamp jfet" (at 0 0 0))
\t)
)
"""


def _write(tmp_path: Path, filename: str, body: str) -> Path:
    p = tmp_path / filename
    p.write_text(body, encoding="utf-8")
    return p


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibSearchSymbolTool()
    assert tool.name == "lib_search_symbol"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_single_lib_single_term(tmp_path: Path) -> None:
    lib = _write(tmp_path, "a.kicad_sym", _lib_a())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="resistor")
    )
    assert out.status == "ok"
    assert out.total == 1
    assert out.results[0].entry.name == "R_Small"
    assert out.results[0].lib_path == str(lib)
    assert out.results[0].score == 1


@pytest.mark.asyncio
async def test_and_semantics_all_terms_required(tmp_path: Path) -> None:
    lib = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    # "dual opamp" → both entries. "dual opamp jfet" → only TL072.
    dual_opamp = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="dual opamp")
    )
    assert dual_opamp.total == 2
    dual_opamp_jfet = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="dual opamp jfet")
    )
    assert dual_opamp_jfet.total == 1
    assert dual_opamp_jfet.results[0].entry.name == "TL072"


@pytest.mark.asyncio
async def test_case_insensitive(tmp_path: Path) -> None:
    lib = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    out = await tool.run(LibSearchSymbolInput(lib_paths=[lib], query="LM358"))
    assert out.total == 1
    # Upper + lower both match.
    lower = await tool.run(LibSearchSymbolInput(lib_paths=[lib], query="lm358"))
    assert lower.total == 1


@pytest.mark.asyncio
async def test_score_is_term_count(tmp_path: Path) -> None:
    """With three matching terms the score equals 3."""
    lib = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="dual opamp jfet")
    )
    assert out.results[0].score == 3


@pytest.mark.asyncio
async def test_sorted_score_desc(tmp_path: Path) -> None:
    """Higher-score results come first."""
    lib = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    # Only TL072 has all 3 terms ("dual opamp jfet"). A broader query
    # ("dual opamp") returns both, tied at 2.
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="dual opamp")
    )
    assert out.total == 2
    # All scores equal → deterministic alphabetical (LM358 < TL072).
    assert [r.entry.name for r in out.results] == ["LM358", "TL072"]


@pytest.mark.asyncio
async def test_multi_lib(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.kicad_sym", _lib_a())
    b = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[a, b], query="dual")
    )
    # Only the op-amp entries match.
    assert out.total == 2
    assert {r.lib_path for r in out.results} == {str(b)}
    assert len(out.libs_scanned) == 2
    assert set(out.libs_scanned) == {str(a), str(b)}


@pytest.mark.asyncio
async def test_directory_scan(tmp_path: Path) -> None:
    """Passing a directory expands to every .kicad_sym in it (non-recursive)."""
    lib_dir = tmp_path / "libs"
    lib_dir.mkdir()
    _write(lib_dir, "a.kicad_sym", _lib_a())
    _write(lib_dir, "b.kicad_sym", _lib_b())
    # Nested lib — should be ignored because we don't recurse.
    nested = lib_dir / "nested"
    nested.mkdir()
    _write(nested, "c.kicad_sym", _lib_b())

    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib_dir], query="dual")
    )
    # Two results from b.kicad_sym only — nested/c.kicad_sym not scanned.
    assert out.total == 2
    assert len(out.libs_scanned) == 2  # a + b, not nested/c


@pytest.mark.asyncio
async def test_max_results_truncates(tmp_path: Path) -> None:
    lib = _write(tmp_path, "b.kicad_sym", _lib_b())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="dual", max_results=1)
    )
    assert out.total == 1
    assert out.total_before_truncate == 2


@pytest.mark.asyncio
async def test_no_matches(tmp_path: Path) -> None:
    lib = _write(tmp_path, "a.kicad_sym", _lib_a())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib], query="microcontroller")
    )
    assert out.status == "ok"
    assert out.total == 0
    assert out.total_before_truncate == 0


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_empty_query(tmp_path: Path) -> None:
    lib = _write(tmp_path, "a.kicad_sym", _lib_a())
    tool = LibSearchSymbolTool()
    out = await tool.run(LibSearchSymbolInput(lib_paths=[lib], query="   "))
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_empty_lib_paths(tmp_path: Path) -> None:
    tool = LibSearchSymbolTool()
    out = await tool.run(LibSearchSymbolInput(lib_paths=[], query="r"))
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_no_libs_found_missing_paths(tmp_path: Path) -> None:
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(
            lib_paths=[tmp_path / "does_not_exist.kicad_sym"],
            query="r",
        )
    )
    assert out.status == "no_libs_found"
    assert len(out.parse_errors) == 1


@pytest.mark.asyncio
async def test_parse_errors_do_not_abort(tmp_path: Path) -> None:
    """One broken lib shouldn't hide results from a working one."""
    good = _write(tmp_path, "a.kicad_sym", _lib_a())
    bad = tmp_path / "broken.kicad_sym"
    bad.write_text("(kicad_symbol_lib (oops", encoding="utf-8")
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[good, bad], query="resistor")
    )
    assert out.status == "ok"
    assert out.total == 1
    assert len(out.parse_errors) == 1
    assert "broken.kicad_sym" in out.parse_errors[0]


@pytest.mark.asyncio
async def test_wrong_top_head_records_parse_error(tmp_path: Path) -> None:
    wrong = _write(
        tmp_path,
        "wrong.kicad_sym",
        "(kicad_sch (version 20240108))\n",
    )
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[wrong], query="r")
    )
    assert out.status == "ok"  # no matches, but search ran
    assert any("kicad_symbol_lib" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_wrong_suffix_recorded(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_sch"
    f.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")
    tool = LibSearchSymbolTool()
    out = await tool.run(LibSearchSymbolInput(lib_paths=[f], query="r"))
    assert out.status == "no_libs_found"
    assert any(".kicad_sym" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_duplicate_paths_deduped(tmp_path: Path) -> None:
    """Passing the same lib twice (directly + via its dir) scans once."""
    lib_dir = tmp_path / "libs"
    lib_dir.mkdir()
    lib = _write(lib_dir, "a.kicad_sym", _lib_a())
    tool = LibSearchSymbolTool()
    out = await tool.run(
        LibSearchSymbolInput(lib_paths=[lib, lib_dir], query="resistor")
    )
    # Exactly one match, not two — dedup is on resolved path.
    assert out.total == 1
    assert out.libs_scanned.count(str(lib)) == 1

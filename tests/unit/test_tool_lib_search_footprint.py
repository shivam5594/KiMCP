"""Unit tests for lib_search_footprint.

Mirrors the lib_search_symbol test matrix one-for-one on the
footprint side. Key difference: footprint libraries are directories
of ``.kicad_mod`` files, so ``lib_paths`` accepts either directory
paths (walked non-recursively) or individual ``.kicad_mod`` files.

Fixture bodies are the same three footprints used by
``test_tool_lib_list_footprints`` — R_0603 (SMD, 2 pads),
SOIC-8 (SMD + exclude_from_pos_files, 8 pads, Description-property
fallback), PinHeader_1x04 (through_hole, 4 pads) — so the AND /
scoring / case semantics stay grounded in realistic footprint
shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.lib_search_footprint import (
    LibSearchFootprintInput,
    LibSearchFootprintTool,
)

_R0603 = """\
(footprint "R_0603_1608Metric"
\t(version 20240108)
\t(generator "pcbnew")
\t(layer "F.Cu")
\t(descr "Resistor SMD 0603 (1.6mm x 0.8mm)")
\t(tags "resistor SMD 0603")
\t(property "Reference" "REF**" (at 0 0 0))
\t(property "Value" "R_0603_1608Metric" (at 0 0 0))
\t(attr smd)
\t(pad "1" smd roundrect (at -0.825 0) (size 0.8 0.8))
\t(pad "2" smd roundrect (at 0.825 0) (size 0.8 0.8))
)
"""

_SOIC8 = """\
(footprint "SOIC-8_3.9x4.9mm_P1.27mm"
\t(version 20240108)
\t(generator "pcbnew")
\t(layer "F.Cu")
\t(tags "SOIC SO SMT")
\t(property "Reference" "REF**" (at 0 0 0))
\t(property "Value" "SOIC-8_3.9x4.9mm_P1.27mm" (at 0 0 0))
\t(property "Description" "SOIC 8-pin 3.9x4.9mm pitch 1.27mm" (at 0 0 0))
\t(attr smd exclude_from_pos_files)
\t(pad "1" smd (at -1 -1))
\t(pad "2" smd (at 0 -1))
\t(pad "3" smd (at 1 -1))
\t(pad "4" smd (at 2 -1))
\t(pad "5" smd (at 2 1))
\t(pad "6" smd (at 1 1))
\t(pad "7" smd (at 0 1))
\t(pad "8" smd (at -1 1))
)
"""

_PINHDR = """\
(footprint "PinHeader_1x04_P2.54mm_Vertical"
\t(version 20240108)
\t(generator "pcbnew")
\t(layer "F.Cu")
\t(descr "Through-hole straight pin header, 1x04")
\t(tags "Through hole pin header THT 1x04 2.54mm")
\t(property "Reference" "REF**" (at 0 0 0))
\t(property "Value" "PinHeader_1x04" (at 0 0 0))
\t(attr through_hole)
\t(pad "1" thru_hole (at 0 0))
\t(pad "2" thru_hole (at 2.54 0))
\t(pad "3" thru_hole (at 5.08 0))
\t(pad "4" thru_hole (at 7.62 0))
)
"""


def _make_passives_lib(tmp_path: Path) -> Path:
    """Passives-ish library: R_0603 + SOIC-8."""
    lib = tmp_path / "Passives.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text(
        _SOIC8, encoding="utf-8"
    )
    return lib


def _make_connector_lib(tmp_path: Path) -> Path:
    """Connector library with the pin header."""
    lib = tmp_path / "Connectors.pretty"
    lib.mkdir()
    (lib / "PinHeader_1x04_P2.54mm_Vertical.kicad_mod").write_text(
        _PINHDR, encoding="utf-8"
    )
    return lib


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibSearchFootprintTool()
    assert tool.name == "lib_search_footprint"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_single_lib_single_term(tmp_path: Path) -> None:
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="resistor")
    )
    assert out.status == "ok"
    assert out.total == 1
    assert out.results[0].entry.name == "R_0603_1608Metric"
    assert out.results[0].lib_dir == str(lib)
    assert out.results[0].score == 1


@pytest.mark.asyncio
async def test_and_semantics_all_terms_required(tmp_path: Path) -> None:
    """Every query term must land somewhere in the blob."""
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    # "smd" alone → both fixtures (both have attr smd and "SMD" in tags/name).
    smd_only = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="smd")
    )
    assert smd_only.total == 2
    # "smd 0603" → only R_0603 (SOIC doesn't mention 0603).
    narrowed = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="smd 0603")
    )
    assert narrowed.total == 1
    assert narrowed.results[0].entry.name == "R_0603_1608Metric"


@pytest.mark.asyncio
async def test_case_insensitive(tmp_path: Path) -> None:
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    # "SOIC" in tags is upper-case in the fixture; "soic" must match.
    upper = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="SOIC")
    )
    lower = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="soic")
    )
    assert upper.total == lower.total == 1
    assert upper.results[0].entry.name == lower.results[0].entry.name


@pytest.mark.asyncio
async def test_score_is_term_count(tmp_path: Path) -> None:
    """Three matching terms → score 3."""
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    # R_0603 matches "resistor", "smd", "0603" — three distinct terms
    # across name / tags / descr.
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="resistor smd 0603")
    )
    assert out.total == 1
    assert out.results[0].score == 3


@pytest.mark.asyncio
async def test_sorted_score_desc_then_name(tmp_path: Path) -> None:
    """Ties on score fall back to (lib_dir, entry.name)."""
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    # "smd" hits both with score 1 → alphabetical tiebreak.
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="smd")
    )
    assert out.total == 2
    # R_0603... < SOIC-8... lexicographically.
    assert [r.entry.name for r in out.results] == [
        "R_0603_1608Metric",
        "SOIC-8_3.9x4.9mm_P1.27mm",
    ]


@pytest.mark.asyncio
async def test_multi_lib(tmp_path: Path) -> None:
    passives = _make_passives_lib(tmp_path)
    connectors = _make_connector_lib(tmp_path)
    tool = LibSearchFootprintTool()
    # "through" — PinHeader has "Through-hole" in descr and "Through/THT" in tags.
    out = await tool.run(
        LibSearchFootprintInput(
            lib_paths=[passives, connectors], query="through"
        )
    )
    assert out.total == 1
    assert out.results[0].entry.name == "PinHeader_1x04_P2.54mm_Vertical"
    assert out.results[0].lib_dir == str(connectors)
    # Both libs were scanned even though only one produced a match.
    assert len(out.libs_scanned) == 2
    assert set(out.libs_scanned) == {str(passives), str(connectors)}


@pytest.mark.asyncio
async def test_directory_scan_non_recursive(tmp_path: Path) -> None:
    """Directory expands to its ``*.kicad_mod`` children but doesn't
    descend into sub-pretty dirs."""
    root = tmp_path / "LibRoot.pretty"
    root.mkdir()
    (root / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    # Nested .pretty — should be ignored.
    nested = root / "nested.pretty"
    nested.mkdir()
    (nested / "PinHeader_1x04_P2.54mm_Vertical.kicad_mod").write_text(
        _PINHDR, encoding="utf-8"
    )

    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[root], query="smd")
    )
    # Only R_0603 matches — nested/pin header wasn't scanned.
    assert out.total == 1
    assert out.results[0].entry.name == "R_0603_1608Metric"


@pytest.mark.asyncio
async def test_single_kicad_mod_file_accepted(tmp_path: Path) -> None:
    """Pointing at one ``.kicad_mod`` file searches just that file."""
    lib = _make_passives_lib(tmp_path)
    r0603 = lib / "R_0603_1608Metric.kicad_mod"
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[r0603], query="0603")
    )
    assert out.total == 1
    assert out.results[0].entry.name == "R_0603_1608Metric"
    # lib_dir is the file's parent, not the file itself.
    assert out.results[0].lib_dir == str(lib)
    # libs_scanned records the single file (so the caller sees exactly
    # what was searched).
    assert out.libs_scanned == [str(r0603)]


@pytest.mark.asyncio
async def test_mixed_dir_and_file_inputs(tmp_path: Path) -> None:
    """A dir + a file in another dir compose cleanly."""
    passives = _make_passives_lib(tmp_path)
    connectors = _make_connector_lib(tmp_path)
    pin_file = connectors / "PinHeader_1x04_P2.54mm_Vertical.kicad_mod"
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(
            lib_paths=[passives, pin_file], query="header"
        )
    )
    # Only pin-header matches.
    assert out.total == 1
    assert out.results[0].entry.name == "PinHeader_1x04_P2.54mm_Vertical"


@pytest.mark.asyncio
async def test_max_results_truncates(tmp_path: Path) -> None:
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="smd", max_results=1)
    )
    assert out.total == 1
    assert out.total_before_truncate == 2


@pytest.mark.asyncio
async def test_no_matches(tmp_path: Path) -> None:
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="microcontroller")
    )
    assert out.status == "ok"
    assert out.total == 0
    assert out.total_before_truncate == 0


@pytest.mark.asyncio
async def test_attribute_term_hits_blob(tmp_path: Path) -> None:
    """``through_hole`` is in the ``(attr ...)`` node — the scorer
    must include attributes in its searchable blob."""
    lib = _make_connector_lib(tmp_path)
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="through_hole")
    )
    assert out.total == 1
    assert out.results[0].entry.name == "PinHeader_1x04_P2.54mm_Vertical"


# -- failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_empty_query(tmp_path: Path) -> None:
    lib = _make_passives_lib(tmp_path)
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="   ")
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_empty_lib_paths() -> None:
    tool = LibSearchFootprintTool()
    out = await tool.run(LibSearchFootprintInput(lib_paths=[], query="r"))
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_no_libs_found_missing_paths(tmp_path: Path) -> None:
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(
            lib_paths=[tmp_path / "does_not_exist.pretty"], query="r"
        )
    )
    assert out.status == "no_libs_found"
    assert len(out.parse_errors) == 1
    assert "no such path" in out.parse_errors[0]


@pytest.mark.asyncio
async def test_no_libs_found_empty_pretty(tmp_path: Path) -> None:
    """A .pretty dir with no ``.kicad_mod`` files resolves to zero
    pairs, so the search can't run."""
    empty = tmp_path / "Empty.pretty"
    empty.mkdir()
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[empty], query="r")
    )
    assert out.status == "no_libs_found"
    assert any("no .kicad_mod" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_parse_errors_do_not_abort(tmp_path: Path) -> None:
    """One broken footprint shouldn't hide matches from a working one."""
    lib = tmp_path / "Mixed.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "broken.kicad_mod").write_text(
        "(footprint (unterminated", encoding="utf-8"
    )
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="0603")
    )
    assert out.status == "ok"
    assert out.total == 1
    assert any("broken.kicad_mod" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_wrong_top_head_records_parse_error(tmp_path: Path) -> None:
    """A ``.kicad_mod`` file with a non-footprint top-head surfaces in
    ``parse_errors`` but doesn't abort the search."""
    lib = tmp_path / "Weird.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "imposter.kicad_mod").write_text(
        "(kicad_sch (version 20240108))", encoding="utf-8"
    )
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib], query="0603")
    )
    assert out.status == "ok"
    assert out.total == 1
    assert any("imposter.kicad_mod" in e for e in out.parse_errors)
    assert any("footprint" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_wrong_suffix_single_file_recorded(tmp_path: Path) -> None:
    """A single-file path with the wrong suffix is flagged and skipped."""
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = LibSearchFootprintTool()
    out = await tool.run(LibSearchFootprintInput(lib_paths=[f], query="r"))
    assert out.status == "no_libs_found"
    assert any(".kicad_mod" in e for e in out.parse_errors)


@pytest.mark.asyncio
async def test_duplicate_paths_deduped(tmp_path: Path) -> None:
    """Passing a lib twice (directly as dir + as a child file) searches
    each .kicad_mod exactly once."""
    lib = _make_passives_lib(tmp_path)
    r0603 = lib / "R_0603_1608Metric.kicad_mod"
    tool = LibSearchFootprintTool()
    out = await tool.run(
        LibSearchFootprintInput(lib_paths=[lib, r0603], query="0603")
    )
    # Exactly one match — the dedup key is the resolved file path.
    assert out.total == 1
    assert out.results[0].entry.name == "R_0603_1608Metric"

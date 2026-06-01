"""Unit tests for lib_list_footprints.

Footprint libraries are *directories* of ``.kicad_mod`` files — one
footprint per file — so the input/output shape diverges from
``lib_list_symbols``. The load-bearing bits:

* per-file parsing with per-file error recovery (a single malformed
  ``.kicad_mod`` should NOT tank the whole listing);
* filter composition across name / tag / description;
* attribute + pad-count extraction from the stable ``.kicad_mod``
  schema;
* sort determinism (by name for entries, by file for skipped).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.lib_list_footprints import (
    LibListFootprintsInput,
    LibListFootprintsTool,
)

# Three footprints plus one intentionally broken file. Minimal but
# each exercises a different field path:
#   R_0603 — through (descr ...), tags has "resistor SMD 0603",
#            attr is "smd", 2 pads.
#   SOIC_8 — descr via (Description) property fallback, attr is
#            "smd", 8 pads. Tags mention "SOIC".
#   PinHeader_1x04 — descr via (descr ...), attr is
#            "through_hole", 4 pads.
#   broken.kicad_mod — unterminated paren, should land in skipped.
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


def _make_pretty(tmp_path: Path) -> Path:
    """Create a .pretty library with three footprints + one broken file."""
    lib = tmp_path / "Stuff.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text(
        _SOIC8, encoding="utf-8"
    )
    (lib / "PinHeader_1x04_P2.54mm_Vertical.kicad_mod").write_text(
        _PINHDR, encoding="utf-8"
    )
    (lib / "broken.kicad_mod").write_text(
        "(footprint (unterminated", encoding="utf-8"
    )
    return lib


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibListFootprintsTool()
    assert tool.name == "lib_list_footprints"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_footprints(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.total == 3
    names = [fp.name for fp in out.footprints]
    # Sorted by name.
    assert names == [
        "PinHeader_1x04_P2.54mm_Vertical",
        "R_0603_1608Metric",
        "SOIC-8_3.9x4.9mm_P1.27mm",
    ]


@pytest.mark.asyncio
async def test_broken_file_surfaces_in_skipped(tmp_path: Path) -> None:
    """A malformed ``.kicad_mod`` doesn't abort the listing — it lands
    in ``skipped`` with a reason. Critical for a library with
    thousands of files where one bad entry shouldn't be fatal."""
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    assert len(out.skipped) == 1
    assert out.skipped[0].file == "broken.kicad_mod"
    assert "parse_failed" in out.skipped[0].reason


@pytest.mark.asyncio
async def test_descr_child_field(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    r0603 = next(fp for fp in out.footprints if fp.name == "R_0603_1608Metric")
    assert r0603.description == "Resistor SMD 0603 (1.6mm x 0.8mm)"
    assert r0603.tags == "resistor SMD 0603"


@pytest.mark.asyncio
async def test_description_property_fallback(tmp_path: Path) -> None:
    """SOIC-8 omits ``(descr ...)`` and uses the Description property
    instead. The tool must fall through to the property."""
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    soic = next(
        fp for fp in out.footprints if fp.name == "SOIC-8_3.9x4.9mm_P1.27mm"
    )
    assert soic.description == "SOIC 8-pin 3.9x4.9mm pitch 1.27mm"


@pytest.mark.asyncio
async def test_attributes_multi_value(tmp_path: Path) -> None:
    """``(attr smd exclude_from_pos_files)`` should surface as a
    two-element list preserving order — both tags are independently
    meaningful to downstream checks."""
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    soic = next(
        fp for fp in out.footprints if fp.name == "SOIC-8_3.9x4.9mm_P1.27mm"
    )
    assert soic.attributes == ["smd", "exclude_from_pos_files"]


@pytest.mark.asyncio
async def test_through_hole_attribute(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    ph = next(
        fp for fp in out.footprints if fp.name.startswith("PinHeader")
    )
    assert ph.attributes == ["through_hole"]


@pytest.mark.asyncio
async def test_pad_counts(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    counts = {fp.name: fp.pad_count for fp in out.footprints}
    assert counts["R_0603_1608Metric"] == 2
    assert counts["SOIC-8_3.9x4.9mm_P1.27mm"] == 8
    assert counts["PinHeader_1x04_P2.54mm_Vertical"] == 4


@pytest.mark.asyncio
async def test_file_field_is_basename(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    r0603 = next(fp for fp in out.footprints if fp.name == "R_0603_1608Metric")
    assert r0603.file == "R_0603_1608Metric.kicad_mod"


# -- filters --------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_contains_filter_case_insensitive(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(
        LibListFootprintsInput(lib_path=lib, name_contains="soic")
    )
    assert out.total == 1
    assert out.footprints[0].name == "SOIC-8_3.9x4.9mm_P1.27mm"


@pytest.mark.asyncio
async def test_tag_contains_filter_case_insensitive(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(
        LibListFootprintsInput(lib_path=lib, tag_contains="tht")
    )
    # Only the pin header has "THT" in its tags.
    assert [fp.name for fp in out.footprints] == [
        "PinHeader_1x04_P2.54mm_Vertical"
    ]


@pytest.mark.asyncio
async def test_description_contains_filter(tmp_path: Path) -> None:
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(
        LibListFootprintsInput(lib_path=lib, description_contains="0603")
    )
    assert [fp.name for fp in out.footprints] == ["R_0603_1608Metric"]


@pytest.mark.asyncio
async def test_filters_compose_with_and(tmp_path: Path) -> None:
    """name_contains="R_" + tag_contains="smd" narrows to the 0603
    resistor — SOIC is also SMD but doesn't match "R_"."""
    lib = _make_pretty(tmp_path)
    tool = LibListFootprintsTool()
    out = await tool.run(
        LibListFootprintsInput(
            lib_path=lib, name_contains="R_", tag_contains="smd"
        )
    )
    assert out.total == 1
    assert out.footprints[0].name == "R_0603_1608Metric"


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_directory(tmp_path: Path) -> None:
    tool = LibListFootprintsTool()
    out = await tool.run(
        LibListFootprintsInput(lib_path=tmp_path / "nope.pretty")
    )
    assert out.status == "lib_not_found"
    assert out.footprints == []


@pytest.mark.asyncio
async def test_file_instead_of_directory(tmp_path: Path) -> None:
    """If the caller passes a regular file (a single .kicad_mod by
    mistake), we report lib_not_found with a clarifying note —
    footprint libraries are directories, not single files."""
    f = tmp_path / "single.kicad_mod"
    f.write_text(_R0603, encoding="utf-8")
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=f))
    assert out.status == "lib_not_found"
    assert out.note is not None and "directory" in out.note


# -- edge cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_directory(tmp_path: Path) -> None:
    """An existing but empty .pretty directory returns ok with no
    entries and no skips."""
    lib = tmp_path / "Empty.pretty"
    lib.mkdir()
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.footprints == []
    assert out.skipped == []


@pytest.mark.asyncio
async def test_non_kicad_mod_files_ignored(tmp_path: Path) -> None:
    """Library directories often hold README files or metadata (e.g.
    ``.kicad_symlibs``). Only ``*.kicad_mod`` is scanned."""
    lib = tmp_path / "Mixed.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "README.md").write_text("# notes", encoding="utf-8")
    (lib / "index.json").write_text("{}", encoding="utf-8")
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    assert out.status == "ok"
    assert [fp.name for fp in out.footprints] == ["R_0603_1608Metric"]
    assert out.skipped == []


@pytest.mark.asyncio
async def test_wrong_top_head_is_skipped_not_failed(tmp_path: Path) -> None:
    """A file with a ``.kicad_mod`` suffix but a schematic top-head
    should land in skipped — the library listing continues."""
    lib = tmp_path / "Weird.pretty"
    lib.mkdir()
    (lib / "R_0603_1608Metric.kicad_mod").write_text(_R0603, encoding="utf-8")
    (lib / "imposter.kicad_mod").write_text(
        '(kicad_sch (version 20240108))', encoding="utf-8"
    )
    tool = LibListFootprintsTool()
    out = await tool.run(LibListFootprintsInput(lib_path=lib))
    assert out.status == "ok"
    assert out.total == 1
    assert out.footprints[0].name == "R_0603_1608Metric"
    assert len(out.skipped) == 1
    assert out.skipped[0].file == "imposter.kicad_mod"
    assert "invalid_schema" in out.skipped[0].reason

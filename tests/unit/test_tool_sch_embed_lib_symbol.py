"""Unit tests for M19 ``sch_embed_lib_symbol``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol
from kimcp.tools.builtin.sch_embed_lib_symbol import (
    SchEmbedLibSymbolInput,
    SchEmbedLibSymbolOutput,
    SchEmbedLibSymbolTool,
    _clone_and_qualify,
    _find_symbol_in_lib,
    _list_lib_symbols,
)

# -- fixtures --------------------------------------------------------------


_SCH_EMPTY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

_SCH_NO_LIB_SYMBOLS = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4"))
"""

_PCB = """\
(kicad_pcb (version 20240108) (generator "pcbnew"))
"""

# Minimal two-symbol library for testing.
_LIB = """\
(kicad_symbol_lib
\t(version 20231120)
\t(generator "kimcp-test")
\t(symbol "R_Test"
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "R"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(property "Value" "R_Test"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(symbol "R_Test_0_1"
\t\t\t(rectangle
\t\t\t\t(start -0.762 2.032)
\t\t\t\t(end 0.762 -2.032)))
\t\t(symbol "R_Test_1_1"
\t\t\t(pin passive line
\t\t\t\t(at 0 2.54 270)
\t\t\t\t(length 0.508)
\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t(pin passive line
\t\t\t\t(at 0 -2.54 90)
\t\t\t\t(length 0.508)
\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))
\t(symbol "C_Test"
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "C"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(property "Value" "C_Test"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(symbol "C_Test_1_1"
\t\t\t(pin passive line
\t\t\t\t(at 0 2.54 270)
\t\t\t\t(length 2.032)
\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t(pin passive line
\t\t\t\t(at 0 -2.54 90)
\t\t\t\t(length 2.032)
\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "2" (effects (font (size 1.27 1.27))))))))
"""


def _write_sch(tmp_path: Path, body: str = _SCH_EMPTY) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _write_lib(tmp_path: Path, name: str = "Device.kicad_sym", body: str = _LIB) -> Path:
    lib = tmp_path / name
    lib.write_text(body, encoding="utf-8")
    return lib


def _tool(snapshot_mode: str = "off") -> SchEmbedLibSymbolTool:
    tool = SchEmbedLibSymbolTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode}}))
    return tool


def _atom_text(node: SList, idx: int) -> str:
    a = node.items[idx]
    assert isinstance(a, SAtom)
    return a.text


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchEmbedLibSymbolTool()
    assert tool.name == "sch_embed_lib_symbol"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight: schematic path --------------------------------------------


@pytest.mark.asyncio
async def test_sch_missing(tmp_path: Path) -> None:
    lib = _write_lib(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(
            sch_path=tmp_path / "nope.kicad_sch",
            lib_path=lib,
            symbol_name="R_Test",
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_is_directory(tmp_path: Path) -> None:
    lib = _write_lib(tmp_path)
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=d, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_wrong_suffix(tmp_path: Path) -> None:
    lib = _write_lib(tmp_path)
    sch = tmp_path / "board.kicad_pcb"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_wrong_top_head(tmp_path: Path) -> None:
    lib = _write_lib(tmp_path)
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_sch_parse_failed(tmp_path: Path) -> None:
    lib = _write_lib(tmp_path)
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "parse_failed"


# -- preflight: library path ----------------------------------------------


@pytest.mark.asyncio
async def test_lib_missing(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(
            sch_path=sch,
            lib_path=tmp_path / "nope.kicad_sym",
            symbol_name="R_Test",
        )
    )
    assert out.status == "lib_not_found"


@pytest.mark.asyncio
async def test_lib_wrong_suffix(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = tmp_path / "lib.txt"
    lib.write_text(_LIB, encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "lib_not_found"


@pytest.mark.asyncio
async def test_lib_wrong_top_head(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = tmp_path / "bad.kicad_sym"
    lib.write_text(_SCH_EMPTY, encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "invalid_lib"


@pytest.mark.asyncio
async def test_lib_parse_failed(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = tmp_path / "broken.kicad_sym"
    lib.write_text("(kicad_symbol_lib (oops", encoding="utf-8")
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "parse_failed"


# -- symbol not found in library ------------------------------------------


@pytest.mark.asyncio
async def test_symbol_not_found(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="NoSuchThing")
    )
    assert out.status == "symbol_not_found"
    assert out.note is not None
    # The note should list available symbols.
    assert "R_Test" in out.note
    assert "C_Test" in out.note


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchEmbedLibSymbolInput(
            sch_path=sch,
            lib_path=lib,
            symbol_name="R_Test",
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.lib_id == "Device:R_Test"
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy path: embedding ------------------------------------------------


@pytest.mark.asyncio
async def test_embed_r_test(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "ok"
    assert out.lib_id == "Device:R_Test"

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    entry = _find_lib_symbol(lib_symbols, "Device:R_Test")
    assert entry is not None
    # Top-level name is qualified.
    assert isinstance(entry.items[1], SAtom)
    assert entry.items[1].text == "Device:R_Test"
    # Sub-symbols keep unqualified names.
    child_symbols = [
        child for child in entry.items
        if isinstance(child, SList) and child.head == "symbol"
    ]
    sub_names = [
        child.items[1].text
        for child in child_symbols
        if len(child.items) >= 2 and isinstance(child.items[1], SAtom)
    ]
    assert "R_Test_0_1" in sub_names
    assert "R_Test_1_1" in sub_names


@pytest.mark.asyncio
async def test_embed_with_custom_lib_prefix(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(
            sch_path=sch,
            lib_path=lib,
            symbol_name="R_Test",
            lib_prefix="MyLib",
        )
    )
    assert out.status == "ok"
    assert out.lib_id == "MyLib:R_Test"

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    assert _find_lib_symbol(lib_symbols, "MyLib:R_Test") is not None


@pytest.mark.asyncio
async def test_embed_creates_lib_symbols_if_absent(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path, _SCH_NO_LIB_SYMBOLS)
    lib = _write_lib(tmp_path)
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert doc.root.find("lib_symbols") is not None
    assert _find_lib_symbol(doc.root.find("lib_symbols"), "Device:R_Test") is not None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_embed_second_symbol_preserves_first(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    tool = _tool()
    await tool.run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    await tool.run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="C_Test")
    )
    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    assert _find_lib_symbol(lib_symbols, "Device:R_Test") is not None
    assert _find_lib_symbol(lib_symbols, "Device:C_Test") is not None


# -- already_embedded (idempotent) -----------------------------------------


@pytest.mark.asyncio
async def test_already_embedded(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    tool = _tool()
    first = await tool.run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert first.status == "ok"
    before = sch.read_bytes()

    second = await tool.run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert second.status == "already_embedded"
    assert second.lib_id == "Device:R_Test"
    # No mutation.
    assert sch.read_bytes() == before


# -- preservation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_top_uuid_unchanged(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    before = SexprDocument.from_path(sch)
    before_uuid = before.root.find("uuid")
    assert before_uuid is not None

    await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    after = SexprDocument.from_path(sch)
    after_uuid = after.root.find("uuid")
    assert after_uuid is not None
    assert _atom_text(before_uuid, 1) == _atom_text(after_uuid, 1)


# -- snapshot modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_off(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await _tool(snapshot_mode="off").run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    out = await SchEmbedLibSymbolTool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "ok"


# -- write failure --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)

    def _boom(self: Any) -> bytes:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(SexprDocument, "serialize", _boom, raising=True)
    out = await _tool().run(
        SchEmbedLibSymbolInput(sch_path=sch, lib_path=lib, symbol_name="R_Test")
    )
    assert out.status == "write_failed"


# -- helper-level tests ---------------------------------------------------


def test_find_symbol_in_lib() -> None:
    doc = SexprDocument.from_path(
        Path(__file__).parent.parent / "fixtures" / "sexpr" / "simple_resistor.kicad_sym"
    )
    assert _find_symbol_in_lib(doc.root, "R_Small") is not None
    assert _find_symbol_in_lib(doc.root, "C_Small") is not None
    assert _find_symbol_in_lib(doc.root, "NoSuch") is None


def test_list_lib_symbols() -> None:
    doc = SexprDocument.from_path(
        Path(__file__).parent.parent / "fixtures" / "sexpr" / "simple_resistor.kicad_sym"
    )
    names = _list_lib_symbols(doc.root)
    assert "R_Small" in names
    assert "C_Small" in names


def test_clone_and_qualify_renames_top_only() -> None:
    doc = SexprDocument.from_path(
        Path(__file__).parent.parent / "fixtures" / "sexpr" / "simple_resistor.kicad_sym"
    )
    lib_entry = _find_symbol_in_lib(doc.root, "R_Small")
    assert lib_entry is not None
    cloned = _clone_and_qualify(lib_entry, "Device:R_Small")
    # Top renamed.
    assert isinstance(cloned.items[1], SAtom)
    assert cloned.items[1].text == "Device:R_Small"
    # Sub-symbols unqualified.
    child_symbols = [
        c for c in cloned.items if isinstance(c, SList) and c.head == "symbol"
    ]
    for child in child_symbols:
        name = child.items[1]
        assert isinstance(name, SAtom)
        assert ":" not in name.text  # still unqualified
    # Original not mutated.
    assert isinstance(lib_entry.items[1], SAtom)
    assert lib_entry.items[1].text == "R_Small"


def test_output_defaults() -> None:
    out = SchEmbedLibSymbolOutput(status="dry_run")
    assert out.sch_path is None
    assert out.lib_id is None
    assert out.note is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchEmbedLibSymbolTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off"}})
    tool.set_config(cfg)
    assert tool._config is cfg

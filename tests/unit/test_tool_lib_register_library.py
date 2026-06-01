"""Unit tests for lib_register_library (M46)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.lib_register_library import (
    LibRegisterLibraryInput,
    LibRegisterLibraryTool,
    _get_table_version,
)


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off"}},
    )


def _find_row(root: SList, nickname: str) -> SList | None:
    """Locate the ``(lib (name "<nickname>") ...)`` row."""
    for child in root.items:
        if not isinstance(child, SList) or child.head != "lib":
            continue
        name_node = child.find("name")
        if name_node is None or len(name_node.items) < 2:
            continue
        atom = name_node.items[1]
        if isinstance(atom, SAtom) and atom.text == nickname:
            return child
    return None


def _row_field(row: SList, field: str) -> str | None:
    """Pull the string value out of ``(<field> "value")``."""
    node = row.find(field)
    if node is None or len(node.items) < 2:
        return None
    val = node.items[1]
    return val.text if isinstance(val, SAtom) else None


def _count_rows(root: SList) -> int:
    return sum(
        1 for child in root.items if isinstance(child, SList) and child.head == "lib"
    )


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibRegisterLibraryTool()
    assert tool.name == "lib_register_library"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstraps_missing_sym_lib_table(tmp_path: Path) -> None:
    """No sym-lib-table on disk → tool creates a fresh v7 header + row."""
    table = tmp_path / "sym-lib-table"
    lib = tmp_path / "Custom.kicad_sym"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Custom",
            library_path=lib,
            description="Project-local custom symbols",
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    assert out.created_table is True
    assert out.overwrote is False
    assert out.nickname == "Custom"
    # KIPRJMOD rewriting kicks in for same-dir library.
    assert out.uri == "${KIPRJMOD}/Custom.kicad_sym"

    doc = SexprDocument.from_path(table)
    assert doc.top_head == "sym_lib_table"
    assert _get_table_version(doc) == 7

    row = _find_row(doc.root, "Custom")
    assert row is not None
    assert _row_field(row, "type") == "KiCad"
    assert _row_field(row, "uri") == "${KIPRJMOD}/Custom.kicad_sym"
    assert _row_field(row, "descr") == "Project-local custom symbols"


@pytest.mark.asyncio
async def test_bootstraps_missing_fp_lib_table(tmp_path: Path) -> None:
    """Footprint variant: top-head ``fp_lib_table`` and .pretty/ URI."""
    table = tmp_path / "fp-lib-table"
    lib = tmp_path / "Custom.pretty"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="footprint",
            nickname="Custom",
            library_path=lib,
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    assert out.created_table is True

    doc = SexprDocument.from_path(table)
    assert doc.top_head == "fp_lib_table"
    assert _get_table_version(doc) == 7
    row = _find_row(doc.root, "Custom")
    assert row is not None
    assert _row_field(row, "uri") == "${KIPRJMOD}/Custom.pretty"


@pytest.mark.asyncio
async def test_appends_to_existing_sym_lib_table(tmp_path: Path) -> None:
    """Second call appends; does not overwrite a prior row."""
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))

    out1 = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="First",
            library_path=tmp_path / "First.kicad_sym",
        )
    )
    assert out1.status == "ok"
    assert out1.created_table is True

    out2 = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Second",
            library_path=tmp_path / "Second.kicad_sym",
        )
    )
    assert out2.status == "ok"
    assert out2.created_table is False
    assert out2.overwrote is False

    doc = SexprDocument.from_path(table)
    assert _count_rows(doc.root) == 2
    assert _find_row(doc.root, "First") is not None
    assert _find_row(doc.root, "Second") is not None


@pytest.mark.asyncio
async def test_absolute_path_when_library_outside_table_dir(tmp_path: Path) -> None:
    """Library outside the lib-table's directory → store absolute path."""
    project = tmp_path / "project"
    project.mkdir()
    table = project / "sym-lib-table"

    # Library lives in a sibling directory — not reachable via KIPRJMOD.
    external = tmp_path / "shared"
    external.mkdir()
    lib = external / "Shared.kicad_sym"

    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Shared",
            library_path=lib,
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    # No ${KIPRJMOD} substitution — fallback to absolute.
    assert out.uri is not None
    assert not out.uri.startswith("${KIPRJMOD}")
    assert out.uri == str(lib.resolve())


@pytest.mark.asyncio
async def test_dry_run_does_not_write(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Decoy",
            library_path=tmp_path / "Decoy.kicad_sym",
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.created_table is False  # dry-run shouldn't touch disk
    assert not table.exists()
    # URI is still computed for the caller's preview.
    assert out.uri == "${KIPRJMOD}/Decoy.kicad_sym"


@pytest.mark.asyncio
async def test_overwrite_replaces_row_in_place(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    # Seed the row.
    await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Dup",
            library_path=tmp_path / "Dup.kicad_sym",
            description="v1",
        )
    )
    # Overwrite with different description.
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Dup",
            library_path=tmp_path / "Dup.kicad_sym",
            description="v2",
            overwrite=True,
        )
    )
    assert out.status == "ok"
    assert out.overwrote is True

    doc = SexprDocument.from_path(table)
    # Only one row — overwrite must not duplicate.
    assert _count_rows(doc.root) == 1
    row = _find_row(doc.root, "Dup")
    assert row is not None
    assert _row_field(row, "descr") == "v2"


@pytest.mark.asyncio
async def test_row_has_five_canonical_children(tmp_path: Path) -> None:
    """KiCAD emits a 5-child row (name/type/uri/options/descr) always."""
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Shape",
            library_path=tmp_path / "Shape.kicad_sym",
        )
    )
    doc = SexprDocument.from_path(table)
    row = _find_row(doc.root, "Shape")
    assert row is not None
    for field in ("name", "type", "uri", "options", "descr"):
        assert row.find(field) is not None, f"row missing ({field} ...)"


# -- conflict handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_nickname_exists_without_overwrite(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Dup",
            library_path=tmp_path / "Dup.kicad_sym",
        )
    )
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Dup",
            library_path=tmp_path / "Other.kicad_sym",
        )
    )
    assert out.status == "nickname_exists"
    assert out.nickname == "Dup"


# -- invalid input --------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_symbol_kind_with_pretty_path(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="Wrong",
            library_path=tmp_path / "Wrong.pretty",
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_footprint_kind_with_kicad_sym_path(tmp_path: Path) -> None:
    table = tmp_path / "fp-lib-table"
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="footprint",
            nickname="Wrong",
            library_path=tmp_path / "Wrong.kicad_sym",
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_table_path_is_directory(tmp_path: Path) -> None:
    table = tmp_path / "not_a_file"
    table.mkdir()
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="X",
            library_path=tmp_path / "X.kicad_sym",
        )
    )
    assert out.status == "invalid_input"


def test_invalid_input_empty_nickname() -> None:
    """Pydantic-level validation — empty nickname raises."""
    with pytest.raises(ValueError):
        LibRegisterLibraryInput(
            table_path=Path("/tmp/sym-lib-table"),
            table_kind="symbol",
            nickname="",
            library_path=Path("/tmp/X.kicad_sym"),
        )


def test_invalid_input_nickname_with_colon() -> None:
    """KiCAD lib_ids use ':' as separator — nickname must not contain it."""
    with pytest.raises(ValueError):
        LibRegisterLibraryInput(
            table_path=Path("/tmp/sym-lib-table"),
            table_kind="symbol",
            nickname="bad:name",
            library_path=Path("/tmp/X.kicad_sym"),
        )


# -- schema errors --------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_schema_wrong_top_head(tmp_path: Path) -> None:
    """Point table_kind=symbol at a file whose top-head is fp_lib_table."""
    table = tmp_path / "sym-lib-table"
    table.write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="X",
            library_path=tmp_path / "X.kicad_sym",
        )
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    table.write_text("(sym_lib_table (oops", encoding="utf-8")
    tool = LibRegisterLibraryTool(_cfg(tmp_path))
    out = await tool.run(
        LibRegisterLibraryInput(
            table_path=table,
            table_kind="symbol",
            nickname="X",
            library_path=tmp_path / "X.kicad_sym",
        )
    )
    assert out.status == "parse_failed"

"""Unit tests for sch_add_sheet (M29)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_sheet import (
    SchAddSheetInput,
    SchAddSheetTool,
    _find_sheet_by_uuid,
)

# Minimal valid parent schematic. No pre-existing sheet so the first
# add test can assert document-order precisely.
_PARENT = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
)
"""


def _write_parent(tmp_path: Path, body: str = _PARENT) -> Path:
    sch = tmp_path / "parent.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__n_u.toml",
        project_local=tmp_path / "__n_p.toml",
        session_overrides={
            "safety": {"snapshot_mode": "off", "grid_snap_mm": None},
            "kicad": {
                "cli_exe": str(tmp_path / "nope-cli"),
                "ipc_socket": str(tmp_path / "no.sock"),
            },
        },
    )


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = SchAddSheetTool()
    assert tool.name == "sch_add_sheet"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_appends_sheet_and_creates_child(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Power",
            sheet_file="power.kicad_sch",
            at_x=100.0,
            at_y=100.0,
        )
    )
    assert out.status == "ok"
    assert out.sheet_uuid is not None
    assert out.sheet_file == "power.kicad_sch"
    assert out.child_created is True
    # Child file exists and parses as a kicad_sch.
    child = tmp_path / "power.kicad_sch"
    assert child.is_file()
    child_doc = SexprDocument.from_path(child)
    assert child_doc.top_head == "kicad_sch"
    # Parent has the sheet node with the expected UUID.
    parent_doc = SexprDocument.from_path(parent)
    sheet = _find_sheet_by_uuid(parent_doc.root, out.sheet_uuid)
    assert sheet is not None


@pytest.mark.asyncio
async def test_sheet_node_shape(tmp_path: Path) -> None:
    """Assert the sheet node carries the key children KiCAD expects."""
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="MCU",
            sheet_file="mcu.kicad_sch",
            at_x=50.0,
            at_y=50.0,
            size_w=80.0,
            size_h=40.0,
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(parent)
    sheet = _find_sheet_by_uuid(doc.root, out.sheet_uuid or "")
    assert sheet is not None

    # (at ...) carries x + y.
    at = sheet.find("at")
    assert at is not None
    assert len(at.items) >= 3
    assert isinstance(at.items[1], SAtom) and at.items[1].text == "50"
    assert isinstance(at.items[2], SAtom) and at.items[2].text == "50"

    # (size W H)
    size = sheet.find("size")
    assert size is not None
    assert isinstance(size.items[1], SAtom) and size.items[1].text == "80"
    assert isinstance(size.items[2], SAtom) and size.items[2].text == "40"

    # (fields_autoplaced yes)
    fap = sheet.find("fields_autoplaced")
    assert fap is not None
    assert isinstance(fap.items[1], SAtom) and fap.items[1].text == "yes"

    # Load-bearing KiCAD 10 attribute fields: omitting any of these
    # makes kicad-cli refuse to load the parent schematic (exit 3,
    # "Failed to load schematic" with no stderr detail). Matches
    # eeschema's own emission for a placed sheet.
    for head, expected in (
        ("exclude_from_sim", "no"),
        ("in_bom", "yes"),
        ("on_board", "yes"),
        ("dnp", "no"),
    ):
        node = sheet.find(head)
        assert node is not None, f"missing load-bearing '{head}' on sheet node"
        assert isinstance(node.items[1], SAtom) and node.items[1].text == expected

    # Sheetname + Sheetfile properties both present, both with an
    # explicit ``0`` angle on their ``(at X Y 0)`` node. Elision of
    # that zero is what tripped KiCAD 10's sheet-property parser; the
    # assertion pins the emission so future refactors can't regress it.
    sheetname_present = False
    sheetfile_present = False
    for child in sheet.items:
        if isinstance(child, SList) and child.head == "property":
            key = child.items[1] if len(child.items) > 1 else None
            if isinstance(key, SAtom) and key.text == "Sheetname":
                val = child.items[2]
                assert isinstance(val, SAtom) and val.text == "MCU"
                sheetname_present = True
            if isinstance(key, SAtom) and key.text == "Sheetfile":
                val = child.items[2]
                assert isinstance(val, SAtom) and val.text == "mcu.kicad_sch"
                sheetfile_present = True
            # Every sheet property must carry an (at X Y 0) node with
            # the angle atom explicitly present.
            at_prop = child.find("at")
            assert at_prop is not None
            assert len(at_prop.items) == 4, (
                f"sheet property '{key.text if isinstance(key, SAtom) else '?'}'"
                f" must emit (at X Y 0) with explicit zero angle; got "
                f"{len(at_prop.items) - 1} positional atoms"
            )
            angle = at_prop.items[3]
            assert isinstance(angle, SAtom) and angle.text == "0"
    assert sheetname_present
    assert sheetfile_present


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    before = parent.read_text(encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Decoy",
            sheet_file="decoy.kicad_sch",
            at_x=0.0,
            at_y=0.0,
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.sheet_uuid is None
    # Parent unchanged.
    assert parent.read_text(encoding="utf-8") == before
    # Child not created in dry-run either.
    assert not (tmp_path / "decoy.kicad_sch").exists()


@pytest.mark.asyncio
async def test_existing_valid_child_is_linked(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    # Pre-create a valid child.
    pre = tmp_path / "existing.kicad_sch"
    pre.write_text(
        """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols)
)
""",
        encoding="utf-8",
    )
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Existing",
            sheet_file="existing.kicad_sch",
            at_x=10.0,
            at_y=10.0,
        )
    )
    assert out.status == "ok"
    assert out.child_created is False  # linked, not created.


@pytest.mark.asyncio
async def test_create_if_missing_false_skips_child(tmp_path: Path) -> None:
    """Caller opts out — we don't create the child."""
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Manual",
            sheet_file="manual.kicad_sch",
            at_x=10.0,
            at_y=10.0,
            create_if_missing=False,
        )
    )
    assert out.status == "ok"
    assert out.child_created is False
    assert not (tmp_path / "manual.kicad_sch").exists()


@pytest.mark.asyncio
async def test_absolute_sheet_file_is_rebased(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    abs_child = tmp_path / "sub.kicad_sch"
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Sub",
            sheet_file=str(abs_child),
            at_x=10.0,
            at_y=10.0,
        )
    )
    assert out.status == "ok"
    # Stored form is the relative path, not the absolute one.
    assert out.sheet_file == "sub.kicad_sch"
    assert out.sheet_file_abs == str(abs_child.resolve())


# -- invalid input ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_empty_name(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="",
            sheet_file="x.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_empty_file(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="OK",
            sheet_file="",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_wrong_file_suffix(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="OK",
            sheet_file="wrong.txt",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "invalid_input"
    assert out.note is not None and ".kicad_sch" in out.note


# -- parent errors ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_missing(tmp_path: Path) -> None:
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=tmp_path / "nope.kicad_sch",
            sheet_name="OK",
            sheet_file="x.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_sch_not_found_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=f,
            sheet_name="OK",
            sheet_file="x.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=sch,
            sheet_name="OK",
            sheet_file="x.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "wrong.kicad_sch"
    sch.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=sch,
            sheet_name="OK",
            sheet_file="x.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "invalid_schema"


# -- child-file conflicts --------------------------------------------------


@pytest.mark.asyncio
async def test_child_conflict_existing_directory(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    # sheet_file points at a directory — cannot be used as a file.
    (tmp_path / "dir.kicad_sch").mkdir()
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Sub",
            sheet_file="dir.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sheet_file_conflict"


@pytest.mark.asyncio
async def test_child_conflict_unparseable_existing(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    bad = tmp_path / "bad.kicad_sch"
    bad.write_text("(kicad_sch (oops", encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Sub",
            sheet_file="bad.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sheet_file_conflict"


@pytest.mark.asyncio
async def test_child_conflict_wrong_top_head(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    wrong = tmp_path / "wrong.kicad_sch"
    wrong.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Sub",
            sheet_file="wrong.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sheet_file_conflict"

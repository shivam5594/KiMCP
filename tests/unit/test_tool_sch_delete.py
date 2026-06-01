"""Unit tests for M20 ``sch_delete``.

Strategy: add an element via the existing M14-M17 tools, then delete it
by UUID and verify it's gone. Also covers the status matrix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom
from kimcp.tools.builtin.sch_add_junction import SchAddJunctionInput, SchAddJunctionTool
from kimcp.tools.builtin.sch_add_label import SchAddLabelInput, SchAddLabelTool
from kimcp.tools.builtin.sch_add_wire import SchAddWireInput, SchAddWireTool
from kimcp.tools.builtin.sch_delete import (
    SchDeleteInput,
    SchDeleteOutput,
    SchDeleteTool,
    _find_by_uuid,
)

# -- fixtures --------------------------------------------------------------


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

_PCB = """\
(kicad_pcb (version 20240108) (generator "pcbnew"))
"""


def _write(tmp_path: Path, body: str = _SCH) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _tool(snapshot_mode: str = "off") -> SchDeleteTool:
    tool = SchDeleteTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode}}))
    return tool


def _add_tool(snapshot_mode: str = "off"):
    """Return a configured wire tool for seeding elements."""
    tool = SchAddWireTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode}}))
    return tool


def _junction_tool(snapshot_mode: str = "off"):
    tool = SchAddJunctionTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode}}))
    return tool


def _label_tool(snapshot_mode: str = "off"):
    tool = SchAddLabelTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode}}))
    return tool


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchDeleteTool()
    assert tool.name == "sch_delete"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight / input validation -----------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    out = await _tool().run(
        SchDeleteInput(
            sch_path=tmp_path / "nope.kicad_sch",
            uuid="some-uuid",
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_path_is_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    out = await _tool().run(SchDeleteInput(sch_path=d, uuid="u"))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(SchDeleteInput(sch_path=f, uuid="u"))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(SchDeleteInput(sch_path=sch, uuid="u"))
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(SchDeleteInput(sch_path=sch, uuid="u"))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_uuid_not_found(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid="nonexistent-uuid")
    )
    assert out.status == "uuid_not_found"


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    # Add a wire to have something to target.
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None
    before = sch.read_bytes()

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid, dry_run=True)
    )
    assert out.status == "dry_run"
    assert out.deleted_head == "wire"
    assert out.deleted_uuid == wire_out.wire_uuid
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy paths: delete each element type ---------------------------------


@pytest.mark.asyncio
async def test_delete_wire(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid)
    )
    assert out.status == "ok"
    assert out.deleted_head == "wire"
    assert out.deleted_uuid == wire_out.wire_uuid

    # Verify it's gone.
    doc = SexprDocument.from_path(sch)
    assert _find_by_uuid(doc.root, wire_out.wire_uuid) is None


@pytest.mark.asyncio
async def test_delete_junction(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    junc_out = await _junction_tool().run(
        SchAddJunctionInput(sch_path=sch, at_x=5.0, at_y=5.0)
    )
    assert junc_out.junction_uuid is not None

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=junc_out.junction_uuid)
    )
    assert out.status == "ok"
    assert out.deleted_head == "junction"

    doc = SexprDocument.from_path(sch)
    assert _find_by_uuid(doc.root, junc_out.junction_uuid) is None


@pytest.mark.asyncio
async def test_delete_label(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    label_out = await _label_tool().run(
        SchAddLabelInput(sch_path=sch, text="NET", at_x=0.0, at_y=0.0)
    )
    assert label_out.label_uuid is not None

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=label_out.label_uuid)
    )
    assert out.status == "ok"
    assert out.deleted_head == "label"

    doc = SexprDocument.from_path(sch)
    assert _find_by_uuid(doc.root, label_out.label_uuid) is None


@pytest.mark.asyncio
async def test_delete_global_label(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    label_out = await _label_tool().run(
        SchAddLabelInput(
            sch_path=sch, text="VCC", at_x=0.0, at_y=0.0, kind="global"
        )
    )
    assert label_out.label_uuid is not None

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=label_out.label_uuid)
    )
    assert out.status == "ok"
    assert out.deleted_head == "global_label"


@pytest.mark.asyncio
async def test_delete_hierarchical_label(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    label_out = await _label_tool().run(
        SchAddLabelInput(
            sch_path=sch,
            text="SDA",
            at_x=0.0,
            at_y=0.0,
            kind="hierarchical",
            shape="bidirectional",
        )
    )
    assert label_out.label_uuid is not None

    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=label_out.label_uuid)
    )
    assert out.status == "ok"
    assert out.deleted_head == "hierarchical_label"


# -- preservation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_other_elements_preserved_after_delete(tmp_path: Path) -> None:
    """After deleting one wire, another wire remains intact."""
    sch = _write(tmp_path)
    add = _add_tool()
    w1 = await add.run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    w2 = await add.run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=10.0, end_x=10.0, end_y=10.0)
    )
    assert w1.wire_uuid is not None and w2.wire_uuid is not None

    await _tool().run(SchDeleteInput(sch_path=sch, uuid=w1.wire_uuid))

    doc = SexprDocument.from_path(sch)
    assert _find_by_uuid(doc.root, w1.wire_uuid) is None
    assert _find_by_uuid(doc.root, w2.wire_uuid) is not None


@pytest.mark.asyncio
async def test_top_uuid_unchanged_after_delete(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = SexprDocument.from_path(sch)
    before_uuid_node = before.root.find("uuid")
    assert before_uuid_node is not None
    orig_uuid = before_uuid_node.items[1]
    assert isinstance(orig_uuid, SAtom)

    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None
    await _tool().run(SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid))

    after = SexprDocument.from_path(sch)
    after_uuid_node = after.root.find("uuid")
    assert after_uuid_node is not None
    after_uuid = after_uuid_node.items[1]
    assert isinstance(after_uuid, SAtom)
    assert orig_uuid.text == after_uuid.text


# -- snapshot modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_off(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None
    out = await _tool(snapshot_mode="off").run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None
    out = await _tool(snapshot_mode="copy").run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None
    out = await SchDeleteTool().run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid)
    )
    assert out.status == "ok"


# -- write failure --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sch = _write(tmp_path)
    wire_out = await _add_tool().run(
        SchAddWireInput(sch_path=sch, start_x=0.0, start_y=0.0, end_x=10.0, end_y=0.0)
    )
    assert wire_out.wire_uuid is not None

    def _boom(self: Any) -> bytes:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(SexprDocument, "serialize", _boom, raising=True)
    out = await _tool().run(
        SchDeleteInput(sch_path=sch, uuid=wire_out.wire_uuid)
    )
    assert out.status == "write_failed"


# -- helper / output pin ---------------------------------------------------


def test_output_defaults() -> None:
    out = SchDeleteOutput(status="dry_run")
    assert out.deleted_head is None
    assert out.deleted_uuid is None
    assert out.note is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchDeleteTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off"}})
    tool.set_config(cfg)
    assert tool._config is cfg

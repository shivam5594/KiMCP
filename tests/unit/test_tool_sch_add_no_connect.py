"""Unit tests for M21 ``sch_add_no_connect``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_no_connect import (
    SchAddNoConnectInput,
    SchAddNoConnectOutput,
    SchAddNoConnectTool,
    _build_no_connect_node,
    _find_no_connect_by_uuid,
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


def _tool(snapshot_mode: str = "off") -> SchAddNoConnectTool:
    tool = SchAddNoConnectTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None}}))
    return tool


def _atom_text(node: SList, idx: int) -> str:
    a = node.items[idx]
    assert isinstance(a, SAtom)
    return a.text


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchAddNoConnectTool()
    assert tool.name == "sch_add_no_connect"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight / input validation -----------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    out = await _tool().run(
        SchAddNoConnectInput(
            sch_path=tmp_path / "nope.kicad_sch",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_path_is_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=d, at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=f, at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "parse_failed"


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=10.0, at_y=20.0, dry_run=True)
    )
    assert out.status == "dry_run"
    assert out.no_connect_uuid is None
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_connect_roundtrips(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=50.0, at_y=30.0)
    )
    assert out.status == "ok"
    assert out.no_connect_uuid is not None

    doc = SexprDocument.from_path(sch)
    node = _find_no_connect_by_uuid(doc.root, out.no_connect_uuid)
    assert node is not None
    assert node.head == "no_connect"
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "50"
    assert _atom_text(at, 2) == "30"


@pytest.mark.asyncio
async def test_no_connect_fractional_coordinates(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=25.4, at_y=12.7)
    )
    assert out.status == "ok"
    assert out.no_connect_uuid is not None

    doc = SexprDocument.from_path(sch)
    node = _find_no_connect_by_uuid(doc.root, out.no_connect_uuid)
    assert node is not None
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "25.4"
    assert _atom_text(at, 2) == "12.7"


# -- multiple no-connects --------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_no_connects_distinct_uuids(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = _tool()
    uuids: list[str | None] = []
    for i in range(3):
        out = await tool.run(
            SchAddNoConnectInput(sch_path=sch, at_x=float(i * 10), at_y=0.0)
        )
        assert out.status == "ok"
        uuids.append(out.no_connect_uuid)
    assert len({u for u in uuids if u is not None}) == 3


# -- preservation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_content_preserved(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = SexprDocument.from_path(sch)
    before_uuid = before.root.find("uuid")
    assert before_uuid is not None

    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    after = SexprDocument.from_path(sch)
    after_uuid = after.root.find("uuid")
    assert after_uuid is not None
    assert _atom_text(before_uuid, 1) == _atom_text(after_uuid, 1)


# -- snapshot modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_off(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool(snapshot_mode="off").run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await SchAddNoConnectTool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"


# -- write failure --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sch = _write(tmp_path)

    def _boom(self: Any) -> bytes:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(SexprDocument, "serialize", _boom, raising=True)
    out = await _tool().run(
        SchAddNoConnectInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "write_failed"


# -- helper / output pin --------------------------------------------------


def test_build_no_connect_node_shape() -> None:
    node = _build_no_connect_node(at_x=10.0, at_y=20.0, nc_uuid="test-uuid")
    assert node.head == "no_connect"
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "10"
    assert _atom_text(at, 2) == "20"
    uuid_child = node.find("uuid")
    assert uuid_child is not None
    assert _atom_text(uuid_child, 1) == "test-uuid"


def test_output_defaults() -> None:
    out = SchAddNoConnectOutput(status="dry_run")
    assert out.no_connect_uuid is None
    assert out.sch_path is None
    assert out.note is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddNoConnectTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}})
    tool.set_config(cfg)
    assert tool._config is cfg

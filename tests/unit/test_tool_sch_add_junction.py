"""Unit tests for M16 ``sch_add_junction``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_junction import (
    SchAddJunctionInput,
    SchAddJunctionOutput,
    SchAddJunctionTool,
    _build_junction_node,
)

_SCH_MINIMAL = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""


_PCB_NOT_SCH = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew"))
"""


def _write_sch(tmp_path: Path, body: str = _SCH_MINIMAL) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _tool(snapshot_mode: str = "off") -> SchAddJunctionTool:
    tool = SchAddJunctionTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None}}))
    return tool


def _atom_text(node: SList, idx: int) -> str:
    a = node.items[idx]
    assert isinstance(a, SAtom)
    return a.text


def _find_junction_by_uuid(root: SList, junction_uuid: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "junction":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        payload = uuid_node.items[1]
        if isinstance(payload, SAtom) and payload.text == junction_uuid:
            return child
    return None


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchAddJunctionTool()
    assert tool.name == "sch_add_junction"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight -------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    out = await _tool().run(
        SchAddJunctionInput(sch_path=tmp_path / "nope.kicad_sch", at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "sub.kicad_sch").mkdir()
    out = await _tool().run(
        SchAddJunctionInput(sch_path=tmp_path / "sub.kicad_sch", at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"
    assert out.note is not None and "not a regular file" in out.note


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB_NOT_SCH, encoding="utf-8")
    out = await _tool().run(
        SchAddJunctionInput(sch_path=path, at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


# -- parse / shape ---------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0))
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB_NOT_SCH, encoding="utf-8")
    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0))
    assert out.status == "invalid_schema"


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes_and_omits_uuid(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddJunctionInput(sch_path=sch, at_x=100.0, at_y=50.0, dry_run=True)
    )
    assert out.status == "dry_run"
    assert out.junction_uuid is None
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_adds_junction_and_round_trips(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=100.0, at_y=50.0))
    assert out.status == "ok"
    assert out.junction_uuid is not None

    doc = SexprDocument.from_path(sch)
    jn = _find_junction_by_uuid(doc.root, out.junction_uuid)
    assert jn is not None
    at = jn.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "100"
    assert _atom_text(at, 2) == "50"


@pytest.mark.asyncio
async def test_default_diameter_and_color(tmp_path: Path) -> None:
    """Emitted junction carries ``(diameter 0)`` and ``(color 0 0 0 0)``."""
    sch = _write_sch(tmp_path)
    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0))
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.junction_uuid is not None
    jn = _find_junction_by_uuid(doc.root, out.junction_uuid)
    assert jn is not None
    diameter = jn.find("diameter")
    assert diameter is not None and _atom_text(diameter, 1) == "0"
    color = jn.find("color")
    assert color is not None
    # head + 4 RGBA atoms = 5 items
    assert len(color.items) == 5
    for idx in (1, 2, 3, 4):
        assert _atom_text(color, idx) == "0"


@pytest.mark.asyncio
async def test_multiple_junctions_distinct_uuids(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = _tool()
    uuids: list[str | None] = []
    for y in (0.0, 10.0, 20.0):
        out = await tool.run(SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=y))
        assert out.status == "ok"
        uuids.append(out.junction_uuid)
    assert len({u for u in uuids if u is not None}) == 3


@pytest.mark.asyncio
async def test_existing_content_preserved(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    doc_before = SexprDocument.from_path(sch)
    uuid_before = doc_before.root.find("uuid")
    assert uuid_before is not None

    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=5.0, at_y=5.0))
    assert out.status == "ok"

    doc_after = SexprDocument.from_path(sch)
    uuid_after = doc_after.root.find("uuid")
    assert uuid_after is not None
    assert _atom_text(uuid_before, 1) == _atom_text(uuid_after, 1)


# -- snapshot modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_off(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool(snapshot_mode="off").run(
        SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await SchAddJunctionTool().run(
        SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"


# -- write failure ---------------------------------------------------------


@pytest.mark.asyncio
async def test_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sch = _write_sch(tmp_path)

    def _boom(self: Any) -> bytes:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(SexprDocument, "serialize", _boom, raising=True)
    out = await _tool().run(SchAddJunctionInput(sch_path=sch, at_x=0.0, at_y=0.0))
    assert out.status == "write_failed"
    assert out.note is not None and "save failed" in out.note


# -- helper pin ------------------------------------------------------------


def test_build_junction_node_shape() -> None:
    node = _build_junction_node(at_x=10.0, at_y=20.0, junction_uuid="deadbeef")
    assert node.head == "junction"
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "10"
    assert _atom_text(at, 2) == "20"
    # Default diameter=0, color=(0 0 0 0).
    assert node.find("diameter") is not None
    color = node.find("color")
    assert color is not None and len(color.items) == 5
    uuid = node.find("uuid")
    assert uuid is not None
    payload = uuid.items[1]
    assert isinstance(payload, SAtom) and payload.quoted is True


def test_output_defaults() -> None:
    out = SchAddJunctionOutput(status="dry_run")
    assert out.sch_path is None
    assert out.junction_uuid is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddJunctionTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}})
    tool.set_config(cfg)
    assert tool._config is cfg

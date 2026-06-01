"""Unit tests for M15 ``sch_add_wire``.

Structure mirrors M14 — preflight matrix, parse failure, invalid_schema,
invalid_geometry (zero-length), dry_run (bytes preserved), happy-path
mutation + round-trip, snapshot modes, DI, and helper pins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import Config, SafetyCfg, load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_wire import (
    SchAddWireInput,
    SchAddWireOutput,
    SchAddWireTool,
    _build_wire_node,
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


def _write_sch(tmp_path: Path, text: str = _SCH_MINIMAL) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    return sch


def _tool(snapshot_mode: str = "off") -> SchAddWireTool:
    """Return a tool wired with a snapshot_mode override.

    Uses ``load_config`` so the ``Literal['git','copy','off']`` type on
    ``SafetyCfg.snapshot_mode`` doesn't require ``cast`` at every call
    site — matches M14's pattern.
    """
    tool = SchAddWireTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None}}))
    return tool


# -- tool metadata ---------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchAddWireTool()
    assert tool.name == "sch_add_wire"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight matrix ------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_returns_sch_not_found(tmp_path: Path) -> None:
    out = await _tool().run(
        SchAddWireInput(
            sch_path=tmp_path / "nope.kicad_sch",
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "sch_not_found"
    assert out.sch_path is None


@pytest.mark.asyncio
async def test_directory_returns_sch_not_found(tmp_path: Path) -> None:
    (tmp_path / "sub.kicad_sch").mkdir()
    out = await _tool().run(
        SchAddWireInput(
            sch_path=tmp_path / "sub.kicad_sch",
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "sch_not_found"
    assert out.note is not None and "not a regular file" in out.note


@pytest.mark.asyncio
async def test_wrong_suffix_returns_sch_not_found(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB_NOT_SCH, encoding="utf-8")
    out = await _tool().run(
        SchAddWireInput(
            sch_path=path,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "sch_not_found"
    assert out.note is not None and "kicad_sch" in out.note


# -- parse / shape ---------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_returns_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (unterminated", encoding="utf-8")
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_wrong_top_head_returns_invalid_schema(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB_NOT_SCH, encoding="utf-8")
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "invalid_schema"
    assert out.note is not None and "kicad_sch" in out.note


# -- geometry --------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_length_wire_rejected(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=5.0,
            start_y=5.0,
            end_x=5.0,
            end_y=5.0,
        )
    )
    assert out.status == "invalid_geometry"
    assert out.note is not None and "zero-length" in out.note
    # File untouched.
    assert sch.read_bytes() == before


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes_and_omits_uuid(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=10.0,
            start_y=20.0,
            end_x=30.0,
            end_y=20.0,
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.wire_uuid is None
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_adds_wire_and_round_trips(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=10.0,
            start_y=20.0,
            end_x=30.0,
            end_y=20.0,
        )
    )
    assert out.status == "ok"
    assert out.wire_uuid is not None and len(out.wire_uuid) > 0

    # Re-parse from disk — the new wire landed.
    doc = SexprDocument.from_path(sch)
    wire = _find_wire_by_uuid(doc.root, out.wire_uuid)
    assert wire is not None
    pts = wire.find("pts")
    assert pts is not None
    xys = [child for child in pts.items if isinstance(child, SList) and child.head == "xy"]
    assert len(xys) == 2
    # Endpoints preserved.
    assert _atom_text(xys[0], 1) == "10" and _atom_text(xys[0], 2) == "20"
    assert _atom_text(xys[1], 1) == "30" and _atom_text(xys[1], 2) == "20"


@pytest.mark.asyncio
async def test_adds_wire_with_fractional_coordinates(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=12.7,
            start_y=5.08,
            end_x=25.4,
            end_y=5.08,
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.wire_uuid is not None
    wire = _find_wire_by_uuid(doc.root, out.wire_uuid)
    assert wire is not None
    pts = wire.find("pts")
    assert pts is not None
    xys = [c for c in pts.items if isinstance(c, SList) and c.head == "xy"]
    assert _atom_text(xys[0], 1) == "12.7"
    assert _atom_text(xys[0], 2) == "5.08"


@pytest.mark.asyncio
async def test_multiple_wires_get_distinct_uuids(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = _tool()
    outs = []
    for x in (10.0, 20.0, 30.0):
        outs.append(
            await tool.run(
                SchAddWireInput(
                    sch_path=sch,
                    start_x=x,
                    start_y=0.0,
                    end_x=x,
                    end_y=10.0,
                )
            )
        )
    uuids = [o.wire_uuid for o in outs]
    assert all(u is not None for u in uuids)
    assert len({u for u in uuids if u is not None}) == 3


@pytest.mark.asyncio
async def test_vertical_wire_endpoints(tmp_path: Path) -> None:
    """Vertical segment: X preserved, Y distinct. Simple sanity pin."""
    sch = _write_sch(tmp_path)
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=50.0,
            start_y=10.0,
            end_x=50.0,
            end_y=40.0,
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.wire_uuid is not None
    wire = _find_wire_by_uuid(doc.root, out.wire_uuid)
    assert wire is not None
    pts = wire.find("pts")
    assert pts is not None
    xys = [c for c in pts.items if isinstance(c, SList) and c.head == "xy"]
    assert _atom_text(xys[0], 1) == _atom_text(xys[1], 1) == "50"
    assert _atom_text(xys[0], 2) == "10" and _atom_text(xys[1], 2) == "40"


@pytest.mark.asyncio
async def test_wire_carries_default_stroke(tmp_path: Path) -> None:
    """New wire emits ``(stroke (width 0) (type default))`` verbatim."""
    sch = _write_sch(tmp_path)
    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.wire_uuid is not None
    wire = _find_wire_by_uuid(doc.root, out.wire_uuid)
    assert wire is not None
    stroke = wire.find("stroke")
    assert stroke is not None
    width = stroke.find("width")
    assert width is not None and _atom_text(width, 1) == "0"
    stroke_type = stroke.find("type")
    assert stroke_type is not None and _atom_text(stroke_type, 1) == "default"


# -- preserves existing content -------------------------------------------


@pytest.mark.asyncio
async def test_existing_nodes_preserved(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    doc_before = SexprDocument.from_path(sch)
    top_uuid_before = doc_before.root.find("uuid")
    paper_before = doc_before.root.find("paper")
    assert top_uuid_before is not None and paper_before is not None

    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "ok"

    doc_after = SexprDocument.from_path(sch)
    top_uuid_after = doc_after.root.find("uuid")
    paper_after = doc_after.root.find("paper")
    assert top_uuid_after is not None and paper_after is not None
    assert _atom_text(top_uuid_before, 1) == _atom_text(top_uuid_after, 1)
    assert _atom_text(paper_before, 1) == _atom_text(paper_after, 1)


# -- snapshot modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mode_off_reports_disabled(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool(snapshot_mode="off").run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_mode_copy_creates_ref(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    """Tool ships usable without DI — defaults to snapshot_mode='git'.

    In a non-git dir this falls back to copy-mode, exercising the
    safety layer end-to-end with zero wiring.
    """
    sch = _write_sch(tmp_path)
    out = await SchAddWireTool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "ok"


# -- write_failed path -----------------------------------------------------


@pytest.mark.asyncio
async def test_write_failure_returns_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch serializer → save() raises RuntimeError → write_failed."""
    sch = _write_sch(tmp_path)

    def _boom(self: Any) -> bytes:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(SexprDocument, "serialize", _boom, raising=True)

    out = await _tool().run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.status == "write_failed"
    assert out.meta.snapshot_ref == "disabled"
    assert out.note is not None and "save failed" in out.note


# -- DI --------------------------------------------------------------------


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddWireTool()
    assert tool._config is None
    cfg = Config(safety=SafetyCfg(snapshot_mode="off", grid_snap_mm=None))
    tool.set_config(cfg)
    assert tool._config is cfg


# -- helper pin ------------------------------------------------------------


def test_build_wire_node_shape() -> None:
    node = _build_wire_node(
        start_x=1.0,
        start_y=2.0,
        end_x=11.0,
        end_y=2.0,
        wire_uuid="deadbeef",
    )
    assert node.head == "wire"
    pts = node.find("pts")
    assert pts is not None
    xys = [c for c in pts.items if isinstance(c, SList) and c.head == "xy"]
    assert len(xys) == 2
    # Endpoints encoded as integer strings (1.0 → "1").
    assert _atom_text(xys[0], 1) == "1"
    assert _atom_text(xys[0], 2) == "2"
    assert _atom_text(xys[1], 1) == "11"
    assert _atom_text(xys[1], 2) == "2"
    # UUID present and quoted.
    uuid = node.find("uuid")
    assert uuid is not None
    payload = uuid.items[1]
    assert isinstance(payload, SAtom) and payload.quoted is True
    assert payload.text == "deadbeef"


def test_output_defaults_have_correct_types() -> None:
    out = SchAddWireOutput(status="dry_run")
    assert out.sch_path is None
    assert out.wire_uuid is None
    assert out.note is None


# -- test-local helpers ----------------------------------------------------


def _atom_text(node: SList, idx: int) -> str:
    """Narrow a ``(head … atom …)`` child into a plain string for assertion."""
    a = node.items[idx]
    assert isinstance(a, SAtom), f"expected SAtom at index {idx}, got {type(a).__name__}"
    return a.text


def _find_wire_by_uuid(root: SList, wire_uuid: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "wire":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        payload = uuid_node.items[1]
        if isinstance(payload, SAtom) and payload.text == wire_uuid:
            return child
    return None

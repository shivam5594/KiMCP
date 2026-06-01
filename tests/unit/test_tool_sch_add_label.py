"""Unit tests for M17 ``sch_add_label`` (local/global/hierarchical)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_label import (
    SchAddLabelInput,
    SchAddLabelOutput,
    SchAddLabelTool,
    _build_label_node,
    _find_label_by_uuid,
)

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


def _tool(snapshot_mode: str = "off") -> SchAddLabelTool:
    tool = SchAddLabelTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None}}))
    return tool


def _atom_text(node: SList, idx: int) -> str:
    a = node.items[idx]
    assert isinstance(a, SAtom)
    return a.text


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchAddLabelTool()
    assert tool.name == "sch_add_label"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight / input validation -----------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    out = await _tool().run(
        SchAddLabelInput(
            sch_path=tmp_path / "nope.kicad_sch", text="NET", at_x=0.0, at_y=0.0
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_empty_text_rejected(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddLabelInput(sch_path=sch, text="", at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_input"
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchAddLabelInput(sch_path=sch, text="NET", at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(
        SchAddLabelInput(sch_path=sch, text="NET", at_x=0.0, at_y=0.0)
    )
    assert out.status == "parse_failed"


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddLabelInput(
            sch_path=sch, text="VCC", at_x=10.0, at_y=20.0, dry_run=True
        )
    )
    assert out.status == "dry_run"
    assert out.label_uuid is None
    assert out.meta.snapshot_ref is None
    assert out.text == "VCC"
    assert out.kind == "local"
    assert sch.read_bytes() == before


# -- happy-path for each kind --------------------------------------------


@pytest.mark.asyncio
async def test_local_label_roundtrips(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddLabelInput(
            sch_path=sch, text="BOOT_MODE", at_x=50.0, at_y=30.0, kind="local"
        )
    )
    assert out.status == "ok"
    assert out.label_uuid is not None
    assert out.kind == "local"

    doc = SexprDocument.from_path(sch)
    node = _find_label_by_uuid(doc.root, out.label_uuid)
    assert node is not None
    assert node.head == "label"
    # (label "BOOT_MODE" ...) — text is the atom at index 1.
    name_atom = node.items[1]
    assert isinstance(name_atom, SAtom) and name_atom.text == "BOOT_MODE"
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "50" and _atom_text(at, 2) == "30"
    # Local labels should NOT carry a (shape ...) child.
    assert node.find("shape") is None


@pytest.mark.asyncio
async def test_global_label_roundtrips(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddLabelInput(
            sch_path=sch,
            text="VBUS",
            at_x=100.0,
            at_y=40.0,
            kind="global",
            shape="bidirectional",
        )
    )
    assert out.status == "ok"
    assert out.label_uuid is not None

    doc = SexprDocument.from_path(sch)
    node = _find_label_by_uuid(doc.root, out.label_uuid)
    assert node is not None
    assert node.head == "global_label"
    shape = node.find("shape")
    assert shape is not None and _atom_text(shape, 1) == "bidirectional"
    # fields_autoplaced is emitted for globals.
    fap = node.find("fields_autoplaced")
    assert fap is not None and _atom_text(fap, 1) == "yes"
    # Intersheetrefs property exists + is hidden.
    isr = None
    for child in node.items:
        if (
            isinstance(child, SList)
            and child.head == "property"
            and len(child.items) >= 2
        ):
            name = child.items[1]
            if isinstance(name, SAtom) and name.text == "Intersheetrefs":
                isr = child
                break
    assert isr is not None
    effects = isr.find("effects")
    assert effects is not None
    assert effects.find("hide") is not None


@pytest.mark.asyncio
async def test_hierarchical_label_roundtrips(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddLabelInput(
            sch_path=sch,
            text="SDA",
            at_x=0.0,
            at_y=0.0,
            kind="hierarchical",
            shape="output",
        )
    )
    assert out.status == "ok"
    assert out.label_uuid is not None

    doc = SexprDocument.from_path(sch)
    node = _find_label_by_uuid(doc.root, out.label_uuid)
    assert node is not None
    assert node.head == "hierarchical_label"
    shape = node.find("shape")
    assert shape is not None and _atom_text(shape, 1) == "output"
    # Hierarchical does NOT carry fields_autoplaced or Intersheetrefs.
    assert node.find("fields_autoplaced") is None


@pytest.mark.asyncio
async def test_local_label_with_angle(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddLabelInput(sch_path=sch, text="NET", at_x=0.0, at_y=0.0, angle=90.0)
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.label_uuid is not None
    node = _find_label_by_uuid(doc.root, out.label_uuid)
    assert node is not None
    at = node.find("at")
    assert at is not None
    assert _atom_text(at, 3) == "90"


# -- multiple labels ------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_labels_distinct_uuids(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = _tool()
    uuids: list[str | None] = []
    for name in ("A", "B", "C"):
        out = await tool.run(
            SchAddLabelInput(sch_path=sch, text=name, at_x=0.0, at_y=0.0)
        )
        assert out.status == "ok"
        uuids.append(out.label_uuid)
    assert len({u for u in uuids if u is not None}) == 3


# -- preservation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_content_preserved(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = SexprDocument.from_path(sch)
    before_uuid = before.root.find("uuid")
    assert before_uuid is not None

    out = await _tool().run(
        SchAddLabelInput(sch_path=sch, text="NET", at_x=0.0, at_y=0.0)
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
        SchAddLabelInput(sch_path=sch, text="N", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchAddLabelInput(sch_path=sch, text="N", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await SchAddLabelTool().run(
        SchAddLabelInput(sch_path=sch, text="N", at_x=0.0, at_y=0.0)
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
        SchAddLabelInput(sch_path=sch, text="N", at_x=0.0, at_y=0.0)
    )
    assert out.status == "write_failed"


# -- helper / output pin --------------------------------------------------


def test_build_label_node_local_shape() -> None:
    node = _build_label_node(
        kind="local",
        text="NET",
        at_x=10.0,
        at_y=20.0,
        angle=0.0,
        shape="input",  # ignored for local
        label_uuid="u",
    )
    assert node.head == "label"
    # Local: no (shape ...)
    assert node.find("shape") is None


def test_build_label_node_global_has_intersheetrefs() -> None:
    node = _build_label_node(
        kind="global",
        text="VCC",
        at_x=0.0,
        at_y=0.0,
        angle=0.0,
        shape="input",
        label_uuid="u",
    )
    assert node.head == "global_label"
    isr = None
    for child in node.items:
        if isinstance(child, SList) and child.head == "property":
            name_atom = child.items[1]
            if isinstance(name_atom, SAtom) and name_atom.text == "Intersheetrefs":
                isr = child
    assert isr is not None
    # Template literal — KiCAD substitutes at render time.
    value_atom = isr.items[2]
    assert isinstance(value_atom, SAtom) and value_atom.text == "${INTERSHEET_REFS}"


def test_build_label_node_hierarchical_shape() -> None:
    node = _build_label_node(
        kind="hierarchical",
        text="SDA",
        at_x=0.0,
        at_y=0.0,
        angle=0.0,
        shape="bidirectional",
        label_uuid="u",
    )
    assert node.head == "hierarchical_label"
    shape = node.find("shape")
    assert shape is not None
    assert _atom_text(shape, 1) == "bidirectional"


def test_build_label_node_rejects_invalid_shape() -> None:
    with pytest.raises(ValueError, match="invalid label shape"):
        _build_label_node(
            kind="global",
            text="X",
            at_x=0.0,
            at_y=0.0,
            angle=0.0,
            shape="not-a-shape",  # type: ignore[arg-type]
            label_uuid="u",
        )


def test_output_defaults() -> None:
    out = SchAddLabelOutput(status="dry_run")
    assert out.label_uuid is None
    assert out.text is None
    assert out.kind is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddLabelTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}})
    tool.set_config(cfg)
    assert tool._config is cfg

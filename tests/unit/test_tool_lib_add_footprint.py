"""Unit tests for lib_add_footprint (M45)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.lib_add_footprint import (
    FootprintLine,
    FootprintPad,
    LibAddFootprintInput,
    LibAddFootprintTool,
)


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off"}},
    )


def _property_value(root: SList, name: str) -> str | None:
    for prop in root.find_all("property"):
        if len(prop.items) < 3:
            continue
        key = prop.items[1]
        if isinstance(key, SAtom) and key.text == name:
            val = prop.items[2]
            return val.text if isinstance(val, SAtom) else None
    return None


_SMD_PAD = FootprintPad(
    number="1", pad_type="smd", shape="roundrect",
    x=0.0, y=0.0, size_w=1.0, size_h=0.6,
)


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibAddFootprintTool()
    assert tool.name == "lib_add_footprint"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_pretty_dir_and_footprint(tmp_path: Path) -> None:
    """Missing .pretty/ → tool creates dir and writes the .kicad_mod."""
    lib = tmp_path / "custom.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="R_0603",
            description="Resistor 0603 SMD",
            tags="resistor SMD 0603",
            attributes=["smd"],
            pads=[
                FootprintPad(
                    number="1", pad_type="smd", shape="roundrect",
                    x=-0.8, y=0.0, size_w=0.9, size_h=0.95,
                ),
                FootprintPad(
                    number="2", pad_type="smd", shape="roundrect",
                    x=0.8, y=0.0, size_w=0.9, size_h=0.95,
                ),
            ],
            lines=[
                FootprintLine(
                    start_x=-1.5, start_y=-0.6, end_x=1.5, end_y=-0.6,
                    layer="F.SilkS", width=0.12,
                ),
            ],
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    assert out.created_library is True
    assert out.pad_count == 2
    assert out.line_count == 1
    assert lib.is_dir()

    fp_file = lib / "R_0603.kicad_mod"
    assert fp_file.is_file()
    doc = SexprDocument.from_path(fp_file)
    assert doc.top_head == "footprint"
    assert doc.version == "20241229"
    # Name atom at position 1.
    assert isinstance(doc.root.items[1], SAtom)
    assert doc.root.items[1].text == "R_0603"

    # Reference property carries the REF** placeholder.
    assert _property_value(doc.root, "Reference") == "REF**"
    # Two pads, one line.
    pads = [c for c in doc.root.items if isinstance(c, SList) and c.head == "pad"]
    assert len(pads) == 2
    lines = [c for c in doc.root.items if isinstance(c, SList) and c.head == "fp_line"]
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    lib = tmp_path / "dry.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib, footprint_name="X", pads=[_SMD_PAD], dry_run=True
        )
    )
    assert out.status == "dry_run"
    assert not lib.exists()


@pytest.mark.asyncio
async def test_footprint_exists_without_overwrite(tmp_path: Path) -> None:
    lib = tmp_path / "dup.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out1 = await tool.run(
        LibAddFootprintInput(lib_path=lib, footprint_name="X", pads=[_SMD_PAD])
    )
    assert out1.status == "ok"
    out2 = await tool.run(
        LibAddFootprintInput(lib_path=lib, footprint_name="X", pads=[_SMD_PAD])
    )
    assert out2.status == "footprint_exists"


@pytest.mark.asyncio
async def test_overwrite_replaces_file(tmp_path: Path) -> None:
    lib = tmp_path / "over.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    await tool.run(
        LibAddFootprintInput(
            lib_path=lib, footprint_name="X", value="v1", pads=[_SMD_PAD]
        )
    )
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="X",
            value="v2",
            pads=[_SMD_PAD],
            overwrite=True,
        )
    )
    assert out.status == "ok"
    assert out.overwrote is True

    doc = SexprDocument.from_path(lib / "X.kicad_mod")
    assert _property_value(doc.root, "Value") == "v2"


@pytest.mark.asyncio
async def test_through_hole_pad_gets_drill(tmp_path: Path) -> None:
    lib = tmp_path / "tht.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="PIN_THT",
            pads=[
                FootprintPad(
                    number="1", pad_type="thru_hole", shape="circle",
                    x=0.0, y=0.0, size_w=1.7, size_h=1.7, drill=1.0,
                )
            ],
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib / "PIN_THT.kicad_mod")
    pads = [c for c in doc.root.items if isinstance(c, SList) and c.head == "pad"]
    pad = pads[0]
    drill = pad.find("drill")
    assert drill is not None
    assert isinstance(drill.items[1], SAtom) and drill.items[1].text == "1"

    # THT pads use *.Cu / *.Mask.
    layers = pad.find("layers")
    assert layers is not None
    layer_texts = [
        item.text for item in layers.items[1:] if isinstance(item, SAtom)
    ]
    assert "*.Cu" in layer_texts


@pytest.mark.asyncio
async def test_smd_pad_picks_front_layers(tmp_path: Path) -> None:
    lib = tmp_path / "smd.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="SMD1",
            pads=[
                FootprintPad(
                    number="1", pad_type="smd", shape="roundrect",
                    x=0.0, y=0.0, size_w=1.0, size_h=0.6, layer="F.Cu",
                )
            ],
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib / "SMD1.kicad_mod")
    pad = next(
        c for c in doc.root.items if isinstance(c, SList) and c.head == "pad"
    )
    layers = pad.find("layers")
    assert layers is not None
    layer_texts = [
        item.text for item in layers.items[1:] if isinstance(item, SAtom)
    ]
    assert "F.Cu" in layer_texts
    assert "F.Paste" in layer_texts
    assert "F.Mask" in layer_texts


# -- invalid input --------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_empty_name(tmp_path: Path) -> None:
    lib = tmp_path / "lib.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(lib_path=lib, footprint_name="", pads=[_SMD_PAD])
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_tht_without_drill(tmp_path: Path) -> None:
    lib = tmp_path / "lib.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="BAD",
            pads=[
                FootprintPad(
                    number="1", pad_type="thru_hole", shape="circle",
                    x=0, y=0, size_w=1, size_h=1, drill=0.0,
                )
            ],
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_duplicate_pad_numbers(tmp_path: Path) -> None:
    lib = tmp_path / "lib.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="DUP",
            pads=[
                FootprintPad(
                    number="1", pad_type="smd", shape="rect",
                    x=0, y=0, size_w=1, size_h=1,
                ),
                FootprintPad(
                    number="1", pad_type="smd", shape="rect",
                    x=2, y=0, size_w=1, size_h=1,
                ),
            ],
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_mechanical_pads_zero_number_allowed(tmp_path: Path) -> None:
    """Thermal tabs / mechanical mounts use '0' or '' — don't treat as duplicate."""
    lib = tmp_path / "mech.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name="THERMAL",
            pads=[
                FootprintPad(
                    number="0", pad_type="smd", shape="rect",
                    x=0, y=0, size_w=2, size_h=2,
                ),
                FootprintPad(
                    number="0", pad_type="smd", shape="rect",
                    x=3, y=0, size_w=2, size_h=2,
                ),
            ],
        )
    )
    assert out.status == "ok"


def test_invalid_input_bad_pad_type() -> None:
    with pytest.raises(ValueError):
        FootprintPad(
            number="1", pad_type="telepathic", shape="rect",
            x=0, y=0, size_w=1, size_h=1,
        )


def test_invalid_input_bad_attribute() -> None:
    with pytest.raises(ValueError):
        LibAddFootprintInput(
            lib_path=Path("/tmp/x.pretty"),
            footprint_name="X",
            attributes=["telepathic"],
        )

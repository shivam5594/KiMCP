"""Unit tests for lib_add_symbol (M44)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.lib_add_symbol import (
    LibAddSymbolInput,
    LibAddSymbolTool,
    LibSymbolBodyRect,
    LibSymbolPin,
)


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off"}},
    )


def _find_symbol(root: SList, name: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) < 2:
            continue
        head = child.items[1]
        if isinstance(head, SAtom) and head.text == name:
            return child
    return None


def _find_sub_symbol(symbol: SList, name: str) -> SList | None:
    """Nested unit sub-symbol (e.g. 'NAME_1_1')."""
    for child in symbol.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        if len(child.items) < 2:
            continue
        head = child.items[1]
        if isinstance(head, SAtom) and head.text == name:
            return child
    return None


def _property_value(symbol: SList, name: str) -> str | None:
    for child in symbol.items:
        if not isinstance(child, SList) or child.head != "property":
            continue
        if len(child.items) < 3:
            continue
        key = child.items[1]
        if isinstance(key, SAtom) and key.text == name:
            val = child.items[2]
            return val.text if isinstance(val, SAtom) else None
    return None


_MINIMAL_PIN = LibSymbolPin(
    number="1", name="VCC", x=-5.08, y=0.0, length=2.54, angle=0.0
)


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibAddSymbolTool()
    assert tool.name == "lib_add_symbol"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstraps_missing_library(tmp_path: Path) -> None:
    """No library on disk → tool writes a fresh KiCAD 10 header + symbol."""
    lib = tmp_path / "custom.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="MY_CHIP",
            reference="U",
            value="MY_CHIP",
            description="Test part",
            pins=[
                LibSymbolPin(
                    number="1",
                    name="VCC",
                    x=-5.08,
                    y=0.0,
                    electrical_type="power_in",
                ),
                LibSymbolPin(
                    number="2", name="GND", x=-5.08, y=-2.54, electrical_type="power_in"
                ),
            ],
            body_rect=LibSymbolBodyRect(
                start_x=-2.54, start_y=2.54, end_x=2.54, end_y=-2.54
            ),
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    assert out.created_library is True
    assert out.pin_count == 2
    assert lib.is_file()

    doc = SexprDocument.from_path(lib)
    assert doc.top_head == "kicad_symbol_lib"
    # Version stamp present and KiCAD 10.
    assert doc.version == "20241209"
    # Symbol landed.
    sym = _find_symbol(doc.root, "MY_CHIP")
    assert sym is not None
    # Reference property carries the designator prefix.
    assert _property_value(sym, "Reference") == "U"
    # Description property is present (hidden, but present).
    assert _property_value(sym, "Description") == "Test part"


@pytest.mark.asyncio
async def test_appends_to_existing_library(tmp_path: Path) -> None:
    """Second call to same file appends rather than replacing."""
    lib = tmp_path / "custom.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))

    out1 = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="FIRST", pins=[_MINIMAL_PIN])
    )
    assert out1.status == "ok"
    assert out1.created_library is True

    out2 = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="SECOND", pins=[_MINIMAL_PIN])
    )
    assert out2.status == "ok"
    assert out2.created_library is False  # already existed
    assert out2.overwrote is False

    doc = SexprDocument.from_path(lib)
    assert _find_symbol(doc.root, "FIRST") is not None
    assert _find_symbol(doc.root, "SECOND") is not None


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    lib = tmp_path / "dry.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="DECOY",
            pins=[_MINIMAL_PIN],
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.created_library is False  # dry-run shouldn't touch disk
    # Tool may or may not report "append"/"replace" in note; key invariant
    # is that the file wasn't created.
    assert not lib.exists()


@pytest.mark.asyncio
async def test_overwrite_replaces_in_place(tmp_path: Path) -> None:
    lib = tmp_path / "over.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    # Seed with the original.
    await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            value="CHIP_v1",
            pins=[_MINIMAL_PIN],
        )
    )
    # Overwrite with a different value.
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            value="CHIP_v2",
            pins=[_MINIMAL_PIN],
            overwrite=True,
        )
    )
    assert out.status == "ok"
    assert out.overwrote is True

    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None
    assert _property_value(sym, "Value") == "CHIP_v2"

    # Still exactly one entry — overwrite must not duplicate.
    matches = [
        c
        for c in doc.root.items
        if isinstance(c, SList) and c.head == "symbol"
    ]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_canonical_attribute_flags_present(tmp_path: Path) -> None:
    """All three KiCAD 10 load-bearing flags must emit on every symbol."""
    lib = tmp_path / "flags.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib, symbol_name="CHIP", pins=[_MINIMAL_PIN]
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None

    for head, expected in (
        ("exclude_from_sim", "no"),
        ("in_bom", "yes"),
        ("on_board", "yes"),
    ):
        node = sym.find(head)
        assert node is not None, f"missing load-bearing flag {head!r}"
        assert isinstance(node.items[1], SAtom)
        assert node.items[1].text == expected


@pytest.mark.asyncio
async def test_power_flag_adds_power_marker(tmp_path: Path) -> None:
    lib = tmp_path / "pwr.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="+3V3",
            reference="#PWR",
            power=True,
            pins=[
                LibSymbolPin(
                    number="1",
                    name="+3V3",
                    x=0.0,
                    y=0.0,
                    length=0.0,
                    angle=90.0,
                    electrical_type="power_in",
                    hide=True,
                )
            ],
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "+3V3")
    assert sym is not None
    assert sym.find("power") is not None


@pytest.mark.asyncio
async def test_pin_node_shape(tmp_path: Path) -> None:
    """Pins land in a ``<name>_1_1`` sub-symbol with the expected shape."""
    lib = tmp_path / "pins.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            pins=[
                LibSymbolPin(
                    number="3",
                    name="SDA",
                    x=-5.08,
                    y=0.0,
                    length=2.54,
                    angle=0.0,
                    electrical_type="bidirectional",
                    shape="line",
                )
            ],
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None
    sub = _find_sub_symbol(sym, "CHIP_1_1")
    assert sub is not None
    pins = [c for c in sub.items if isinstance(c, SList) and c.head == "pin"]
    assert len(pins) == 1
    pin = pins[0]

    # Electrical type + shape positional atoms.
    assert isinstance(pin.items[1], SAtom) and pin.items[1].text == "bidirectional"
    assert isinstance(pin.items[2], SAtom) and pin.items[2].text == "line"

    # Number + name children.
    num = pin.find("number")
    assert num is not None
    assert isinstance(num.items[1], SAtom) and num.items[1].text == "3"
    name = pin.find("name")
    assert name is not None
    assert isinstance(name.items[1], SAtom) and name.items[1].text == "SDA"


@pytest.mark.asyncio
async def test_body_rect_emitted_in_0_1_sub_symbol(tmp_path: Path) -> None:
    lib = tmp_path / "rect.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            pins=[_MINIMAL_PIN],
            body_rect=LibSymbolBodyRect(
                start_x=-5.0, start_y=5.0, end_x=5.0, end_y=-5.0
            ),
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None
    graphics = _find_sub_symbol(sym, "CHIP_0_1")
    assert graphics is not None
    rect = graphics.find("rectangle")
    assert rect is not None


@pytest.mark.asyncio
async def test_keywords_and_fp_filters_emitted_when_set(tmp_path: Path) -> None:
    lib = tmp_path / "meta.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            keywords="sensor i2c temperature",
            footprint_filters=["QFN-16*", "WSON-16*"],
            pins=[_MINIMAL_PIN],
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None
    assert _property_value(sym, "ki_keywords") == "sensor i2c temperature"
    assert _property_value(sym, "ki_fp_filters") == "QFN-16* WSON-16*"


@pytest.mark.asyncio
async def test_property_at_has_explicit_zero_angle(tmp_path: Path) -> None:
    """Same load-bearing rule as sheet properties: (at X Y 0) with explicit angle."""
    lib = tmp_path / "angle.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="CHIP", pins=[_MINIMAL_PIN])
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(lib)
    sym = _find_symbol(doc.root, "CHIP")
    assert sym is not None
    for prop in sym.items:
        if not isinstance(prop, SList) or prop.head != "property":
            continue
        at = prop.find("at")
        assert at is not None
        assert len(at.items) == 4, (
            f"property at-node must carry explicit angle; got "
            f"{len(at.items) - 1} positional atoms"
        )


# -- conflict handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_symbol_exists_without_overwrite(tmp_path: Path) -> None:
    lib = tmp_path / "conflict.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="DUP", pins=[_MINIMAL_PIN])
    )
    out = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="DUP", pins=[_MINIMAL_PIN])
    )
    assert out.status == "symbol_exists"


# -- invalid input --------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_empty_name(tmp_path: Path) -> None:
    lib = tmp_path / "lib.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="", pins=[_MINIMAL_PIN])
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_wrong_suffix(tmp_path: Path) -> None:
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=tmp_path / "wrong.txt", symbol_name="A", pins=[_MINIMAL_PIN]
        )
    )
    assert out.status == "invalid_input"


@pytest.mark.asyncio
async def test_invalid_input_duplicate_pin_numbers(tmp_path: Path) -> None:
    lib = tmp_path / "dup.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="CHIP",
            pins=[
                LibSymbolPin(number="1", name="A", x=0, y=0),
                LibSymbolPin(number="1", name="B", x=0, y=2),
            ],
        )
    )
    assert out.status == "invalid_input"


def test_invalid_input_unknown_electrical_type() -> None:
    """Pydantic-level validation — unknown electrical raises."""
    with pytest.raises(ValueError):
        LibSymbolPin(number="1", name="A", x=0, y=0, electrical_type="telepathic")


def test_invalid_input_unknown_pin_shape() -> None:
    with pytest.raises(ValueError):
        LibSymbolPin(number="1", name="A", x=0, y=0, shape="holographic")


# -- schema errors --------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    lib = tmp_path / "broken.kicad_sym"
    lib.write_text("(kicad_symbol_lib (oops", encoding="utf-8")
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="A", pins=[_MINIMAL_PIN])
    )
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    lib = tmp_path / "wrong.kicad_sym"
    lib.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(lib_path=lib, symbol_name="A", pins=[_MINIMAL_PIN])
    )
    assert out.status == "invalid_schema"

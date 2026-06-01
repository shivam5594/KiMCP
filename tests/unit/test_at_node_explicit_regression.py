"""Regression tests for the ``at_node_explicit`` migration.

KiCAD 10's schematic load-parser is strict about the 3-atom ``(at X Y
angle)`` form for every ``(at ...)`` node inside a ``(symbol ...)``,
``(property ...)``, ``(label ...)``, ``(global_label ...)``,
``(hierarchical_label ...)``, or lib-symbol ``(pin ...)``. The original
``at_node()`` helper in ``kimcp.tools.builtin._sexpr_build`` elides the
angle atom when it's zero, producing ``(at X Y)`` — which the loader
rejects with ``need a number for 'text angle'`` and refuses to open
the schematic.

Every affected site was migrated to ``at_node_explicit()`` in the
conversation that landed `DEBUG_sch_erc_hierarchical_load_failure.md`.
This file locks the migration in so a future refactor can't silently
reintroduce ``at_node()`` on these sites.

The shape check is the same everywhere: the parsed ``(at ...)`` must
carry four items (the head ``at`` + three number atoms), and the third
atom (the angle payload) must equal ``"0"`` when the caller passed
``angle=0``.

If a future change swaps the serializer away from the textual
``"0"`` atom (e.g. a canonicalizer that renders ``0`` as ``0.0``),
these tests will need a small numeric-comparison tweak — the load
contract still cares only that three atoms are present.

Keep these **unit** tests. The per-tool e2e and the
``test_sch_{power,label,sheet}_cli_load`` integration tests cover
the real-kicad round trip; these tests just pin the bytes on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.lib_add_symbol import (
    LibAddSymbolInput,
    LibAddSymbolTool,
    LibSymbolPin,
)
from kimcp.tools.builtin.sch_add_label import SchAddLabelInput, SchAddLabelTool
from kimcp.tools.builtin.sch_add_power import SchAddPowerInput, SchAddPowerTool
from kimcp.tools.builtin.sch_add_sheet import SchAddSheetInput, SchAddSheetTool
from kimcp.tools.builtin.sch_add_symbol import SchAddSymbolInput, SchAddSymbolTool

# -- fixtures --------------------------------------------------------------


_SCH_EMPTY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

# Embeds Device:R_Small so SchAddSymbolTool can place an instance
# without the lib_symbol_not_found early-return.
_SCH_WITH_R_SMALL = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "deadbeef-dead-beef-dead-beefdeadbeef")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R_Small"
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "R"
\t\t\t\t(at 2.032 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(property "Value" "R_Small"
\t\t\t\t(at 0 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(symbol "R_Small_0_1"
\t\t\t\t(rectangle
\t\t\t\t\t(start -0.762 2.032)
\t\t\t\t\t(end 0.762 -2.032)))
\t\t\t(symbol "R_Small_1_1"
\t\t\t\t(pin passive line
\t\t\t\t\t(at 0 2.54 270)
\t\t\t\t\t(length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t\t(pin passive line
\t\t\t\t\t(at 0 -2.54 90)
\t\t\t\t\t(length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))))
"""


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}},
    )


def _write_sch(tmp_path: Path, body: str = _SCH_EMPTY, name: str = "board.kicad_sch") -> Path:
    sch = tmp_path / name
    sch.write_text(body, encoding="utf-8")
    return sch


# -- assertion helper ------------------------------------------------------


def _assert_three_atom_at(at_node: SList, *, expected_angle: str = "0") -> None:
    """Assert ``(at X Y angle)`` has four items and the angle atom matches.

    The load-parser bug manifests as a 3-item node — ``(at X Y)`` —
    because the serializer elided a zero angle. Four items (head ``at``
    + three number atoms) is the required shape. The third atom text
    check guards against a silent refactor that e.g. renders ``0.0``
    where KiCAD expects ``0``; update ``expected_angle`` if the
    serializer contract legitimately changes.
    """
    assert at_node.head == "at"
    assert len(at_node.items) == 4, (
        f"expected (at X Y angle) but got {len(at_node.items) - 1} coordinate atom(s): "
        f"{[i.text if isinstance(i, SAtom) else repr(i) for i in at_node.items]}"
    )
    angle_atom = at_node.items[3]
    assert isinstance(angle_atom, SAtom)
    assert angle_atom.text == expected_angle, (
        f"expected angle atom {expected_angle!r}, got {angle_atom.text!r}"
    )


def _walk_property_at_nodes(symbol: SList) -> list[SList]:
    """Return every ``(at ...)`` that's the positional anchor of a
    direct-child ``(property ...)`` under ``symbol`` — i.e. the sites
    the strict parser cares about. Nested lib_symbol units are handled
    by recursing into child ``(symbol ...)`` blocks.
    """
    out: list[SList] = []
    for child in symbol.items:
        if not isinstance(child, SList):
            continue
        if child.head == "property":
            at = child.find("at")
            if at is not None:
                out.append(at)
        elif child.head == "symbol":
            out.extend(_walk_property_at_nodes(child))
    return out


# -- sch_add_power ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_add_power_property_and_instance_ats_are_3atom(tmp_path: Path) -> None:
    """Every ``(at ...)`` the tool writes for a zero-angle power-port
    placement (auto-embedded lib_symbol properties, instance-level
    properties, and the symbol anchor itself) must be 3-atom."""
    sch = _write_sch(tmp_path)
    tool = SchAddPowerTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=100.0, at_y=50.0)
    )
    assert out.status == "ok", out.note

    doc = SexprDocument.from_path(sch)

    # Auto-embedded lib_symbol: property anchors all 3-atom.
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    power_lib = None
    for child in lib_symbols.items:
        if (
            isinstance(child, SList)
            and child.head == "symbol"
            and len(child.items) >= 2
            and isinstance(child.items[1], SAtom)
            and child.items[1].text == "power:GND"
        ):
            power_lib = child
            break
    assert power_lib is not None
    for at in _walk_property_at_nodes(power_lib):
        _assert_three_atom_at(at)

    # Instance-level: the top-level (symbol ...) appended to root.
    instance = None
    for child in doc.root.items:
        if (
            isinstance(child, SList)
            and child.head == "symbol"
            and child.find("uuid") is not None
        ):
            uid_node = child.find("uuid")
            if (
                uid_node is not None
                and len(uid_node.items) >= 2
                and isinstance(uid_node.items[1], SAtom)
                and uid_node.items[1].text == out.instance_uuid
            ):
                instance = child
                break
    assert instance is not None

    # Symbol-anchor (at X Y angle) on the instance.
    anchor = instance.find("at")
    assert anchor is not None
    _assert_three_atom_at(anchor)

    # Every property on the instance.
    for at in _walk_property_at_nodes(instance):
        _assert_three_atom_at(at)


# -- sch_add_symbol --------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_add_symbol_property_and_anchor_ats_are_3atom(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path, _SCH_WITH_R_SMALL)
    tool = SchAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSymbolInput(
            sch_path=sch,
            lib_id="Device:R_Small",
            reference="R1",
            value="10k",
            at_x=25.0,
            at_y=35.0,
        )
    )
    assert out.status == "ok", out.note

    doc = SexprDocument.from_path(sch)
    instance = None
    for child in doc.root.items:
        if (
            isinstance(child, SList)
            and child.head == "symbol"
            and child.find("lib_id") is not None
        ):
            lib_id = child.find("lib_id")
            if (
                lib_id is not None
                and len(lib_id.items) >= 2
                and isinstance(lib_id.items[1], SAtom)
                and lib_id.items[1].text == "Device:R_Small"
            ):
                instance = child
                break
    assert instance is not None

    anchor = instance.find("at")
    assert anchor is not None
    _assert_three_atom_at(anchor)
    for at in _walk_property_at_nodes(instance):
        _assert_three_atom_at(at)


# -- sch_add_label ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "head"),
    [
        ("local", "label"),
        ("global", "global_label"),
        ("hierarchical", "hierarchical_label"),
    ],
)
@pytest.mark.asyncio
async def test_sch_add_label_body_at_is_3atom(
    tmp_path: Path, kind: str, head: str
) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddLabelTool()
    tool.set_config(_cfg(tmp_path))
    out = await tool.run(
        SchAddLabelInput(
            sch_path=sch,
            text="NET_A",
            at_x=10.0,
            at_y=20.0,
            kind=kind,
        )
    )
    assert out.status == "ok", out.note

    doc = SexprDocument.from_path(sch)
    label_node = doc.root.find(head)
    assert label_node is not None, f"no ({head} ...) found in output"
    body_at = label_node.find("at")
    assert body_at is not None
    _assert_three_atom_at(body_at)

    # Global labels also carry an Intersheetrefs ``(property ...)`` whose
    # anchor sits at the label location — check that too.
    if kind == "global":
        for at in _walk_property_at_nodes(label_node):
            _assert_three_atom_at(at)


# -- sch_add_sheet ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_add_sheet_property_ats_are_3atom(tmp_path: Path) -> None:
    """The outer ``(sheet (at X Y))`` stays 2-atom (that's the safe
    context ``at_node()`` is documented for), but every property anchor
    under the sheet must be 3-atom — that's the site KiCAD 10 rejected
    in the bug this test guards."""
    sch = _write_sch(tmp_path)
    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=sch,
            sheet_name="Power",
            sheet_file="sheets/power.kicad_sch",
            at_x=30.0,
            at_y=30.0,
        )
    )
    assert out.status == "ok", out.note

    doc = SexprDocument.from_path(sch)
    sheet = doc.root.find("sheet")
    assert sheet is not None
    for at in _walk_property_at_nodes(sheet):
        _assert_three_atom_at(at)


# -- lib_add_symbol --------------------------------------------------------


@pytest.mark.asyncio
async def test_lib_add_symbol_property_and_pin_ats_are_3atom(tmp_path: Path) -> None:
    lib = tmp_path / "custom.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="MY_CHIP",
            reference="U",
            value="MY_CHIP",
            pins=[
                LibSymbolPin(
                    number="1",
                    name="VCC",
                    x=-5.08,
                    y=0.0,
                    length=2.54,
                    angle=0.0,
                ),
            ],
        )
    )
    assert out.status == "ok", out.note

    doc = SexprDocument.from_path(lib)

    # lib_add_symbol writes into the top ``(kicad_symbol_lib ...)``. Find
    # our new MY_CHIP entry.
    my_chip = None
    for child in doc.root.items:
        if (
            isinstance(child, SList)
            and child.head == "symbol"
            and len(child.items) >= 2
            and isinstance(child.items[1], SAtom)
            and child.items[1].text == "MY_CHIP"
        ):
            my_chip = child
            break
    assert my_chip is not None

    # Properties (Reference / Value / Footprint / Datasheet / Description).
    for at in _walk_property_at_nodes(my_chip):
        _assert_three_atom_at(at)

    # Pin anchors. The pin sits in the nested unit sub-symbol
    # ``MY_CHIP_1_1``; walk there and verify every ``(pin ... (at ...))``.
    unit_sub = None
    for child in my_chip.items:
        if (
            isinstance(child, SList)
            and child.head == "symbol"
            and len(child.items) >= 2
            and isinstance(child.items[1], SAtom)
            and child.items[1].text == "MY_CHIP_1_1"
        ):
            unit_sub = child
            break
    assert unit_sub is not None
    pin_nodes = [c for c in unit_sub.items if isinstance(c, SList) and c.head == "pin"]
    assert len(pin_nodes) == 1
    pin_at = pin_nodes[0].find("at")
    assert pin_at is not None
    _assert_three_atom_at(pin_at)

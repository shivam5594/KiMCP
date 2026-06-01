"""Unit tests for M18 ``sch_add_power``.

Covers the full status matrix + round-trip invariants:

* lib_symbol synthesis (auto-embed on first placement, reuse on second).
* Power-port instance shape: hidden Reference, visible Value, in_bom=no,
  single pin, correct ``(instances (project (path ...)))`` block.
* Preservation: pre-existing lib_symbols and a pre-embedded ``power:NET``
  must not be duplicated or mutated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_power import (
    SchAddPowerInput,
    SchAddPowerOutput,
    SchAddPowerTool,
    _build_power_instance,
    _build_power_lib_symbol,
    _find_power_instance_by_uuid,
)
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol

# -- fixtures --------------------------------------------------------------


_SCH_EMPTY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

_SCH_NO_TOP_UUID = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(paper "A4")
\t(lib_symbols))
"""

_SCH_NO_LIB_SYMBOLS = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4"))
"""

_PCB = """\
(kicad_pcb (version 20240108) (generator "pcbnew"))
"""


def _write(tmp_path: Path, body: str = _SCH_EMPTY) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _tool(snapshot_mode: str = "off") -> SchAddPowerTool:
    tool = SchAddPowerTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None}}))
    return tool


def _atom_text(node: SList, idx: int) -> str:
    a = node.items[idx]
    assert isinstance(a, SAtom)
    return a.text


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    tool = SchAddPowerTool()
    assert tool.name == "sch_add_power"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- preflight / input validation -----------------------------------------


@pytest.mark.asyncio
async def test_empty_net_name_rejected(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="", at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_input"
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=tmp_path / "nope.kicad_sch",
            net_name="GND",
            at_x=0.0,
            at_y=0.0,
        )
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_path_is_directory(tmp_path: Path) -> None:
    d = tmp_path / "sub.kicad_sch"
    d.mkdir()
    out = await _tool().run(
        SchAddPowerInput(sch_path=d, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_pcb"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PCB, encoding="utf-8")
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("(kicad_sch (oops", encoding="utf-8")
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "parse_failed"


@pytest.mark.asyncio
async def test_missing_top_uuid(tmp_path: Path) -> None:
    sch = _write(tmp_path, _SCH_NO_TOP_UUID)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "invalid_schema"
    assert out.note is not None
    assert "uuid" in out.note.lower()


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preserves_bytes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = sch.read_bytes()
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=sch,
            net_name="VCC",
            at_x=10.0,
            at_y=20.0,
            dry_run=True,
        )
    )
    assert out.status == "dry_run"
    assert out.instance_uuid is None
    assert out.lib_symbol_embedded is None
    assert out.lib_id == "power:VCC"
    assert out.net_name == "VCC"
    assert out.meta.snapshot_ref is None
    assert sch.read_bytes() == before


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_embeds_lib_symbol_on_first_placement(tmp_path: Path) -> None:
    """First placement of a canonical net (GND) must embed some lib_symbol
    with the ``(power)`` marker. Whether it was cloned from KiCAD's
    bundled ``power.kicad_sym`` (canonical) or synthesized depends on the
    host — both paths are valid here; more specific tests below pin each
    branch by monkeypatching the resolver."""
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=100.0, at_y=50.0)
    )
    assert out.status == "ok"
    assert out.lib_symbol_embedded is True
    assert out.lib_symbol_source in {"canonical", "synthesized"}
    assert out.instance_uuid is not None

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    lib = _find_lib_symbol(lib_symbols, "power:GND")
    assert lib is not None
    # (power) marker is what makes a symbol a power port — load-bearing
    # on both canonical and synthesized branches.
    assert lib.find("power") is not None


@pytest.mark.asyncio
async def test_reuses_existing_lib_symbol(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = _tool()
    # First call embeds.
    first = await tool.run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert first.lib_symbol_embedded is True

    # Second call must reuse — the lib_symbols block should still
    # contain exactly one entry for power:GND.
    second = await tool.run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=10.0, at_y=10.0)
    )
    assert second.status == "ok"
    assert second.lib_symbol_embedded is False
    assert second.lib_symbol_source == "preexisting"

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    matches = [
        child
        for child in lib_symbols.items
        if isinstance(child, SList)
        and child.head == "symbol"
        and len(child.items) >= 2
        and isinstance(child.items[1], SAtom)
        and child.items[1].text == "power:GND"
    ]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_creates_lib_symbols_if_absent(tmp_path: Path) -> None:
    """Schematic with no ``(lib_symbols)`` block still works — we append one."""
    sch = _write(tmp_path, _SCH_NO_LIB_SYMBOLS)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="+3V3", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert doc.root.find("lib_symbols") is not None


@pytest.mark.asyncio
async def test_instance_has_correct_shape(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=sch, net_name="VCC", at_x=50.0, at_y=30.0
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_power_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None

    # lib_id points into power:.
    lib_id = instance.find("lib_id")
    assert lib_id is not None and _atom_text(lib_id, 1) == "power:VCC"
    # in_bom=no is the power-symbol convention.
    in_bom = instance.find("in_bom")
    assert in_bom is not None and _atom_text(in_bom, 1) == "no"
    # Value property visible with net name.
    value_prop = None
    ref_prop = None
    for child in instance.items:
        if (
            isinstance(child, SList)
            and child.head == "property"
            and len(child.items) >= 3
        ):
            name_atom = child.items[1]
            if isinstance(name_atom, SAtom):
                if name_atom.text == "Value":
                    value_prop = child
                elif name_atom.text == "Reference":
                    ref_prop = child
    assert value_prop is not None
    assert isinstance(value_prop.items[2], SAtom)
    assert value_prop.items[2].text == "VCC"
    # Value is visible — its effects node has no (hide yes).
    value_effects = value_prop.find("effects")
    assert value_effects is not None
    assert value_effects.find("hide") is None

    # Reference property is hidden.
    assert ref_prop is not None
    ref_effects = ref_prop.find("effects")
    assert ref_effects is not None
    hide_node = ref_effects.find("hide")
    assert hide_node is not None and _atom_text(hide_node, 1) == "yes"


@pytest.mark.asyncio
async def test_instance_at_coordinates_and_angle(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=sch, net_name="GND", at_x=30.0, at_y=40.0, angle=90.0
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_power_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None
    at = instance.find("at")
    assert at is not None
    assert _atom_text(at, 1) == "30"
    assert _atom_text(at, 2) == "40"
    assert _atom_text(at, 3) == "90"


@pytest.mark.asyncio
async def test_instance_has_single_pin(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_power_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None
    pins = [
        child
        for child in instance.items
        if isinstance(child, SList) and child.head == "pin"
    ]
    assert len(pins) == 1
    # Pin number "1".
    assert _atom_text(pins[0], 1) == "1"


@pytest.mark.asyncio
async def test_instance_reference_override(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=sch,
            net_name="+5V",
            at_x=0.0,
            at_y=0.0,
            reference="#PWR042",
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_power_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None
    # Both the visible property and the instances-block reference must agree.
    for child in instance.items:
        if (
            isinstance(child, SList)
            and child.head == "property"
            and len(child.items) >= 3
        ):
            name_atom = child.items[1]
            if isinstance(name_atom, SAtom) and name_atom.text == "Reference":
                value_atom = child.items[2]
                assert isinstance(value_atom, SAtom)
                assert value_atom.text == "#PWR042"
                break
    instances = instance.find("instances")
    assert instances is not None
    path = None
    for sub in instances.walk():
        if isinstance(sub, SList) and sub.head == "path":
            path = sub
            break
    assert path is not None
    ref = path.find("reference")
    assert ref is not None and _atom_text(ref, 1) == "#PWR042"


# -- multiple nets --------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_different_nets_each_embed_own_lib_symbol(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    tool = _tool()
    uuids: list[str | None] = []
    for net in ("GND", "VCC", "+3V3"):
        out = await tool.run(
            SchAddPowerInput(sch_path=sch, net_name=net, at_x=0.0, at_y=0.0)
        )
        assert out.status == "ok"
        assert out.lib_symbol_embedded is True
        uuids.append(out.instance_uuid)
    assert len({u for u in uuids if u is not None}) == 3

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    for net in ("GND", "VCC", "+3V3"):
        assert _find_lib_symbol(lib_symbols, f"power:{net}") is not None


# -- preservation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_top_uuid_unchanged(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    before = SexprDocument.from_path(sch)
    before_uuid = before.root.find("uuid")
    assert before_uuid is not None

    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
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
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_copy(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await _tool(snapshot_mode="copy").run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.meta.snapshot_ref is not None
    assert out.meta.snapshot_ref.startswith("copy:")


@pytest.mark.asyncio
async def test_no_config_still_writes(tmp_path: Path) -> None:
    sch = _write(tmp_path)
    out = await SchAddPowerTool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
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
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "write_failed"


# -- canonical-vs-synthesized branch tests --------------------------------
#
# These lock the canonical-lib preference path in place by monkeypatching
# the system-symbol-lib resolver. Without this, the behavior would be
# host-dependent (clean CI without KiCAD installed → synthesis; dev box
# with KiCAD → canonical), and regressions could slip past unnoticed.


@pytest.mark.asyncio
async def test_canonical_source_when_bundled_library_has_net(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the resolver at a fake ``power.kicad_sym`` we control, verify
    the embedded lib_symbol is a clone of our fake entry and that
    ``lib_symbol_source == 'canonical'`` with no fallback warning."""
    # Minimal kicad_symbol_lib with just GND. Shape mirrors the real
    # bundled library closely enough that ``_find_symbol_in_lib`` finds
    # it and ``_clone_and_qualify`` produces a valid top-level entry.
    fake_lib = tmp_path / "fake_power.kicad_sym"
    fake_lib.write_text(
        '(kicad_symbol_lib (version 20240108) (generator "eeschema")\n'
        '\t(symbol "GND"\n'
        '\t\t(power)\n'
        '\t\t(in_bom yes)\n'
        '\t\t(on_board yes)\n'
        '\t\t(property "Reference" "#PWR"\n'
        '\t\t\t(at 0 0 0) (effects (font (size 1.27 1.27))))\n'
        '\t\t(property "Value" "GND"\n'
        '\t\t\t(at 0 -3.81 0) (effects (font (size 1.27 1.27))))\n'
        '\t\t(symbol "GND_0_1"\n'
        '\t\t\t(polyline (pts (xy 0 0) (xy 0 -1.27))\n'
        '\t\t\t\t(stroke (width 0) (type default))\n'
        '\t\t\t\t(fill (type none))))\n'
        '\t\t(symbol "GND_1_1"\n'
        '\t\t\t(pin power_in line\n'
        '\t\t\t\t(at 0 0 270) (length 0) (hide yes)\n'
        '\t\t\t\t(name "GND" (effects (font (size 1.27 1.27))))\n'
        '\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))))))\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "kimcp.tools.builtin.sch_add_power.resolve_system_symbol_lib",
        lambda name: fake_lib if name == "power" else None,
    )

    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.lib_symbol_embedded is True
    assert out.lib_symbol_source == "canonical"
    # No warning — canonical embed is the clean path.
    assert out.meta.warnings == []

    # The embedded lib_symbol must carry our fake's signature shape —
    # specifically ``in_bom=yes`` from our fake, proving it was cloned
    # from the library rather than synthesized.
    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    lib = _find_lib_symbol(lib_symbols, "power:GND")
    assert lib is not None
    in_bom = lib.find("in_bom")
    assert in_bom is not None and _atom_text(in_bom, 1) == "yes"


@pytest.mark.asyncio
async def test_synthesized_source_when_net_missing_from_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundled library exists but lacks the requested net (custom rail
    like ``+VIN_IN``). Must fall back to synthesis, set
    ``lib_symbol_source == 'synthesized'``, and emit a user-actionable
    warning citing the missing net name."""
    fake_lib = tmp_path / "fake_power.kicad_sym"
    fake_lib.write_text(
        '(kicad_symbol_lib (version 20240108) (generator "eeschema")\n'
        '\t(symbol "GND" (power)))\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "kimcp.tools.builtin.sch_add_power.resolve_system_symbol_lib",
        lambda name: fake_lib if name == "power" else None,
    )

    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="+VIN_IN", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.lib_symbol_embedded is True
    assert out.lib_symbol_source == "synthesized"
    # Warning must mention the missing net name so callers see the
    # actionable hint rather than a generic "fallback" message.
    assert any("+VIN_IN" in w for w in out.meta.warnings)

    # Synthesis-specific signature: our synthetic stand-in writes
    # ``in_bom=no``, distinguishing the synthesis branch from the
    # canonical branch on-disk.
    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    lib = _find_lib_symbol(lib_symbols, "power:+VIN_IN")
    assert lib is not None
    in_bom = lib.find("in_bom")
    assert in_bom is not None and _atom_text(in_bom, 1) == "no"


@pytest.mark.asyncio
async def test_synthesized_source_when_bundled_library_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolver returns None (KiCAD not installed). Must fall back to
    synthesis and warn, without raising."""
    monkeypatch.setattr(
        "kimcp.tools.builtin.sch_add_power.resolve_system_symbol_lib",
        lambda name: None,
    )

    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0)
    )
    assert out.status == "ok"
    assert out.lib_symbol_source == "synthesized"
    assert any("power.kicad_sym" in w for w in out.meta.warnings)


@pytest.mark.asyncio
async def test_dry_run_probes_canonical_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run must look ahead and report whether the real call would use
    a canonical or synthesized entry, so callers can preview."""
    monkeypatch.setattr(
        "kimcp.tools.builtin.sch_add_power.resolve_system_symbol_lib",
        lambda name: None,
    )
    sch = _write(tmp_path)
    out = await _tool().run(
        SchAddPowerInput(
            sch_path=sch, net_name="GND", at_x=0.0, at_y=0.0, dry_run=True
        )
    )
    assert out.status == "dry_run"
    # Note should spell out which branch would fire — users reading
    # the preview shouldn't have to guess.
    assert out.note is not None and "synthesized" in out.note


# -- helper-level tests ---------------------------------------------------


def test_build_power_lib_symbol_has_power_marker() -> None:
    lib = _build_power_lib_symbol("GND")
    assert lib.head == "symbol"
    # Lib id is power:<net>.
    assert isinstance(lib.items[1], SAtom)
    assert lib.items[1].text == "power:GND"
    # The (power) flag list.
    power = lib.find("power")
    assert power is not None


def test_build_power_lib_symbol_has_pin_names_offset_zero() -> None:
    lib = _build_power_lib_symbol("VCC")
    pin_names = lib.find("pin_names")
    assert pin_names is not None
    offset = pin_names.find("offset")
    assert offset is not None
    assert _atom_text(offset, 1) == "0"


def test_build_power_lib_symbol_contains_graphic_and_pin_units() -> None:
    lib = _build_power_lib_symbol("GND")
    child_symbols = [
        child
        for child in lib.items
        if isinstance(child, SList) and child.head == "symbol"
    ]
    # Two nested units: _0_1 (graphics) and _1_1 (pin).
    assert len(child_symbols) == 2
    assert isinstance(child_symbols[0].items[1], SAtom)
    assert child_symbols[0].items[1].text == "GND_0_1"
    assert isinstance(child_symbols[1].items[1], SAtom)
    assert child_symbols[1].items[1].text == "GND_1_1"


def test_build_power_instance_has_in_bom_no() -> None:
    node = _build_power_instance(
        net_name="GND",
        reference="#PWR?",
        at_x=0.0,
        at_y=0.0,
        angle=0.0,
        instance_uuid="iu",
        pin_uuid="pu",
        project_name="proj",
        top_uuid="top",
    )
    in_bom = node.find("in_bom")
    assert in_bom is not None
    assert _atom_text(in_bom, 1) == "no"


def test_build_power_instance_reference_is_hidden() -> None:
    node = _build_power_instance(
        net_name="GND",
        reference="#PWR?",
        at_x=0.0,
        at_y=0.0,
        angle=0.0,
        instance_uuid="iu",
        pin_uuid="pu",
        project_name="proj",
        top_uuid="top",
    )
    # Find the Reference property.
    ref_prop = None
    for child in node.items:
        if (
            isinstance(child, SList)
            and child.head == "property"
            and len(child.items) >= 2
            and isinstance(child.items[1], SAtom)
            and child.items[1].text == "Reference"
        ):
            ref_prop = child
            break
    assert ref_prop is not None
    effects = ref_prop.find("effects")
    assert effects is not None
    hide = effects.find("hide")
    assert hide is not None
    assert _atom_text(hide, 1) == "yes"


def test_output_defaults() -> None:
    out = SchAddPowerOutput(status="dry_run")
    assert out.instance_uuid is None
    assert out.net_name is None
    assert out.lib_id is None
    assert out.lib_symbol_embedded is None


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddPowerTool()
    assert tool._config is None
    cfg = load_config(session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}})
    tool.set_config(cfg)
    assert tool._config is cfg


# -- at-angle explicitness (regression guard) -----------------------------
#
# The MK-II Controller Board hit ``need a number for 'text angle'`` at
# line 2226 because ``_power_property_lib`` emitted 2-atom ``(at X Y)``
# via the old ``at_node(x, y, 0.0)`` call. KiCAD 10's strict parser
# requires the 3-atom form inside ``lib_symbols`` properties AND on the
# instance-level property + symbol-position nodes. Pin these shapes so
# a future refactor can't regress to the 2-atom form.


def _every_property_at_has_three_coords(root: SList) -> list[SList]:
    """Walk the tree; return any ``(at ...)`` node with <4 items that
    sits directly under a ``(property ...)`` parent."""
    offenders: list[SList] = []

    def walk(node: SList) -> None:
        for child in node.items:
            if not isinstance(child, SList):
                continue
            if child.head == "property":
                for grand in child.items:
                    if (
                        isinstance(grand, SList)
                        and grand.head == "at"
                        and len(grand.items) < 4
                    ):
                        offenders.append(grand)
            walk(child)

    walk(root)
    return offenders


def test_build_power_lib_symbol_property_at_nodes_have_explicit_angle() -> None:
    """Lib-symbol ``power:GND`` properties must emit ``(at X Y 0)``."""
    lib = _build_power_lib_symbol("GND")
    offenders = _every_property_at_has_three_coords(lib)
    assert offenders == [], (
        f"found {len(offenders)} property (at ...) nodes missing the "
        f"angle atom — KiCAD 10 will reject this at load with "
        f"'need a number for text angle'. offenders={offenders!r}"
    )


def test_build_power_instance_property_at_nodes_have_explicit_angle() -> None:
    """Instance-level properties must emit ``(at X Y 0)`` too."""
    instance = _build_power_instance(
        net_name="GND",
        reference="#PWR01",
        at_x=50.0,
        at_y=50.0,
        angle=0.0,
        instance_uuid="11111111-2222-3333-4444-555555555555",
        pin_uuid="66666666-7777-8888-9999-aaaaaaaaaaaa",
        project_name="proj",
        top_uuid="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
    )
    offenders = _every_property_at_has_three_coords(instance)
    assert offenders == [], f"offenders={offenders!r}"


def test_build_power_instance_symbol_position_has_explicit_angle() -> None:
    """The ``(symbol (at X Y 0))`` top-level at-node needs the angle."""
    instance = _build_power_instance(
        net_name="GND",
        reference="#PWR01",
        at_x=50.0,
        at_y=50.0,
        angle=0.0,
        instance_uuid="11111111-2222-3333-4444-555555555555",
        pin_uuid="66666666-7777-8888-9999-aaaaaaaaaaaa",
        project_name="proj",
        top_uuid="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
    )
    # Direct-child (at ...) of the instance — not inside a property.
    at_node = next(
        c for c in instance.items if isinstance(c, SList) and c.head == "at"
    )
    assert len(at_node.items) == 4, (
        "instance-level (symbol (at X Y)) without explicit angle will "
        "break KiCAD 10's schematic loader."
    )

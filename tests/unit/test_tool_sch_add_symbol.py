"""Unit tests for ``SchAddSymbolTool`` (M14).

Pins the mutation shape, the status matrix, and the round-trip property
that matters most: after ``sch_add_symbol`` runs, the schematic must
reparse cleanly and the new instance must be locatable in the tree.

Fixtures live inline — the schematic body is small enough to keep the
lib_symbol, pins, and top-level uuid visible in one place. A shared
fixture would hide the exact structure each test depends on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kimcp.config import Config, SafetyCfg, load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_symbol import (
    SchAddSymbolInput,
    SchAddSymbolOutput,
    SchAddSymbolTool,
    _build_symbol_instance,
    _derive_project_name,
    _extract_pin_numbers,
    _find_lib_symbol,
)

# -- fixtures --------------------------------------------------------------


# Minimal but realistic schematic: top-level uuid present, lib_symbols
# section embeds a Device:R_Small entry with two pins inside its unit-1
# sub-symbol. Mirrors the shape KiCAD 9.x eeschema emits.
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

_SCH_WITHOUT_TOP_UUID = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R_Small"
\t\t\t(symbol "R_Small_1_1"
\t\t\t\t(pin passive line
\t\t\t\t\t(at 0 2.54 270)
\t\t\t\t\t(length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))))))
"""

_SCH_EMPTY_LIB_SYMBOLS = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "deadbeef-dead-beef-dead-beefdeadbeef")
\t(paper "A4")
\t(lib_symbols))
"""

_PCB_NOT_SCH = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew"))
"""


def _write_sch(tmp_path: Path, body: str = _SCH_WITH_R_SMALL) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


def _config_with_snapshot(mode: str) -> Config:
    """Config that only touches safety.snapshot_mode.

    M12 uses the same pattern — we need a Config to flip snapshot_mode
    without the tool stamping an unrelated backend call.
    """
    return load_config(
        session_overrides={
            "safety": {"snapshot_mode": mode, "grid_snap_mm": None}
        },
    )


async def _run(
    tool: SchAddSymbolTool, **kwargs: Any
) -> SchAddSymbolOutput:
    return await tool.run(SchAddSymbolInput(**kwargs))


def _happy_kwargs(sch: Path, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sch_path": sch,
        "lib_id": "Device:R_Small",
        "reference": "R1",
        "value": "10k",
        "at_x": 100.0,
        "at_y": 50.0,
    }
    base.update(overrides)
    return base


# -- preflight validation --------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_when_missing(tmp_path: Path) -> None:
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(tmp_path / "nope.kicad_sch"))
    assert out.status == "sch_not_found"
    assert out.sch_path is None
    assert out.instance_uuid is None


@pytest.mark.asyncio
async def test_sch_not_found_when_directory(tmp_path: Path) -> None:
    d = tmp_path / "looks.kicad_sch"
    d.mkdir()
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(d))
    assert out.status == "sch_not_found"
    assert "not a regular file" in (out.note or "")


@pytest.mark.asyncio
async def test_sch_not_found_when_wrong_suffix(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_PCB_NOT_SCH, encoding="utf-8")
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(pcb))
    assert out.status == "sch_not_found"
    assert "not a .kicad_sch" in (out.note or "")


@pytest.mark.asyncio
async def test_invalid_schema_when_wrong_top_head(tmp_path: Path) -> None:
    """A file with .kicad_sch suffix but (kicad_pcb ...) head is rejected
    at the shape-check step — not at the suffix check — because the
    suffix can lie."""
    fake = tmp_path / "spoof.kicad_sch"
    fake.write_text(_PCB_NOT_SCH, encoding="utf-8")
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(fake))
    assert out.status == "invalid_schema"
    assert "kicad_pcb" in (out.note or "")


@pytest.mark.asyncio
async def test_parse_failed_on_bad_bytes(tmp_path: Path) -> None:
    sch = tmp_path / "bad.kicad_sch"
    sch.write_text("(kicad_sch this is not valid", encoding="utf-8")
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "parse_failed"
    assert out.sch_path == str(sch.resolve())


@pytest.mark.asyncio
async def test_invalid_schema_when_top_uuid_missing(tmp_path: Path) -> None:
    """The instances block needs the root uuid — reject if it's absent."""
    sch = _write_sch(tmp_path, _SCH_WITHOUT_TOP_UUID)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "invalid_schema"
    assert "uuid" in (out.note or "").lower()


# -- lib_symbol_not_found path --------------------------------------------


@pytest.mark.asyncio
async def test_lib_symbol_not_found_when_not_embedded(tmp_path: Path) -> None:
    """Requesting a symbol not in lib_symbols returns the dedicated status,
    not a generic parse error. Tells the caller to embed first."""
    sch = _write_sch(tmp_path, _SCH_EMPTY_LIB_SYMBOLS)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "lib_symbol_not_found"
    assert "Device:R_Small" in (out.note or "")


@pytest.mark.asyncio
async def test_lib_symbol_not_found_matches_exact_lib_id(tmp_path: Path) -> None:
    """Partial-prefix lib_ids don't match — 'Device:R' shouldn't find
    'Device:R_Small'."""
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch, lib_id="Device:R"))
    assert out.status == "lib_symbol_not_found"


# -- dry_run path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_does_not_touch_file(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch, dry_run=True))

    assert out.status == "dry_run"
    assert out.reference == "R1"
    # No UUID assigned on dry_run — keeps dry_run idempotent so
    # repeated planning doesn't drift.
    assert out.instance_uuid is None
    assert sch.read_bytes() == before
    # Note mentions the lib_id, reference, and pin count (2 for R_Small).
    note = out.note or ""
    assert "Device:R_Small" in note
    assert "R1" in note
    assert "2 pin" in note


# -- happy path: mutation lands on disk -----------------------------------


@pytest.mark.asyncio
async def test_ok_appends_symbol_instance_and_persists(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))

    out = await _run(tool, **_happy_kwargs(sch))

    assert out.status == "ok"
    assert out.sch_path == str(sch.resolve())
    assert out.reference == "R1"
    assert out.instance_uuid is not None
    # meta.snapshot_ref is 'disabled' because we passed snapshot_mode="off".
    assert out.meta.snapshot_ref == "disabled"

    # Round-trip: reparse the written file and find the new instance by UUID.
    doc = SexprDocument.from_path(sch)
    instance = _find_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None, "new instance not found in re-parsed schematic"

    # Shape checks on the synthesized block.
    lib_id = instance.find("lib_id")
    assert lib_id is not None
    assert isinstance(lib_id.items[1], SAtom)
    assert lib_id.items[1].text == "Device:R_Small"

    at = instance.find("at")
    assert at is not None
    # We passed at_x=100 (integer-valued float) — should serialize
    # without a decimal, matching KiCAD's own formatting.
    assert _atom_text(at, 1) == "100"
    assert _atom_text(at, 2) == "50"

    # Reference property mirrors the reference field on the instance.
    ref_prop = _find_property(instance, "Reference")
    assert ref_prop is not None
    assert _atom_text(ref_prop, 2) == "R1"

    value_prop = _find_property(instance, "Value")
    assert value_prop is not None
    assert _atom_text(value_prop, 2) == "10k"


@pytest.mark.asyncio
async def test_ok_emits_one_pin_entry_per_unique_pin(tmp_path: Path) -> None:
    """Resistor has two pins — the instance must have exactly two
    ``(pin "N" (uuid ...))`` entries, one per unique number, each with
    its own UUID.
    """
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))

    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "ok"

    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None

    pins = [
        child
        for child in instance.items
        if isinstance(child, SList) and child.head == "pin"
    ]
    assert len(pins) == 2
    numbers = [_atom_text(pin, 1) for pin in pins]
    assert numbers == ["1", "2"]

    # Each pin has a uuid that's unique — not accidentally sharing one.
    uuids = {_nested_atom_text(pin, "uuid") for pin in pins}
    assert len(uuids) == 2
    assert None not in uuids


@pytest.mark.asyncio
async def test_ok_instance_binds_to_top_level_uuid(tmp_path: Path) -> None:
    """The ``(instances (project ...) (path "/<uuid>" ...))`` path must
    reference the schematic's top-level UUID — otherwise ERC/BOM loses
    the component on re-open."""
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch, reference="R42", unit=1))
    assert out.status == "ok"

    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None

    instances_block = instance.find("instances")
    assert instances_block is not None
    project = instances_block.find("project")
    assert project is not None
    path = project.find("path")
    assert path is not None
    # path leads with a quoted "/<top_uuid>".
    assert _atom_text(path, 1) == "/deadbeef-dead-beef-dead-beefdeadbeef"
    # reference inside the path matches the input.
    path_ref = path.find("reference")
    assert path_ref is not None
    assert _atom_text(path_ref, 1) == "R42"


@pytest.mark.asyncio
async def test_ok_places_footprint_when_provided(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(
        tool,
        **_happy_kwargs(
            sch, footprint="Resistor_SMD:R_0603_1608Metric"
        ),
    )
    assert out.status == "ok"

    doc = SexprDocument.from_path(sch)
    assert out.instance_uuid is not None
    instance = _find_instance_by_uuid(doc.root, out.instance_uuid)
    assert instance is not None
    footprint_prop = _find_property(instance, "Footprint")
    assert footprint_prop is not None
    assert _atom_text(footprint_prop, 2) == "Resistor_SMD:R_0603_1608Metric"


@pytest.mark.asyncio
async def test_ok_preserves_existing_top_level_nodes(tmp_path: Path) -> None:
    """Appending must not mutate pre-existing (uuid), (paper), or
    (lib_symbols) — only grow the root by the new (symbol ...)."""
    sch = _write_sch(tmp_path)

    # Count top-level children before vs after.
    before = SexprDocument.from_path(sch)
    before_count = len(before.root.items)

    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "ok"

    after = SexprDocument.from_path(sch)
    after_count = len(after.root.items)
    assert after_count == before_count + 1

    # Top uuid unchanged.
    before_uuid = before.root.find("uuid")
    after_uuid = after.root.find("uuid")
    assert before_uuid is not None and after_uuid is not None
    assert _atom_text(before_uuid, 1) == _atom_text(after_uuid, 1)


@pytest.mark.asyncio
async def test_ok_multiple_calls_produce_distinct_uuids(tmp_path: Path) -> None:
    """Successive calls to add a second instance must allocate a fresh
    instance UUID, not reuse the first — guards against accidental
    module-level state."""
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))

    first = await _run(tool, **_happy_kwargs(sch, reference="R1"))
    second = await _run(
        tool, **_happy_kwargs(sch, reference="R2", at_x=120.0)
    )
    assert first.status == "ok"
    assert second.status == "ok"
    assert first.instance_uuid != second.instance_uuid

    doc = SexprDocument.from_path(sch)
    # Both instances must be present after the second write.
    assert _find_instance_by_uuid(doc.root, first.instance_uuid) is not None  # type: ignore[arg-type]
    assert _find_instance_by_uuid(doc.root, second.instance_uuid) is not None  # type: ignore[arg-type]


# -- snapshot plumbing -----------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mode_off_surfaces_disabled(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("off"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "ok"
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_mode_copy_creates_copy_snapshot(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool(_config_with_snapshot("copy"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "ok"
    snap_ref = out.meta.snapshot_ref
    assert isinstance(snap_ref, str)
    assert snap_ref.startswith("copy:")
    snap_dir = Path(snap_ref[len("copy:") :])
    assert snap_dir.is_dir()
    # The original schematic (pre-mutation) lives inside the snapshot.
    copied = snap_dir / "board.kicad_sch"
    assert copied.is_file()
    # Snapshot content is the *original* bytes, not the post-mutation
    # bytes — snapshot runs before save.
    assert copied.read_text(encoding="utf-8") == _SCH_WITH_R_SMALL


@pytest.mark.asyncio
async def test_no_config_defaults_to_git_snapshot_mode(tmp_path: Path) -> None:
    """Tool used standalone (no server wiring) falls back to 'git' mode,
    which auto-falls-back to 'copy' in a non-git dir. Same as M12."""
    sch = _write_sch(tmp_path)
    tool = SchAddSymbolTool()  # no config
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "ok"
    snap_ref = out.meta.snapshot_ref
    assert isinstance(snap_ref, str)
    # Not a git repo → copy fallback.
    assert snap_ref.startswith("copy:")


# -- write failure translation --------------------------------------------


@pytest.mark.asyncio
async def test_write_failed_when_save_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a serialize-time failure and confirm we surface
    ``write_failed`` with snapshot_ref preserved."""
    sch = _write_sch(tmp_path)

    def _boom(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("synthetic serializer failure")

    monkeypatch.setattr(
        "kimcp.sexpr.document.serialize", _boom
    )

    tool = SchAddSymbolTool(_config_with_snapshot("copy"))
    out = await _run(tool, **_happy_kwargs(sch))
    assert out.status == "write_failed"
    assert out.reference == "R1"
    # Snapshot already ran before the save failure.
    assert isinstance(out.meta.snapshot_ref, str)
    assert out.meta.snapshot_ref.startswith("copy:")
    # File untouched on disk — save() bails before rename.
    assert sch.read_text(encoding="utf-8") == _SCH_WITH_R_SMALL


# -- DI / config plumbing -------------------------------------------------


def test_set_config_updates_internal_reference() -> None:
    tool = SchAddSymbolTool()
    assert tool._config is None
    cfg = Config(safety=SafetyCfg(snapshot_mode="off", grid_snap_mm=None))
    tool.set_config(cfg)
    assert tool._config is cfg


# -- helper-function pins --------------------------------------------------


def test_extract_pin_numbers_returns_unique_in_order() -> None:
    """Two pin entries in nested unit blocks → ['1', '2'], not duplicated."""
    doc = SexprDocument.from_bytes(Path("x.kicad_sch"), _SCH_WITH_R_SMALL.encode())
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    lib_symbol = _find_lib_symbol(lib_symbols, "Device:R_Small")
    assert lib_symbol is not None
    assert _extract_pin_numbers(lib_symbol) == ["1", "2"]


def test_find_lib_symbol_returns_none_when_missing() -> None:
    doc = SexprDocument.from_bytes(
        Path("x.kicad_sch"), _SCH_EMPTY_LIB_SYMBOLS.encode()
    )
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    assert _find_lib_symbol(lib_symbols, "Device:R_Small") is None


def test_derive_project_name_finds_neighboring_pro(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.touch()
    (tmp_path / "MyProject.kicad_pro").write_text("{}", encoding="utf-8")
    assert _derive_project_name(sch) == "MyProject"


def test_derive_project_name_empty_when_no_pro(tmp_path: Path) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.touch()
    assert _derive_project_name(sch) == ""


def test_build_symbol_instance_roundtrips_through_document(tmp_path: Path) -> None:
    """Synthesized instance + host schematic must reparse cleanly.

    Exercises the SexprDocument.save round-trip check in isolation from
    the tool: this is the load-bearing property that lets us trust any
    future schematic-mutation tool built on the same primitives.
    """
    sch = _write_sch(tmp_path)
    doc = SexprDocument.from_path(sch)

    symbol = _build_symbol_instance(
        lib_id="Device:R_Small",
        reference="R1",
        value="4.7k",
        at_x=100.0,
        at_y=50.0,
        angle=90.0,
        footprint="",
        unit=1,
        instance_uuid="11111111-2222-3333-4444-555555555555",
        pin_uuids={"1": "pin-1-uuid", "2": "pin-2-uuid"},
        project_name="TestProject",
        top_uuid="deadbeef-dead-beef-dead-beefdeadbeef",
    )
    doc.root.append(symbol)
    doc.save()

    reparsed = SexprDocument.from_path(sch)
    found = _find_instance_by_uuid(
        reparsed.root, "11111111-2222-3333-4444-555555555555"
    )
    assert found is not None
    # Angle round-trips as a third atom on (at ...).
    at = found.find("at")
    assert at is not None
    assert len(at.items) == 4
    assert _atom_text(at, 3) == "90"


# -- local helpers ---------------------------------------------------------


def _atom_text(node: SList, idx: int) -> str:
    """Return the text of the atom at ``idx`` on ``node``.

    Small helper that narrows ``SAtom | SList`` to ``SAtom`` cleanly —
    mirrors the M12 ``_atom_at`` pattern. Without it every assertion
    would need a cast for mypy.
    """
    atom = node.items[idx]
    assert isinstance(atom, SAtom), f"expected atom at index {idx}, got {type(atom)}"
    return atom.text


def _nested_atom_text(parent: SList, head: str) -> str | None:
    """Return the text of ``(head "value")`` atom child of ``parent``."""
    node = parent.find(head)
    if node is None or len(node.items) < 2:
        return None
    atom = node.items[1]
    return atom.text if isinstance(atom, SAtom) else None


def _find_property(instance: SList, name: str) -> SList | None:
    """Return the ``(property "<name>" ...)`` child under ``instance``."""
    for child in instance.items:
        if not isinstance(child, SList) or child.head != "property":
            continue
        if len(child.items) < 2:
            continue
        name_atom = child.items[1]
        if isinstance(name_atom, SAtom) and name_atom.text == name:
            return child
    return None


def _find_instance_by_uuid(root: SList, instance_uuid: str) -> SList | None:
    """Return the top-level ``(symbol ...)`` whose ``(uuid "...")`` matches.

    Top-level (symbol ...) nodes are the instances. lib_symbols' nested
    ``(symbol "Foo:Bar" ...)`` lives under ``lib_symbols``, not the
    root, so the head match + root-child scan is enough to isolate
    instances.
    """
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        atom = uuid_node.items[1]
        if isinstance(atom, SAtom) and atom.text == instance_uuid:
            return child
    return None

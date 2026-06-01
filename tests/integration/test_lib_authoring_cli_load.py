"""Integration test: library authoring outputs load in real kicad-cli.

Thread D (M44-M47) ships four MUTATE tools that synthesize KiCAD 10
library artefacts from scratch:

* ``lib_add_symbol``     — writes ``.kicad_sym``
* ``lib_add_footprint``  — writes ``.kicad_mod`` inside a ``.pretty/``
* ``lib_attach_3d_model`` — appends ``(model ...)`` to a footprint
* ``lib_register_library`` — edits ``sym-lib-table`` / ``fp-lib-table``

Each tool has unit tests that pin the required S-expression shape, but
an assertion-based schema can drift as KiCAD's strict parser evolves.
Loading the files through the real ``kicad-cli`` is the only check that
stays honest across format bumps.

Auto-skips when no ``kicad-cli`` is discoverable, same pattern as
``test_kicad_cli_probe.py`` and ``test_sch_add_sheet_cli_load.py``.

The test uses ``sym upgrade`` / ``fp upgrade`` (parse + rewrite) as the
load check — strictly stricter than a bare ``--help`` probe. Both exit
non-zero if anything in the library file tree fails KiCAD's parser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.config import load_config
from kimcp.tools.builtin.lib_add_footprint import (
    FootprintLine,
    FootprintPad,
    LibAddFootprintInput,
    LibAddFootprintTool,
)
from kimcp.tools.builtin.lib_add_symbol import (
    LibAddSymbolInput,
    LibAddSymbolTool,
    LibSymbolBodyRect,
    LibSymbolPin,
)
from kimcp.tools.builtin.lib_attach_3d_model import (
    LibAttach3dModelInput,
    LibAttach3dModelTool,
    Xyz,
)
from kimcp.tools.builtin.lib_register_library import (
    LibRegisterLibraryInput,
    LibRegisterLibraryTool,
)

pytestmark = pytest.mark.integration


def _resolved_cli_or_skip() -> Path:
    """Skip cleanly when no kicad-cli is on this host."""
    resolved = resolve_cli_path("auto")
    if resolved is None:
        pytest.skip("no kicad-cli discoverable on this machine")
    return Path(resolved)


def _cfg(tmp_path: Path):
    """Snapshot-off config — library authoring tests don't need git."""
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off"}},
    )


# -- symbol library --------------------------------------------------------


@pytest.mark.asyncio
async def test_lib_add_symbol_output_loads_via_kicad_cli(tmp_path: Path) -> None:
    """Write a fresh `.kicad_sym` with M44, ask kicad-cli to re-parse it.

    ``sym upgrade --force`` reopens the library, validates every symbol,
    and re-emits. Exit 0 means the parser accepted the file — exit !=0
    is the exact symptom we'd see if the KiCAD 10 mandatory attribute
    flags or explicit-zero-angle rule drifted.
    """
    cli_path = _resolved_cli_or_skip()

    lib = tmp_path / "Custom.kicad_sym"
    tool = LibAddSymbolTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddSymbolInput(
            lib_path=lib,
            symbol_name="MY_CHIP",
            reference="U",
            value="MY_CHIP",
            description="Integration-test part",
            keywords="test integration",
            footprint_filters=["QFN-16*"],
            pins=[
                LibSymbolPin(
                    number="1",
                    name="VCC",
                    x=-5.08,
                    y=2.54,
                    electrical_type="power_in",
                ),
                LibSymbolPin(
                    number="2",
                    name="GND",
                    x=-5.08,
                    y=-2.54,
                    electrical_type="power_in",
                ),
                LibSymbolPin(
                    number="3",
                    name="SDA",
                    x=5.08,
                    y=2.54,
                    angle=180.0,
                    electrical_type="bidirectional",
                ),
                LibSymbolPin(
                    number="4",
                    name="SCL",
                    x=5.08,
                    y=-2.54,
                    angle=180.0,
                    electrical_type="bidirectional",
                ),
            ],
            body_rect=LibSymbolBodyRect(
                start_x=-2.54, start_y=2.54, end_x=2.54, end_y=-2.54
            ),
        )
    )
    assert out.status == "ok", f"lib_add_symbol failed: {out.note!r}"
    assert lib.is_file()

    # Hand to real kicad-cli. `sym upgrade` parses + re-emits the library;
    # exit 0 is the round-trip proof.
    result = await run_cli(
        ("sym", "upgrade", "--force", str(lib)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli sym upgrade exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}\n"
        f"If you see this, diff the emitted .kicad_sym against "
        f"`/Applications/KiCad/demos/complex_hierarchy/"
        f"complex_hierarchy.kicad_sym` for a newly-required field."
    )


# -- footprint library -----------------------------------------------------


@pytest.mark.asyncio
async def test_lib_add_footprint_output_loads_via_kicad_cli(tmp_path: Path) -> None:
    """Write a ``.pretty/<name>.kicad_mod`` with M45, round-trip via CLI.

    ``fp upgrade --force`` walks the ``.pretty`` directory, re-parses each
    footprint, and re-emits. Same drift guard as the symbol test.
    """
    cli_path = _resolved_cli_or_skip()

    lib = tmp_path / "Custom.pretty"
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
                FootprintLine(
                    start_x=-1.5, start_y=0.6, end_x=1.5, end_y=0.6,
                    layer="F.SilkS", width=0.12,
                ),
            ],
        )
    )
    assert out.status == "ok", f"lib_add_footprint failed: {out.note!r}"
    assert (lib / "R_0603.kicad_mod").is_file()

    # fp upgrade requires --output when given a directory input.
    upgrade_out = tmp_path / "upgraded.pretty"
    result = await run_cli(
        ("fp", "upgrade", "--force", "--output", str(upgrade_out), str(lib)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli fp upgrade exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}\n"
        f"If you see this, diff against `/Applications/KiCad/demos/"
        f"complex_hierarchy/complex_hierarchy.pretty/*.kicad_mod`."
    )


# -- attach + register end-to-end -----------------------------------------


@pytest.mark.asyncio
async def test_full_authoring_flow_loads_via_kicad_cli(tmp_path: Path) -> None:
    """M44 + M45 + M47 + M46 in sequence, then load with kicad-cli.

    Emulates the realistic "new custom part" workflow:

    1. Create a symbol library with one symbol (M44).
    2. Create a footprint library with one footprint (M45).
    3. Attach a 3D model reference to the footprint (M47).
    4. Register both libraries in the project lib-tables (M46).
    5. Ask kicad-cli to re-parse both — proves the full chain survives
       a KiCAD round-trip even after the model block is inserted.
    """
    cli_path = _resolved_cli_or_skip()
    cfg = _cfg(tmp_path)

    # 1. Symbol.
    sym_lib = tmp_path / "Parts.kicad_sym"
    sym_out = await LibAddSymbolTool(cfg).run(
        LibAddSymbolInput(
            lib_path=sym_lib,
            symbol_name="U_LED",
            reference="D",
            value="LED",
            pins=[
                LibSymbolPin(
                    number="1", name="A", x=-2.54, y=0.0,
                    electrical_type="passive",
                ),
                LibSymbolPin(
                    number="2", name="K", x=2.54, y=0.0, angle=180.0,
                    electrical_type="passive",
                ),
            ],
        )
    )
    assert sym_out.status == "ok", f"sym step: {sym_out.note!r}"

    # 2. Footprint.
    fp_lib = tmp_path / "Parts.pretty"
    fp_out = await LibAddFootprintTool(cfg).run(
        LibAddFootprintInput(
            lib_path=fp_lib,
            footprint_name="LED_0603",
            description="LED 0603",
            tags="led smd",
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
        )
    )
    assert fp_out.status == "ok", f"fp step: {fp_out.note!r}"

    # 3. Attach 3D model (stored-as-string; file need not exist).
    fp_file = fp_lib / "LED_0603.kicad_mod"
    attach_out = await LibAttach3dModelTool(cfg).run(
        LibAttach3dModelInput(
            footprint_path=fp_file,
            model_path="${KICAD6_3DMODEL_DIR}/LED_SMD.3dshapes/LED_0603.step",
            offset=Xyz(x=0.0, y=0.0, z=0.0),
            scale=Xyz(x=1.0, y=1.0, z=1.0),
            rotate=Xyz(x=0.0, y=0.0, z=0.0),
        )
    )
    assert attach_out.status == "ok", f"attach step: {attach_out.note!r}"

    # 4. Register both in project lib-tables.
    reg = LibRegisterLibraryTool(cfg)
    sym_table = tmp_path / "sym-lib-table"
    fp_table = tmp_path / "fp-lib-table"
    reg_sym = await reg.run(
        LibRegisterLibraryInput(
            table_path=sym_table,
            table_kind="symbol",
            nickname="Parts",
            library_path=sym_lib,
            description="Integration-test symbols",
        )
    )
    assert reg_sym.status == "ok"
    assert reg_sym.uri == "${KIPRJMOD}/Parts.kicad_sym"
    reg_fp = await reg.run(
        LibRegisterLibraryInput(
            table_path=fp_table,
            table_kind="footprint",
            nickname="Parts",
            library_path=fp_lib,
            description="Integration-test footprints",
        )
    )
    assert reg_fp.status == "ok"
    assert reg_fp.uri == "${KIPRJMOD}/Parts.pretty"

    # 5. Load via kicad-cli — both libraries, post-attach. This is the
    # real round-trip guard: if M47's (model ...) insertion breaks the
    # footprint schema, fp upgrade will blow up here.
    sym_result = await run_cli(
        ("sym", "upgrade", "--force", str(sym_lib)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert sym_result.exit_code == 0, (
        f"sym upgrade exited {sym_result.exit_code}; "
        f"stdout={sym_result.stdout!r} stderr={sym_result.stderr!r}"
    )

    fp_upgrade_out = tmp_path / "Parts_upgraded.pretty"
    fp_result = await run_cli(
        ("fp", "upgrade", "--force", "--output", str(fp_upgrade_out), str(fp_lib)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert fp_result.exit_code == 0, (
        f"fp upgrade exited {fp_result.exit_code}; "
        f"stdout={fp_result.stdout!r} stderr={fp_result.stderr!r}\n"
        f"(This often means the (model ...) block shape is wrong — "
        f"diff the emitted .kicad_mod against a KiCAD-editor-saved one.)"
    )


# Keep the module runnable on its own for quick manual iteration:
#   .venv/bin/python tests/integration/test_lib_authoring_cli_load.py
if __name__ == "__main__":  # pragma: no cover

    async def _main() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            await test_lib_add_symbol_output_loads_via_kicad_cli(Path(d))
        with tempfile.TemporaryDirectory() as d:
            await test_lib_add_footprint_output_loads_via_kicad_cli(Path(d))
        with tempfile.TemporaryDirectory() as d:
            await test_full_authoring_flow_loads_via_kicad_cli(Path(d))
        print("ok")

    asyncio.run(_main())

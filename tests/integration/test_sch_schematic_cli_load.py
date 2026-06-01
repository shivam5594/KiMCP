"""Integration test: schematic-builder outputs load in real kicad-cli.

Regression guard for the ``need a number for 'text angle'`` load
failure reported against the MK-II Controller Board schematic — line
2226 had ``(at 0 -3.81)`` on a power-symbol Reference property where
KiCAD 10's strict parser requires ``(at 0 -3.81 0)``.

The fix was to add a sibling ``at_node_explicit`` helper alongside
``at_node`` (the existing helper elides zero angles to match
``junction`` / ``sheet`` / ``no_connect`` emission), and to migrate
every property-node, symbol-instance-position, and label-position call
site to the explicit form.

These tests reproduce the user's scenario: synthesize a schematic
containing the bug-prone shapes (``sch_add_power``, ``sch_add_symbol``,
``sch_add_label``), then hand the file to real ``kicad-cli sch erc`` —
which is the only drift guard that stays honest across KiCAD's format
bumps.

Auto-skips when no ``kicad-cli`` is discoverable, same pattern as the
other integration tests in this directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.config import load_config
from kimcp.tools.builtin.sch_add_label import SchAddLabelInput, SchAddLabelTool
from kimcp.tools.builtin.sch_add_power import SchAddPowerInput, SchAddPowerTool

pytestmark = pytest.mark.integration


# Minimal KiCAD 10-shaped parent — matches what eeschema emits for a
# freshly-created empty schematic. Tools below append nodes to this
# before we hand it to kicad-cli.
_PARENT_KICAD10 = """\
(kicad_sch
\t(version 20250610)
\t(generator "eeschema")
\t(generator_version "9.99")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols)
\t(sheet_instances
\t\t(path "/" (page "1")))
\t(embedded_fonts no))
"""


def _resolved_cli_or_skip() -> Path:
    resolved = resolve_cli_path("auto")
    if resolved is None:
        pytest.skip("no kicad-cli discoverable on this machine")
    return Path(resolved)


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}},
    )


@pytest.mark.asyncio
async def test_sch_add_power_output_loads_via_real_kicad_cli(tmp_path: Path) -> None:
    """Place a GND power port at angle=0 — exactly the shape that
    produced the ``need a number for 'text angle'`` failure on the
    user's MK-II Controller Board file."""
    cli_path = _resolved_cli_or_skip()

    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddPowerTool()
    tool.set_config(_cfg(tmp_path))
    out = await tool.run(
        SchAddPowerInput(
            sch_path=sch,
            net_name="GND",
            reference="#PWR01",
            at_x=50.0,
            at_y=50.0,
            angle=0.0,  # zero angle — the previous bug trigger
        )
    )
    assert out.status == "ok", f"sch_add_power failed: {out.note!r}"

    result = await run_cli(
        ("sch", "erc", str(sch)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli sch erc exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}\n"
        f"This is the exact 'need a number for text angle' symptom that "
        f"the at_node_explicit patch fixed — regressed. Check every "
        f"(at X Y) node in sch_add_power for 2-atom form."
    )


@pytest.mark.asyncio
async def test_multiple_power_ports_load_via_real_kicad_cli(tmp_path: Path) -> None:
    """Two power ports on different nets — exercises both the
    lib_symbol embed + reuse path and two distinct instance blocks,
    all with the formerly-broken zero-angle at-nodes."""
    cli_path = _resolved_cli_or_skip()

    sch = tmp_path / "two_power.kicad_sch"
    sch.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddPowerTool()
    tool.set_config(_cfg(tmp_path))

    out1 = await tool.run(
        SchAddPowerInput(
            sch_path=sch, net_name="GND", reference="#PWR01",
            at_x=50.0, at_y=50.0, angle=0.0,
        )
    )
    assert out1.status == "ok"
    out2 = await tool.run(
        SchAddPowerInput(
            sch_path=sch, net_name="+3V3", reference="#PWR02",
            at_x=60.0, at_y=50.0, angle=0.0,
        )
    )
    assert out2.status == "ok"

    result = await run_cli(
        ("sch", "erc", str(sch)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli sch erc exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


@pytest.mark.asyncio
async def test_sch_add_label_zero_angle_loads_via_real_kicad_cli(tmp_path: Path) -> None:
    """Drop a local label at angle=0 and round-trip through kicad-cli.

    Label position nodes also migrated to at_node_explicit; regressing
    to at_node would emit 2-atom form and blow up the parser the same
    way the power property did.
    """
    cli_path = _resolved_cli_or_skip()

    sch = tmp_path / "labels.kicad_sch"
    sch.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddLabelTool()
    tool.set_config(_cfg(tmp_path))
    out = await tool.run(
        SchAddLabelInput(
            sch_path=sch,
            text="SDA",
            at_x=50.0,
            at_y=50.0,
            angle=0.0,  # zero angle — regression guard
            kind="local",
        )
    )
    assert out.status == "ok", f"sch_add_label failed: {out.note!r}"

    result = await run_cli(
        ("sch", "erc", str(sch)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli sch erc exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


# Keep the module runnable on its own for quick manual iteration:
#   .venv/bin/python tests/integration/test_sch_schematic_cli_load.py
if __name__ == "__main__":  # pragma: no cover

    async def _main() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            await test_sch_add_power_output_loads_via_real_kicad_cli(Path(d))
        with tempfile.TemporaryDirectory() as d:
            await test_multiple_power_ports_load_via_real_kicad_cli(Path(d))
        with tempfile.TemporaryDirectory() as d:
            await test_sch_add_label_zero_angle_loads_via_real_kicad_cli(Path(d))
        print("ok")

    asyncio.run(_main())

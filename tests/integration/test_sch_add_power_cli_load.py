"""Integration test: sch_add_power output loads in real kicad-cli.

Companion to ``test_sch_add_sheet_cli_load.py``. Guards the same
``need a number for 'text angle'`` regression family on the power-port
code path: auto-embedded ``power:<net>`` lib_symbol properties and the
top-level power-port instance both need the 3-atom ``(at X Y 0)`` form
for zero-angle placements. Running the produced schematic through
``kicad-cli sch erc`` is the only check that stays honest across KiCAD
format bumps — the unit assertions only pin the bytes we emit, not
what KiCAD accepts.

Auto-skips when no ``kicad-cli`` is discoverable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.config import load_config
from kimcp.tools.builtin.sch_add_power import SchAddPowerInput, SchAddPowerTool

pytestmark = pytest.mark.integration


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
    cli_path = _resolved_cli_or_skip()

    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddPowerTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=100.0, at_y=50.0)
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
        f"If this fails with 'need a number for \\'text angle\\'', a "
        f"(property ...) or instance (at ...) somewhere in the emitted "
        f"power port is 2-atom — re-run the migration against "
        f"sch_add_power.py."
    )


if __name__ == "__main__":  # pragma: no cover

    async def _main() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            await test_sch_add_power_output_loads_via_real_kicad_cli(Path(d))
        print("ok")

    asyncio.run(_main())

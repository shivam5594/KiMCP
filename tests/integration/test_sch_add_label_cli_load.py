"""Integration test: sch_add_label output loads in real kicad-cli.

Companion to ``test_sch_add_sheet_cli_load.py`` and
``test_sch_add_power_cli_load.py``. Guards the
``need a number for 'text angle'`` regression on the label code paths:
all four label variants (local, global, hierarchical, plus the
Intersheetrefs property inside a global label) must write the 3-atom
``(at X Y 0)`` form for zero-angle placements.

Covers two kinds in one test — local and global — because KiCAD's
load-parser enforces the same rule against both and doubling the test
cost would be wasteful. ``hierarchical`` is an empty placeholder path
until sheet-pin wiring is implemented; skipping it here is fine.

Auto-skips when no ``kicad-cli`` is discoverable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.config import load_config
from kimcp.tools.builtin.sch_add_label import SchAddLabelInput, SchAddLabelTool

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
async def test_sch_add_label_output_loads_via_real_kicad_cli(tmp_path: Path) -> None:
    cli_path = _resolved_cli_or_skip()

    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddLabelTool()
    tool.set_config(_cfg(tmp_path))

    # Local label at angle=0.
    out_local = await tool.run(
        SchAddLabelInput(sch_path=sch, text="NET_A", at_x=20.0, at_y=30.0, kind="local")
    )
    assert out_local.status == "ok", out_local.note

    # Global label at angle=0 — exercises the Intersheetrefs property
    # (an inner (at ...) site that was missed by the first migration
    # pass if the caller isn't careful).
    out_global = await tool.run(
        SchAddLabelInput(sch_path=sch, text="NET_B", at_x=60.0, at_y=30.0, kind="global")
    )
    assert out_global.status == "ok", out_global.note

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
        f"If this fails with 'need a number for \\'text angle\\'', one "
        f"of the label / global_label / Intersheetrefs property (at ...) "
        f"nodes is still 2-atom — re-run the migration against "
        f"sch_add_label.py."
    )


if __name__ == "__main__":  # pragma: no cover

    async def _main() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            await test_sch_add_label_output_loads_via_real_kicad_cli(Path(d))
        print("ok")

    asyncio.run(_main())

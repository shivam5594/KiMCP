"""Integration test: sch_add_sheet output loads in real kicad-cli.

This guards against the exact regression documented in
``DEBUG_sch_erc_hierarchical_load_failure.md`` — a parent schematic with
a ``(sheet ...)`` reference that our SEXPR parser accepts but KiCAD's
``SCH_IO_KICAD_SEXPR`` rejects at load time (exit 3, terse "Failed to
load schematic" with empty stderr).

The unit test in ``tests/unit/test_tool_sch_add_sheet.py`` pins the
current required shape (four attribute flags + explicit angle on sheet
properties), but pinning a schema in an assertion can drift out of sync
with reality as KiCAD evolves. Loading the file through the real
``kicad-cli`` is the only check that stays honest across format bumps.

Auto-skips when no ``kicad-cli`` is discoverable, same pattern as
``test_kicad_cli_probe.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.config import load_config
from kimcp.tools.builtin.sch_add_sheet import SchAddSheetInput, SchAddSheetTool

pytestmark = pytest.mark.integration


# Minimal KiCAD 10-shaped parent — matches what eeschema emits for a
# freshly-created empty schematic. `sch_add_sheet` will append a
# `(sheet ...)` node to this before we hand it to kicad-cli.
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
    """Skip cleanly when no kicad-cli is on this host."""
    resolved = resolve_cli_path("auto")
    if resolved is None:
        pytest.skip("no kicad-cli discoverable on this machine")
    return Path(resolved)


def _cfg(tmp_path: Path):
    """Snapshot-off config — the snapshot machinery touches git and
    would require a repo context; irrelevant to what we're testing."""
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off", "grid_snap_mm": None}},
    )


@pytest.mark.asyncio
async def test_sch_add_sheet_output_loads_via_real_kicad_cli(tmp_path: Path) -> None:
    """End-to-end: bootstrap a parent, place a subsheet via the tool,
    run ``kicad-cli sch erc`` on the result, assert exit 0.

    Regression guard for the hierarchical-sheet load failure where the
    `(sheet ...)` node was missing four mandatory attribute flags
    (`exclude_from_sim`, `in_bom`, `on_board`, `dnp`) and emitting sheet
    properties with the angle atom elided (KiCAD 10's sheet-property
    parser is strict and requires `(at X Y 0)` explicitly).
    """
    cli_path = _resolved_cli_or_skip()

    parent = tmp_path / "parent.kicad_sch"
    parent.write_text(_PARENT_KICAD10, encoding="utf-8")

    tool = SchAddSheetTool(_cfg(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=parent,
            sheet_name="Power",
            sheet_file="sheets/power.kicad_sch",
            at_x=30.0,
            at_y=30.0,
            size_w=60.0,
            size_h=40.0,
        )
    )
    assert out.status == "ok", f"sch_add_sheet failed: {out.note!r}"
    assert out.child_created is True
    assert (tmp_path / "sheets" / "power.kicad_sch").is_file()

    # Hand the parent to real kicad-cli. ERC is a convenient load path:
    # it parses the whole hierarchy and fails loudly if anything rejects.
    result = await run_cli(
        ("sch", "erc", str(parent)),
        cli_path=cli_path,
        timeout=60.0,
        check=False,
    )
    assert result.exit_code == 0, (
        f"kicad-cli sch erc exited {result.exit_code}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}\n"
        f"This is the exact load-failure symptom the sheet-node patch "
        f"fixed — if you see it again, diff the emitted (sheet ...) "
        f"against `/Applications/KiCad/demos/complex_hierarchy/"
        f"complex_hierarchy.kicad_sch` for any newly-required field."
    )


# Keep the module runnable on its own for quick manual iteration:
#   .venv/bin/python tests/integration/test_sch_add_sheet_cli_load.py
if __name__ == "__main__":  # pragma: no cover

    async def _main() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            await test_sch_add_sheet_output_loads_via_real_kicad_cli(Path(d))
        print("ok")

    asyncio.run(_main())

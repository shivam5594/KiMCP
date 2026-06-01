"""Unit tests for the ``pcb_export_step`` built-in tool (M22).

Exercises the full status matrix (ok / dry_run / pcb_not_found /
cli_failed / output_missing) plus the argv surface (origin selectors,
no_unspecified / no_dnp / subst_models / board_only / include_tracks /
include_zones / min_distance / no_optimize / force) and the origin
mutual-exclusion guard.

The kicad-cli stub is a per-file script (same rationale as
``test_tool_pcb_export_drill.py``) that:

* Handles ``version`` so ``CliBackend.probe()`` can resolve a version.
* Records the ``pcb export step`` argv to ``<stub>.argv`` (JSON).
* Writes the file named by ``--output`` with the contents of
  ``<stub>.contents`` (or a default STEP-ish header).
* Exits with the integer in ``<stub>.exit`` (0 if absent).
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from kimcp.backends.cli import CliBackend
from kimcp.tools.builtin.pcb_export_step import (
    PcbExportStepInput,
    PcbExportStepTool,
)

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "kicad-cli-stub"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path

            here = Path(__file__).resolve()
            argv = sys.argv[1:]

            if argv[:1] == ["version"]:
                sys.stdout.write("Application: kicad-cli\\n")
                sys.stdout.write("Version: 9.0.1, release build\\n")
                sys.exit(0)

            (here.parent / (here.name + ".argv")).write_text(
                json.dumps(argv), encoding="utf-8"
            )

            out_path = None
            for i, a in enumerate(argv):
                if a == "--output" and i + 1 < len(argv):
                    out_path = argv[i + 1]
                    break

            exit_file = here.parent / (here.name + ".exit")
            exit_code = 0
            if exit_file.exists():
                try:
                    exit_code = int(exit_file.read_text(encoding="utf-8").strip())
                except ValueError:
                    exit_code = 0

            # Write the output file only on success; tests that stage a
            # non-zero exit want the file to stay absent.
            if exit_code == 0 and out_path is not None:
                contents_file = here.parent / (here.name + ".contents")
                contents = "ISO-10303-21;\\nHEADER;\\nENDSEC;\\nDATA;\\nENDSEC;\\nEND-ISO-10303-21;\\n"
                if contents_file.exists():
                    contents = contents_file.read_text(encoding="utf-8")
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(contents, encoding="utf-8")

            if exit_code != 0:
                sys.stderr.write("synthetic kicad-cli failure\\n")
            sys.exit(exit_code)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _stage_contents(stub: Path, contents: str) -> None:
    (stub.parent / (stub.name + ".contents")).write_text(contents, encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> PcbExportStepTool:
    tool = PcbExportStepTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_with_defaults(tmp_path: Path) -> None:
    """Happy path: default output path lands as board.step next to the pcb."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.pcb_path == str(pcb.resolve())
    assert out.output_path == str(pcb.with_suffix(".step").resolve())
    assert out.size_bytes is not None and out.size_bytes > 0
    assert out.note is None
    assert Path(out.output_path).exists()


@pytest.mark.asyncio
async def test_status_ok_with_custom_output_path(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    custom = tmp_path / "mcad" / "rev_a" / "assembly.step"

    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=pcb, output_path=custom))

    assert out.status == "ok"
    assert out.output_path == str(custom.resolve())
    assert custom.exists()


@pytest.mark.asyncio
async def test_output_size_matches_disk(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_contents(stub, "ABCD")

    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.size_bytes == 4


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_carries_default_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)

    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert argv[:3] == ["pcb", "export", "step"]
    assert argv[argv.index("--output") + 1] == str(pcb.with_suffix(".step").resolve())
    assert argv[argv.index("--min-distance") + 1] == "0.01mm"
    # Defaults -> no origin, no filters, no inclusion flags
    assert "--grid-origin" not in argv
    assert "--drill-origin" not in argv
    assert "--user-origin" not in argv
    assert "--no-unspecified" not in argv
    assert "--no-dnp" not in argv
    assert "--subst-models" not in argv
    assert "--board-only" not in argv
    assert "--include-tracks" not in argv
    assert "--include-zones" not in argv
    assert "--no-optimize-step" not in argv
    # force defaults True — overwrite is the rebuild contract.
    assert "--force" in argv
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_carries_grid_origin(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, grid_origin=True))
    argv = _read_argv(stub)
    assert "--grid-origin" in argv


@pytest.mark.asyncio
async def test_argv_carries_drill_origin(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, drill_origin=True))
    argv = _read_argv(stub)
    assert "--drill-origin" in argv


@pytest.mark.asyncio
async def test_argv_carries_user_origin(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, user_origin="50,50"))
    argv = _read_argv(stub)
    assert argv[argv.index("--user-origin") + 1] == "50,50"


@pytest.mark.asyncio
async def test_argv_carries_filter_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(
        PcbExportStepInput(
            pcb_path=pcb,
            no_unspecified=True,
            no_dnp=True,
            subst_models=True,
        )
    )
    argv = _read_argv(stub)
    assert "--no-unspecified" in argv
    assert "--no-dnp" in argv
    assert "--subst-models" in argv


@pytest.mark.asyncio
async def test_argv_carries_geometry_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(
        PcbExportStepInput(
            pcb_path=pcb,
            board_only=True,
            include_tracks=True,
            include_zones=True,
        )
    )
    argv = _read_argv(stub)
    assert "--board-only" in argv
    assert "--include-tracks" in argv
    assert "--include-zones" in argv


@pytest.mark.asyncio
async def test_argv_carries_min_distance(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, min_distance_mm=0.1))
    argv = _read_argv(stub)
    assert argv[argv.index("--min-distance") + 1] == "0.1mm"


@pytest.mark.asyncio
async def test_argv_carries_no_optimize(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, no_optimize_step=True))
    argv = _read_argv(stub)
    assert "--no-optimize-step" in argv


@pytest.mark.asyncio
async def test_argv_omits_force_when_disabled(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportStepInput(pcb_path=pcb, force=False))
    argv = _read_argv(stub)
    assert "--force" not in argv


# -- origin mutual exclusion ----------------------------------------------


@pytest.mark.asyncio
async def test_origin_mutex_grid_plus_drill(tmp_path: Path) -> None:
    """grid_origin + drill_origin -> cli_failed before any subprocess."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportStepInput(pcb_path=pcb, grid_origin=True, drill_origin=True)
    )
    assert out.status == "cli_failed"
    assert out.note is not None and "mutually exclusive" in out.note
    # Pre-flight guard — no stub invocation happened.
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_origin_mutex_drill_plus_user(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportStepInput(pcb_path=pcb, drill_origin=True, user_origin="10,10")
    )
    assert out.status == "cli_failed"
    assert out.note is not None
    assert "drill_origin" in out.note and "user_origin" in out.note


@pytest.mark.asyncio
async def test_origin_mutex_all_three(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportStepInput(
            pcb_path=pcb,
            grid_origin=True,
            drill_origin=True,
            user_origin="0,0",
        )
    )
    assert out.status == "cli_failed"
    assert out.note is not None and "mutually exclusive" in out.note


# -- dry-run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_skips_cli(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    custom = tmp_path / "mcad_dry" / "board.step"
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportStepInput(pcb_path=pcb, output_path=custom, dry_run=True)
    )
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "step" in out.cli_argv
    assert out.size_bytes is None
    assert out.note is not None and "dry_run" in out.note
    # CLI never ran, parent dir never created.
    assert not (stub.parent / (stub.name + ".argv")).exists()
    assert not custom.exists()
    assert not custom.parent.exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_pcb_not_found_when_missing(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportStepInput(pcb_path=tmp_path / "nope.kicad_pcb")
    )
    assert out.status == "pcb_not_found"
    assert out.pcb_path is None
    assert out.note is not None and "no such file" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_pcb_not_found_wrong_extension(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = tmp_path / "design.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=sch))
    assert out.status == "pcb_not_found"
    assert out.pcb_path == str(sch.resolve())
    assert out.note is not None and ".kicad_pcb" in out.note


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 3)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))

    assert out.status == "cli_failed"
    assert out.cli_argv is not None
    assert out.note is not None
    assert "exited 3" in out.note
    assert "synthetic kicad-cli failure" in out.note


@pytest.mark.asyncio
async def test_status_output_missing_when_file_not_written(tmp_path: Path) -> None:
    """CLI exits 0 but nothing on disk -> output_missing."""
    # Custom stub: version subcommand + exit 0 but never writes --output.
    stub = tmp_path / "kicad-cli-stub-noop"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            if sys.argv[1:2] == ["version"]:
                sys.stdout.write("Application: kicad-cli\\n")
                sys.stdout.write("Version: 9.0.1, release build\\n")
                sys.exit(0)
            sys.exit(0)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    pcb = _touch_pcb(tmp_path)

    tool = PcbExportStepTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))

    assert out.status == "output_missing"
    assert out.note is not None
    assert "not on disk" in out.note


@pytest.mark.asyncio
async def test_status_output_missing_when_file_is_zero_bytes(tmp_path: Path) -> None:
    """CLI exits 0 but writes an empty file -> output_missing."""
    stub = _write_kicad_cli_stub(tmp_path)
    _stage_contents(stub, "")
    pcb = _touch_pcb(tmp_path)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))
    assert out.status == "output_missing"
    assert out.note is not None and "zero bytes" in out.note


# -- dependency injection --------------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_still_runs(tmp_path: Path) -> None:
    """Bare tool load must still produce a valid envelope (shape-only assert)."""
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportStepTool()
    out = await tool.run(PcbExportStepInput(pcb_path=pcb))
    # Without kicad-cli on the tmpdir, we'll fall into cli_failed /
    # output_missing (depending on host env), but never crash.
    assert out.status in {"ok", "cli_failed", "output_missing"}
    assert out.pcb_path == str(pcb.resolve())


@pytest.mark.asyncio
async def test_tool_without_injection_dry_run_returns_argv(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportStepTool()
    out = await tool.run(PcbExportStepInput(pcb_path=pcb, dry_run=True))
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "step" in out.cli_argv


# -- metadata --------------------------------------------------------------


def test_tool_metadata() -> None:
    from kimcp._types import Backend, ToolClass

    tool = PcbExportStepTool()
    assert tool.name == "pcb_export_step"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})
    assert tool.mutates is True

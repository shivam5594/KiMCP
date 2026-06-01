"""Unit tests for sch_export_svg.

kicad-cli writes one SVG per sheet into a directory, so this test
matrix covers the multi-file envelope shape: generated_files list,
total_files/total_bytes aggregation, no_files_produced when the
dir stays empty, and the pre-existing-files warning.

The stub kicad-cli writes two named SVGs into ``--output``, so we
can assert the full multi-file pipeline end to end.
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
from kimcp.tools.builtin.sch_export_svg import (
    SchExportSvgInput,
    SchExportSvgTool,
)


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Stub that drops two SVGs into --output on success, records argv."""
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

            out_dir = None
            for i, a in enumerate(argv):
                if a == "--output" and i + 1 < len(argv):
                    out_dir = Path(argv[i + 1])
                    break

            exit_file = here.parent / (here.name + ".exit")
            exit_code = 0
            if exit_file.exists():
                try:
                    exit_code = int(exit_file.read_text(encoding="utf-8").strip())
                except ValueError:
                    exit_code = 0

            skip_file = here.parent / (here.name + ".skip_write")
            skip_write = skip_file.exists()

            if exit_code == 0 and out_dir is not None and not skip_write:
                out_dir.mkdir(parents=True, exist_ok=True)
                body = "<svg xmlns='http://www.w3.org/2000/svg'/>\\n"
                (out_dir / "design-root.svg").write_text(body, encoding="utf-8")
                (out_dir / "design-sub1.svg").write_text(body, encoding="utf-8")

            if exit_code != 0:
                sys.stderr.write("synthetic kicad-cli failure\\n")
            sys.exit(exit_code)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _stage_exit(stub: Path, code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(code), encoding="utf-8")


def _stage_skip_write(stub: Path) -> None:
    """Make the stub exit 0 without writing any SVGs."""
    (stub.parent / (stub.name + ".skip_write")).write_text("1", encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> SchExportSvgTool:
    tool = SchExportSvgTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_sch(tmp_path: Path) -> Path:
    s = tmp_path / "design.kicad_sch"
    s.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return s


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_defaults(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    out = await _make_tool(stub).run(SchExportSvgInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total_files == 2
    # Default output_dir is <schematic_parent>/svg.
    expected_dir = (sch.parent / "svg").resolve()
    assert out.output_dir == str(expected_dir)
    names = sorted(Path(f.path).name for f in out.generated_files)
    assert names == ["design-root.svg", "design-sub1.svg"]
    assert out.total_bytes > 0


@pytest.mark.asyncio
async def test_custom_output_dir(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    custom = tmp_path / "docs" / "schematic-svgs"
    out = await _make_tool(stub).run(
        SchExportSvgInput(sch_path=sch, output_dir=custom)
    )
    assert out.status == "ok"
    assert out.output_dir == str(custom.resolve())
    assert custom.exists()


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(SchExportSvgInput(sch_path=sch))
    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "svg"]
    # Optional flags absent by default.
    assert "--pages" not in argv
    assert "--theme" not in argv
    assert "--black-and-white" not in argv
    assert "--exclude-drawing-sheet" not in argv
    assert "--no-background-color" not in argv
    assert "--define-var" not in argv
    assert argv[-1] == str(sch.resolve())


@pytest.mark.asyncio
async def test_argv_pages_and_theme(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(
        SchExportSvgInput(sch_path=sch, pages=["1", "3"], theme="KiCAD 2020")
    )
    argv = _read_argv(stub)
    assert argv[argv.index("--pages") + 1] == "1,3"
    assert argv[argv.index("--theme") + 1] == "KiCAD 2020"


@pytest.mark.asyncio
async def test_argv_visual_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(
        SchExportSvgInput(
            sch_path=sch,
            black_and_white=True,
            exclude_drawing_sheet=True,
            no_background_color=True,
        )
    )
    argv = _read_argv(stub)
    assert "--black-and-white" in argv
    assert "--exclude-drawing-sheet" in argv
    assert "--no-background-color" in argv


@pytest.mark.asyncio
async def test_argv_define_vars(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(
        SchExportSvgInput(
            sch_path=sch,
            define_vars={"REV": "B2", "DATE": "2026-04-16"},
        )
    )
    argv = _read_argv(stub)
    # Every entry is preceded by --define-var and uses NAME=VAL form.
    pairs = [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--define-var"
    ]
    assert sorted(pairs) == ["DATE=2026-04-16", "REV=B2"]


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    out = await _make_tool(stub).run(
        SchExportSvgInput(sch_path=sch, dry_run=True)
    )
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "svg" in out.cli_argv
    # No actual invocation happened.
    assert not (stub.parent / (stub.name + ".argv")).exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    out = await _make_tool(stub).run(
        SchExportSvgInput(sch_path=tmp_path / "nope.kicad_sch")
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    out = await _make_tool(stub).run(SchExportSvgInput(sch_path=f))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_cli_failed_nonzero_exit(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_exit(stub, 11)
    out = await _make_tool(stub).run(SchExportSvgInput(sch_path=sch))
    assert out.status == "cli_failed"
    assert out.note is not None and "exited 11" in out.note


@pytest.mark.asyncio
async def test_no_files_produced_clean_exit(tmp_path: Path) -> None:
    """kicad-cli exits 0 but writes nothing — surface no_files_produced
    so the caller doesn't mistake empty for success."""
    stub = _write_kicad_cli_stub(tmp_path)
    _stage_skip_write(stub)
    sch = _touch_sch(tmp_path)
    out = await _make_tool(stub).run(SchExportSvgInput(sch_path=sch))
    assert out.status == "no_files_produced"
    assert out.total_files == 0


# -- dirty-dir warning -----------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_dir_warning(tmp_path: Path) -> None:
    """Pre-existing files in output_dir trigger a warning but don't
    fail the call. Generated_files lists only newly-created entries."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    existing = tmp_path / "svg"
    existing.mkdir()
    stale = existing / "stale.svg"
    stale.write_text("<svg/>\n", encoding="utf-8")

    out = await _make_tool(stub).run(SchExportSvgInput(sch_path=sch))
    assert out.status == "ok"
    assert out.total_files == 2  # only the two new files
    names = {Path(f.path).name for f in out.generated_files}
    assert "stale.svg" not in names
    # Warning fired.
    assert any(
        "output_dir already contained" in w for w in out.meta.warnings
    )


# -- DI --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_injection_dry_run(tmp_path: Path) -> None:
    sch = _touch_sch(tmp_path)
    tool = SchExportSvgTool()
    out = await tool.run(SchExportSvgInput(sch_path=sch, dry_run=True))
    assert out.status == "dry_run"


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    from kimcp._types import Backend, ToolClass

    tool = SchExportSvgTool()
    assert tool.name == "sch_export_svg"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})

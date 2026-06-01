"""Unit tests for sch_export_pdf (M25)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from kimcp.backends.cli import CliBackend
from kimcp.tools.builtin.sch_export_pdf import (
    SchExportPdfInput,
    SchExportPdfTool,
)


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

            if exit_code == 0 and out_path is not None:
                contents_file = here.parent / (here.name + ".contents")
                contents = "%PDF-1.4\\n%%EOF\\n"
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


def _stage_exit(stub: Path, code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(code), encoding="utf-8")


def _stage_contents(stub: Path, text: str) -> None:
    (stub.parent / (stub.name + ".contents")).write_text(text, encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> SchExportPdfTool:
    tool = SchExportPdfTool()
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
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=sch))
    assert out.status == "ok"
    assert out.output_path == str(sch.with_suffix(".pdf").resolve())
    assert out.size_bytes is not None and out.size_bytes > 0


@pytest.mark.asyncio
async def test_custom_output_path(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    custom = tmp_path / "docs" / "review.pdf"
    out = await _make_tool(stub).run(
        SchExportPdfInput(sch_path=sch, output_path=custom)
    )
    assert out.status == "ok"
    assert out.output_path == str(custom.resolve())
    assert custom.exists()


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_minimal_defaults(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(SchExportPdfInput(sch_path=sch))
    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "pdf"]
    # No optional flags emitted by default.
    assert "--pages" not in argv
    assert "--theme" not in argv
    assert "--black-and-white" not in argv
    assert "--exclude-drawing-sheet" not in argv
    assert "--no-background-color" not in argv
    assert "--define-var" not in argv
    assert argv[-1] == str(sch.resolve())


@pytest.mark.asyncio
async def test_argv_pages(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(SchExportPdfInput(sch_path=sch, pages=["1", "3"]))
    argv = _read_argv(stub)
    assert argv[argv.index("--pages") + 1] == "1,3"


@pytest.mark.asyncio
async def test_argv_theme(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(SchExportPdfInput(sch_path=sch, theme="BlueTheme"))
    argv = _read_argv(stub)
    assert argv[argv.index("--theme") + 1] == "BlueTheme"


@pytest.mark.asyncio
async def test_argv_visual_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    await _make_tool(stub).run(
        SchExportPdfInput(
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
        SchExportPdfInput(sch_path=sch, define_vars={"REV": "A", "DATE": "2026-04-16"})
    )
    argv = _read_argv(stub)
    # Two --define-var pairs, order preserving the input dict.
    rev_idx = argv.index("REV=A")
    date_idx = argv.index("DATE=2026-04-16")
    assert argv[rev_idx - 1] == "--define-var"
    assert argv[date_idx - 1] == "--define-var"


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=sch, dry_run=True))
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "pdf" in out.cli_argv
    assert not (stub.parent / (stub.name + ".argv")).exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    out = await _make_tool(stub).run(
        SchExportPdfInput(sch_path=tmp_path / "nope.kicad_sch")
    )
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_path_is_directory(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    d = tmp_path / "dir.kicad_sch"
    d.mkdir()
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=d))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    f = tmp_path / "board.kicad_pcb"
    f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=f))
    assert out.status == "sch_not_found"


@pytest.mark.asyncio
async def test_cli_failed_nonzero_exit(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_exit(stub, 9)
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=sch))
    assert out.status == "cli_failed"
    assert out.note is not None and "exited 9" in out.note


@pytest.mark.asyncio
async def test_output_missing_zero_bytes(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_contents(stub, "")
    out = await _make_tool(stub).run(SchExportPdfInput(sch_path=sch))
    assert out.status == "output_missing"


# -- DI + metadata ---------------------------------------------------------


@pytest.mark.asyncio
async def test_without_injection_dry_run(tmp_path: Path) -> None:
    sch = _touch_sch(tmp_path)
    tool = SchExportPdfTool()
    out = await tool.run(SchExportPdfInput(sch_path=sch, dry_run=True))
    assert out.status == "dry_run"


def test_metadata() -> None:
    from kimcp._types import Backend, ToolClass

    tool = SchExportPdfTool()
    assert tool.name == "sch_export_pdf"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})

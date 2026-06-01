"""Unit tests for pcb_export_svg.

Mirrors the pcb_export_pdf test matrix: argv plumbing with a fake
kicad-cli stub, dry-run path, failure modes (missing input, wrong
suffix, nonzero exit, zero-byte output), and metadata.

One new axis for SVG: the ``page_size`` enum that maps to
``--page-size-mode 0/1/2`` — exercised in three dedicated argv
tests so no value silently drops off the CLI line.
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
from kimcp.tools.builtin.pcb_export_svg import (
    PcbExportSvgInput,
    PcbExportSvgTool,
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
                contents = "<svg xmlns='http://www.w3.org/2000/svg'/>\\n"
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


def _make_tool(stub: Path) -> PcbExportSvgTool:
    tool = PcbExportSvgTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_defaults(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(PcbExportSvgInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.output_path == str(pcb.with_suffix(".svg").resolve())
    assert out.size_bytes is not None and out.size_bytes > 0


@pytest.mark.asyncio
async def test_custom_output_path(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    custom = tmp_path / "docs" / "board.svg"
    tool = _make_tool(stub)
    out = await tool.run(PcbExportSvgInput(pcb_path=pcb, output_path=custom))
    assert out.status == "ok"
    assert out.output_path == str(custom.resolve())
    assert custom.exists()


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert argv[:3] == ["pcb", "export", "svg"]
    # Single-mode always emitted; separate/multi deferred.
    assert "--mode-single" in argv
    # Default page size = full (mode 0).
    assert argv[argv.index("--page-size-mode") + 1] == "0"
    # Layers include the 7-layer default.
    layers = argv[argv.index("--layers") + 1]
    for exp in ("F.Cu", "B.Cu", "Edge.Cuts"):
        assert exp in layers
    # drill_shape='real' -> "2".
    assert argv[argv.index("--drill-shape-opt") + 1] == "2"
    # Default booleans off.
    assert "--mirror" not in argv
    assert "--exclude-refdes" not in argv
    assert "--exclude-value" not in argv
    assert "--black-and-white" not in argv
    assert "--negative" not in argv
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_custom_layers(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(
        PcbExportSvgInput(pcb_path=pcb, layers=["F.Cu", "In1.Cu", "B.Cu"])
    )
    argv = _read_argv(stub)
    assert argv[argv.index("--layers") + 1] == "F.Cu,In1.Cu,B.Cu"


@pytest.mark.asyncio
async def test_argv_page_size_current(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb, page_size="current"))
    argv = _read_argv(stub)
    assert argv[argv.index("--page-size-mode") + 1] == "1"


@pytest.mark.asyncio
async def test_argv_page_size_board_only(tmp_path: Path) -> None:
    """board_only → mode 2 — the README-embed case."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(
        PcbExportSvgInput(pcb_path=pcb, page_size="board_only")
    )
    argv = _read_argv(stub)
    assert argv[argv.index("--page-size-mode") + 1] == "2"


@pytest.mark.asyncio
async def test_argv_drill_shape_none(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb, drill_shape="none"))
    argv = _read_argv(stub)
    assert argv[argv.index("--drill-shape-opt") + 1] == "0"


@pytest.mark.asyncio
async def test_argv_drill_shape_small(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb, drill_shape="small"))
    argv = _read_argv(stub)
    assert argv[argv.index("--drill-shape-opt") + 1] == "1"


@pytest.mark.asyncio
async def test_argv_visual_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(
        PcbExportSvgInput(
            pcb_path=pcb,
            mirror=True,
            exclude_refdes=True,
            exclude_value=True,
            black_and_white=True,
            negative=True,
        )
    )
    argv = _read_argv(stub)
    assert "--mirror" in argv
    assert "--exclude-refdes" in argv
    assert "--exclude-value" in argv
    assert "--black-and-white" in argv
    assert "--negative" in argv


@pytest.mark.asyncio
async def test_argv_theme_optional(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb, theme="KiCAD 2020"))
    argv = _read_argv(stub)
    assert argv[argv.index("--theme") + 1] == "KiCAD 2020"


@pytest.mark.asyncio
async def test_argv_no_theme_by_default(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb))
    argv = _read_argv(stub)
    assert "--theme" not in argv


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out = await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb, dry_run=True))
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "svg" in out.cli_argv
    assert not (stub.parent / (stub.name + ".argv")).exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_pcb_not_found(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    out = await _make_tool(stub).run(
        PcbExportSvgInput(pcb_path=tmp_path / "nope.kicad_pcb")
    )
    assert out.status == "pcb_not_found"


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    f = tmp_path / "a.kicad_sch"
    f.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")
    out = await _make_tool(stub).run(PcbExportSvgInput(pcb_path=f))
    assert out.status == "pcb_not_found"


@pytest.mark.asyncio
async def test_cli_failed_nonzero_exit(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 11)
    out = await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb))
    assert out.status == "cli_failed"
    assert out.note is not None and "exited 11" in out.note


@pytest.mark.asyncio
async def test_output_missing_zero_bytes(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_contents(stub, "")
    out = await _make_tool(stub).run(PcbExportSvgInput(pcb_path=pcb))
    assert out.status == "output_missing"
    assert out.note is not None and "zero bytes" in out.note


# -- DI --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_injection_dry_run(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportSvgTool()
    out = await tool.run(PcbExportSvgInput(pcb_path=pcb, dry_run=True))
    assert out.status == "dry_run"


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    from kimcp._types import Backend, ToolClass

    tool = PcbExportSvgTool()
    assert tool.name == "pcb_export_svg"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})

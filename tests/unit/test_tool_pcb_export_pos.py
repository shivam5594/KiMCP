"""Unit tests for pcb_export_pos (M23)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from kimcp.backends.cli import CliBackend
from kimcp.tools.builtin.pcb_export_pos import (
    PcbExportPosInput,
    PcbExportPosTool,
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
                contents = "Ref,Val,Package,PosX,PosY,Rot,Side\\nU1,MCU,QFN48,0,0,0,top\\n"
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


def _make_tool(stub: Path) -> PcbExportPosTool:
    tool = PcbExportPosTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_defaults(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(PcbExportPosInput(pcb_path=pcb))

    assert out.status == "ok"
    # Default output: board-pos.csv
    assert out.output_path == str((pcb.parent / "board-pos.csv").resolve())
    assert out.size_bytes is not None and out.size_bytes > 0
    assert Path(out.output_path).exists()


@pytest.mark.asyncio
async def test_ok_ascii_default_extension(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    out = await tool.run(PcbExportPosInput(pcb_path=pcb, format="ascii"))

    assert out.status == "ok"
    # ASCII format uses .pos extension by convention.
    assert out.output_path == str((pcb.parent / "board-pos.pos").resolve())


@pytest.mark.asyncio
async def test_custom_output_path_honored(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    custom = tmp_path / "out" / "centroid.csv"
    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportPosInput(pcb_path=pcb, output_path=custom)
    )
    assert out.status == "ok"
    assert out.output_path == str(custom.resolve())
    assert custom.exists()


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    tool = _make_tool(stub)
    await tool.run(PcbExportPosInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert argv[:3] == ["pcb", "export", "pos"]
    assert argv[argv.index("--format") + 1] == "csv"
    assert argv[argv.index("--side") + 1] == "both"
    assert argv[argv.index("--units") + 1] == "mm"
    assert "--use-drill-file-origin" not in argv
    assert "--bottom-negate-x" not in argv
    assert "--smd-only" not in argv
    assert "--exclude-fp-th" not in argv
    assert "--exclude-dnp" not in argv
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_format_ascii(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb, format="ascii"))
    argv = _read_argv(stub)
    assert argv[argv.index("--format") + 1] == "ascii"


@pytest.mark.asyncio
async def test_argv_side_front(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb, side="front"))
    argv = _read_argv(stub)
    assert argv[argv.index("--side") + 1] == "front"


@pytest.mark.asyncio
async def test_argv_side_back(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb, side="back"))
    argv = _read_argv(stub)
    assert argv[argv.index("--side") + 1] == "back"


@pytest.mark.asyncio
async def test_argv_units_in(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb, units="in"))
    argv = _read_argv(stub)
    assert argv[argv.index("--units") + 1] == "in"


@pytest.mark.asyncio
async def test_argv_boolean_flags(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(
        PcbExportPosInput(
            pcb_path=pcb,
            use_drill_file_origin=True,
            bottom_negate_x=True,
            smd_only=True,
            exclude_fp_th=True,
            exclude_dnp=True,
        )
    )
    argv = _read_argv(stub)
    assert "--use-drill-file-origin" in argv
    assert "--bottom-negate-x" in argv
    assert "--smd-only" in argv
    assert "--exclude-fp-th" in argv
    assert "--exclude-dnp" in argv


@pytest.mark.asyncio
async def test_argv_exclude_fp_th_alias_fires_once(tmp_path: Path) -> None:
    """Both exclude_fp_th and exclude_footprints_with_th map to a single --exclude-fp-th."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    await _make_tool(stub).run(
        PcbExportPosInput(
            pcb_path=pcb,
            exclude_fp_th=True,
            exclude_footprints_with_th=True,
        )
    )
    argv = _read_argv(stub)
    assert argv.count("--exclude-fp-th") == 1


# -- dry_run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out = await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb, dry_run=True))
    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "pos" in out.cli_argv
    # CLI didn't run.
    assert not (stub.parent / (stub.name + ".argv")).exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_pcb_not_found_missing(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    out = await _make_tool(stub).run(
        PcbExportPosInput(pcb_path=tmp_path / "nope.kicad_pcb")
    )
    assert out.status == "pcb_not_found"
    assert out.pcb_path is None


@pytest.mark.asyncio
async def test_pcb_not_found_wrong_suffix(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    f = tmp_path / "design.kicad_sch"
    f.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")
    out = await _make_tool(stub).run(PcbExportPosInput(pcb_path=f))
    assert out.status == "pcb_not_found"


@pytest.mark.asyncio
async def test_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 7)
    out = await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb))
    assert out.status == "cli_failed"
    assert out.note is not None and "exited 7" in out.note


@pytest.mark.asyncio
async def test_output_missing_when_empty(tmp_path: Path) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_contents(stub, "")
    out = await _make_tool(stub).run(PcbExportPosInput(pcb_path=pcb))
    assert out.status == "output_missing"
    assert out.note is not None and "zero bytes" in out.note


@pytest.mark.asyncio
async def test_output_missing_when_not_written(tmp_path: Path) -> None:
    """CLI exits 0 but writes nothing."""
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

    tool = PcbExportPosTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    out = await tool.run(PcbExportPosInput(pcb_path=pcb))
    assert out.status == "output_missing"
    assert out.note is not None and "not on disk" in out.note


# -- DI --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_dry_run(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportPosTool()
    out = await tool.run(PcbExportPosInput(pcb_path=pcb, dry_run=True))
    assert out.status == "dry_run"


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    from kimcp._types import Backend, ToolClass

    tool = PcbExportPosTool()
    assert tool.name == "pcb_export_pos"
    assert tool.classification == ToolClass.MUTATE
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})

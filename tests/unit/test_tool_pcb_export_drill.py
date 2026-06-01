"""Unit tests for the ``pcb_export_drill`` built-in tool (M8).

Exercises the full status matrix (ok / dry_run / pcb_not_found /
cli_failed / no_files_produced) plus the argv surface (drill_format,
drill_origin, excellon_* flags, generate_map + map_format) and the
``kind`` inference on produced filenames.

The shell stub is a sibling of the one in ``test_tool_pcb_export_gerbers.py``:
a Python script masquerading as ``kicad-cli`` that handles two
subcommands — ``version`` (so ``CliBackend.probe()`` succeeds) and
``pcb export drill`` (parses argv, records it, and writes whatever
fake drill files the test staged into the ``--output`` directory).
The two stubs intentionally don't share a fixture: each test file reads
top-to-bottom, the stub is short, and a premature abstraction would
hide the surface the test actually drives. Same rationale documented
in ``test_tool_ipc_get_version.py``.
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
from kimcp.tools.builtin.pcb_export_drill import (
    PcbExportDrillInput,
    PcbExportDrillTool,
)

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``pcb export drill ...`` — records the invocation to
      ``<stub>.argv`` (JSON), reads ``<stub>.files`` (JSON list of
      ``{name, contents}`` dicts), writes each file into the
      ``--output`` directory, and exits with the integer in
      ``<stub>.exit`` (0 if absent).

    Tests stage ``<stub>.files`` / ``<stub>.exit`` before running.
    """
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
                    out_dir = argv[i + 1]
                    break

            files_file = here.parent / (here.name + ".files")
            if out_dir is not None and files_file.exists():
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                spec = json.loads(files_file.read_text(encoding="utf-8"))
                for entry in spec:
                    Path(out_dir, entry["name"]).write_text(
                        entry.get("contents", ""), encoding="utf-8"
                    )

            exit_file = here.parent / (here.name + ".exit")
            exit_code = 0
            if exit_file.exists():
                try:
                    exit_code = int(exit_file.read_text(encoding="utf-8").strip())
                except ValueError:
                    exit_code = 0

            if exit_code != 0:
                sys.stderr.write("synthetic kicad-cli failure\\n")
            sys.exit(exit_code)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _stage_files(stub: Path, files: list[dict[str, str]]) -> None:
    """Stage the list of fake drill files the stub should produce."""
    (stub.parent / (stub.name + ".files")).write_text(
        json.dumps(files), encoding="utf-8"
    )


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> PcbExportDrillTool:
    tool = PcbExportDrillTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# The default kicad-cli drill output: one combined Excellon file per board.
_DEFAULT_FILE_SPEC: list[dict[str, str]] = [
    {"name": "board.drl", "contents": "M48\n;DRILL file\n%\n"},
]


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_with_default_flags(tmp_path: Path) -> None:
    """Happy path: combined .drl file → status='ok', total_files=1, kind='combined'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.pcb_path == str(pcb.resolve())
    # Output dir defaults to sibling 'drill/' next to the .kicad_pcb.
    assert out.output_dir == str((pcb.parent / "drill").resolve())
    assert out.total_files == 1
    assert out.total_bytes > 0
    assert out.note is None
    assert out.generated_files[0].kind == "combined"


@pytest.mark.asyncio
async def test_kind_inferred_pth_npth_with_separate_th(tmp_path: Path) -> None:
    """`-PTH.drl` / `-NPTH.drl` filenames surface as kind='pth' / 'npth'.

    Pins the split-file mapping that mirrors how KiCAD names separate
    PTH/NPTH Excellon outputs. Loud regression guard if the filename
    convention drifts.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(
        stub,
        [
            {"name": "board-PTH.drl", "contents": "M48\n"},
            {"name": "board-NPTH.drl", "contents": "M48\n"},
        ],
    )

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportDrillInput(pcb_path=pcb, excellon_separate_th=True)
    )

    assert out.status == "ok"
    assert out.total_files == 2
    by_name = {Path(f.path).name: f for f in out.generated_files}
    assert by_name["board-PTH.drl"].kind == "pth"
    assert by_name["board-NPTH.drl"].kind == "npth"


@pytest.mark.asyncio
async def test_kind_inferred_map_for_drl_map_files(tmp_path: Path) -> None:
    """`<board>-drl_map.<ext>` is classified as kind='map'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(
        stub,
        [
            {"name": "board.drl", "contents": "M48\n"},
            {"name": "board-drl_map.pdf", "contents": "%PDF-1.4\n"},
        ],
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb, generate_map=True))

    by_name = {Path(f.path).name: f for f in out.generated_files}
    assert by_name["board.drl"].kind == "combined"
    assert by_name["board-drl_map.pdf"].kind == "map"
    assert by_name["board-drl_map.pdf"].extension == "pdf"


@pytest.mark.asyncio
async def test_kind_is_none_for_unrecognised_filename(tmp_path: Path) -> None:
    """Filenames that don't match any convention report kind=None.

    Safety valve for schema drift — if kicad-cli ever renames drill
    output, the tool keeps reporting the file (the envelope stays
    parseable) but surfaces ``None`` rather than a wrong classifier.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "unexpected-artifact.xyz", "contents": "?"}])

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.generated_files[0].kind is None
    assert out.generated_files[0].extension == "xyz"


@pytest.mark.asyncio
async def test_generated_file_size_matches_disk(tmp_path: Path) -> None:
    """`size_bytes` reports the actual on-disk byte count."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board.drl", "contents": "abcd"}])

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))

    assert out.total_files == 1
    f = out.generated_files[0]
    assert f.size_bytes == len("abcd")
    assert out.total_bytes == len("abcd")


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_carries_default_flags(tmp_path: Path) -> None:
    """All defaults: kicad-cli sees excellon format, inches, decimal, absolute origin."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(PcbExportDrillInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert argv[:3] == ["pcb", "export", "drill"]
    assert argv[argv.index("--format") + 1] == "excellon"
    assert argv[argv.index("--drill-origin") + 1] == "absolute"
    assert argv[argv.index("--excellon-units") + 1] == "in"
    assert argv[argv.index("--excellon-zeros-format") + 1] == "decimal"
    assert argv[argv.index("--excellon-oval-format") + 1] == "alternate"
    # Default booleans should NOT emit their flags.
    assert "--excellon-mirror-y" not in argv
    assert "--excellon-min-header" not in argv
    assert "--excellon-separate-th" not in argv
    assert "--generate-map" not in argv
    # Board path is always the last positional.
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_carries_drill_format_gerber(tmp_path: Path) -> None:
    """`drill_format='gerber'` is forwarded as `--format gerber`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-PTH.gbr", "contents": "%\n"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportDrillInput(pcb_path=pcb, drill_format="gerber"))

    argv = _read_argv(stub)
    assert argv[argv.index("--format") + 1] == "gerber"


@pytest.mark.asyncio
async def test_argv_carries_separate_th_flag(tmp_path: Path) -> None:
    """`excellon_separate_th=True` adds `--excellon-separate-th`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-PTH.drl"}, {"name": "board-NPTH.drl"}])

    tool = _make_tool(stub)
    await tool.run(
        PcbExportDrillInput(pcb_path=pcb, excellon_separate_th=True)
    )

    argv = _read_argv(stub)
    assert "--excellon-separate-th" in argv


@pytest.mark.asyncio
async def test_argv_generate_map_adds_both_flags(tmp_path: Path) -> None:
    """`generate_map=True` adds `--generate-map` and `--map-format <fmt>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(
        stub,
        [
            {"name": "board.drl"},
            {"name": "board-drl_map.gbr"},
        ],
    )

    tool = _make_tool(stub)
    await tool.run(
        PcbExportDrillInput(
            pcb_path=pcb, generate_map=True, map_format="gerberx2"
        )
    )

    argv = _read_argv(stub)
    assert "--generate-map" in argv
    assert argv[argv.index("--map-format") + 1] == "gerberx2"


@pytest.mark.asyncio
async def test_argv_omits_map_flags_when_disabled(tmp_path: Path) -> None:
    """Default `generate_map=False` → no `--generate-map` or `--map-format`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(PcbExportDrillInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert "--generate-map" not in argv
    assert "--map-format" not in argv


@pytest.mark.asyncio
async def test_argv_carries_mirror_and_min_header(tmp_path: Path) -> None:
    """`excellon_mirror_y` / `excellon_min_header` map straight through."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(
        PcbExportDrillInput(
            pcb_path=pcb,
            excellon_mirror_y=True,
            excellon_min_header=True,
        )
    )

    argv = _read_argv(stub)
    assert "--excellon-mirror-y" in argv
    assert "--excellon-min-header" in argv


@pytest.mark.asyncio
async def test_argv_carries_excellon_units_mm(tmp_path: Path) -> None:
    """`excellon_units='mm'` replaces the default 'in' in argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(PcbExportDrillInput(pcb_path=pcb, excellon_units="mm"))

    argv = _read_argv(stub)
    assert argv[argv.index("--excellon-units") + 1] == "mm"


@pytest.mark.asyncio
async def test_argv_carries_drill_origin_plot(tmp_path: Path) -> None:
    """`drill_origin='plot'` replaces the default 'absolute'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(PcbExportDrillInput(pcb_path=pcb, drill_origin="plot"))

    argv = _read_argv(stub)
    assert argv[argv.index("--drill-origin") + 1] == "plot"


# -- output dir handling ---------------------------------------------------


@pytest.mark.asyncio
async def test_custom_output_dir_is_used(tmp_path: Path) -> None:
    """A caller-supplied `output_dir` is honored (not the default)."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)
    custom = tmp_path / "fab_outputs" / "rev_a" / "drill"

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportDrillInput(pcb_path=pcb, output_dir=custom)
    )

    assert out.status == "ok"
    assert out.output_dir == str(custom.resolve())
    argv = _read_argv(stub)
    assert argv[argv.index("--output") + 1] == str(custom.resolve())


@pytest.mark.asyncio
async def test_dirty_output_dir_emits_warning_but_succeeds(tmp_path: Path) -> None:
    """Pre-existing files trigger a meta.warnings entry but don't crash.

    Pins the contract: we never silently mix old and new drill files in
    the file list. The warning is the loud signal; generated_files reports
    only artifacts new to *this* call.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out_dir = tmp_path / "drill_dirty"
    out_dir.mkdir()
    (out_dir / "stale.drl").write_text("from a previous build", encoding="utf-8")
    _stage_files(stub, [{"name": "board.drl", "contents": "fresh"}])

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportDrillInput(pcb_path=pcb, output_dir=out_dir)
    )

    assert out.status == "ok"
    names = [Path(f.path).name for f in out.generated_files]
    assert names == ["board.drl"]
    assert (out_dir / "stale.drl").exists()
    assert any("already contained" in w for w in out.meta.warnings), out.meta.warnings


# -- dry-run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_skips_cli_invocation(tmp_path: Path) -> None:
    """`dry_run=True` returns the planned argv without touching the filesystem.

    Pins the ADR-0008 contract: every mutating tool supports dry-run, and
    dry-run never invokes kicad-cli (so no `.argv` sidecar appears) and
    never creates the output directory.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)
    out_dir = tmp_path / "drill_dry"

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportDrillInput(pcb_path=pcb, output_dir=out_dir, dry_run=True)
    )

    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "drill" in out.cli_argv
    assert out.generated_files == []
    assert out.note is not None and "dry_run" in out.note
    # The CLI was never invoked …
    assert not (stub.parent / (stub.name + ".argv")).exists()
    # … and the output dir was NOT created.
    assert not out_dir.exists()


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_pcb_not_found_when_path_missing(tmp_path: Path) -> None:
    """Missing file short-circuits BEFORE kicad-cli is invoked."""
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)

    out = await tool.run(
        PcbExportDrillInput(pcb_path=tmp_path / "nope.kicad_pcb")
    )

    assert out.status == "pcb_not_found"
    assert out.pcb_path is None
    assert out.note is not None and "no such file" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_pcb_not_found_when_wrong_extension(tmp_path: Path) -> None:
    """Non-.kicad_pcb input is rejected with a hint."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=sch))

    assert out.status == "pcb_not_found"
    assert out.pcb_path == str(sch.resolve())
    assert out.note is not None and ".kicad_pcb" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 5)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))

    assert out.status == "cli_failed"
    assert out.pcb_path == str(pcb.resolve())
    assert out.cli_argv is not None
    assert out.note is not None
    assert "exited 5" in out.note
    assert "synthetic kicad-cli failure" in out.note


@pytest.mark.asyncio
async def test_status_no_files_produced_when_stub_creates_nothing(
    tmp_path: Path,
) -> None:
    """Exit 0 but no files appeared → status='no_files_produced'.

    Defensive verdict — unusual in practice since kicad-cli usually fails
    loudly when a board has nothing to drill, but we surface it explicitly
    so callers don't read an empty `generated_files` as success.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [])  # stub creates no files

    tool = _make_tool(stub)
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))

    assert out.status == "no_files_produced"
    assert out.generated_files == []
    assert out.cli_argv is not None
    assert out.note is not None and "no new files" in out.note


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: ``run`` must build its own backend lazily.

    Same DI contract as ``pcb_export_gerbers``: the server normally
    injects, but tools loaded via entry points before dispatcher wiring
    (or driven from tests directly) must still produce a valid envelope.
    The default backend won't find any kicad-cli on the test tmpdir, so
    we assert only the *shape*.
    """
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportDrillTool()  # no set_cli_backend call
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb))
    assert out.status in {"ok", "cli_failed", "no_files_produced"}
    assert out.pcb_path == str(pcb.resolve())


@pytest.mark.asyncio
async def test_tool_without_injection_dry_run_returns_argv(tmp_path: Path) -> None:
    """Dry-run never needs a CLI binary — it must succeed under any host."""
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportDrillTool()  # no set_cli_backend call
    out = await tool.run(PcbExportDrillInput(pcb_path=pcb, dry_run=True))

    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "drill" in out.cli_argv

"""Unit tests for the ``pcb_export_gerbers`` built-in tool (M7).

Exercises the full status matrix (ok / dry_run / pcb_not_found /
cli_failed / no_files_produced) plus the argv surface, file discovery
contract, and the meta-warnings emitted on a dirty output_dir.

The shell stub is a sibling of the one in ``test_tool_pcb_drc.py``: a
Python script masquerading as ``kicad-cli`` that handles two subcommands
— ``version`` (so ``CliBackend.probe()`` succeeds) and ``pcb export
gerbers`` (parses argv, records it, and writes whatever fake gerber
files the test staged into the ``--output`` directory). The two stubs
intentionally don't share a fixture: each test file reads top-to-bottom,
the stub is short, and a premature abstraction would hide the surface
the test actually drives. Same rationale documented in
``test_tool_ipc_get_version.py``.
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
from kimcp.tools.builtin.pcb_export_gerbers import (
    PcbExportGerbersInput,
    PcbExportGerbersTool,
)

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``pcb export gerbers ...`` — records the invocation to
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
    """Stage the list of fake gerber files the stub should produce."""
    (stub.parent / (stub.name + ".files")).write_text(
        json.dumps(files), encoding="utf-8"
    )


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> PcbExportGerbersTool:
    tool = PcbExportGerbersTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# A canonical 9-layer fab set the stub can produce in one line.
_DEFAULT_FILE_SPEC: list[dict[str, str]] = [
    {"name": "board-F_Cu.gbr", "contents": "G04 F.Cu*\n"},
    {"name": "board-B_Cu.gbr", "contents": "G04 B.Cu*\n"},
    {"name": "board-F_Mask.gbr", "contents": "G04 F.Mask*\n"},
    {"name": "board-B_Mask.gbr", "contents": "G04 B.Mask*\n"},
    {"name": "board-F_SilkS.gbr", "contents": "G04 F.SilkS*\n"},
    {"name": "board-B_SilkS.gbr", "contents": "G04 B.SilkS*\n"},
    {"name": "board-F_Paste.gbr", "contents": "G04 F.Paste*\n"},
    {"name": "board-B_Paste.gbr", "contents": "G04 B.Paste*\n"},
    {"name": "board-Edge_Cuts.gbr", "contents": "G04 Edge.Cuts*\n"},
    {"name": "board-job.gbrjob", "contents": "{}\n"},
]


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_with_default_layers(tmp_path: Path) -> None:
    """Happy path: 9 layers + job file → status='ok', total_files=10."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.pcb_path == str(pcb.resolve())
    # Output dir defaults to sibling 'gerbers/' next to the .kicad_pcb.
    assert out.output_dir == str((pcb.parent / "gerbers").resolve())
    assert out.total_files == 10
    assert out.total_bytes > 0
    assert out.note is None
    # Files come back sorted by name for determinism.
    names = [Path(f.path).name for f in out.generated_files]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_layer_hint_decoded_from_filename(tmp_path: Path) -> None:
    """`F_Cu` in the filename surfaces as `F.Cu` in the envelope.

    Pins the underscore-to-dot mapping that mirrors how KiCAD encodes
    layer names on disk. Loud regression guard if the filename
    convention or our regex drifts.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    by_name = {Path(f.path).name: f for f in out.generated_files}
    assert by_name["board-F_Cu.gbr"].layer_hint == "F.Cu"
    assert by_name["board-Edge_Cuts.gbr"].layer_hint == "Edge.Cuts"
    # The job file doesn't fit the <board>-<layer>.<ext> convention; hint stays None.
    assert by_name["board-job.gbrjob"].layer_hint is None
    assert by_name["board-job.gbrjob"].extension == "gbrjob"


@pytest.mark.asyncio
async def test_generated_file_size_matches_disk(tmp_path: Path) -> None:
    """`size_bytes` reports the actual on-disk byte count."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr", "contents": "abc"}])

    tool = _make_tool(stub)
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    assert out.total_files == 1
    f = out.generated_files[0]
    assert f.size_bytes == len("abc")
    assert out.total_bytes == len("abc")


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_carries_default_layers(tmp_path: Path) -> None:
    """`layers=None` → kicad-cli sees the canonical 9-layer set as a CSV."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, _DEFAULT_FILE_SPEC)

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert argv[:3] == ["pcb", "export", "gerbers"]
    layers_idx = argv.index("--layers")
    layers_csv = argv[layers_idx + 1]
    assert "F.Cu" in layers_csv
    assert "Edge.Cuts" in layers_csv
    # 9 layers in the default set.
    assert len(layers_csv.split(",")) == 9
    # Board path is always the last positional.
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_carries_custom_layers(tmp_path: Path) -> None:
    """A custom `layers` list is forwarded verbatim as a CSV."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}, {"name": "board-B_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb, layers=["F.Cu", "B.Cu"]))

    argv = _read_argv(stub)
    layers_idx = argv.index("--layers")
    assert argv[layers_idx + 1] == "F.Cu,B.Cu"


@pytest.mark.asyncio
async def test_argv_inverts_no_protel_when_disabled(tmp_path: Path) -> None:
    """`use_protel_extensions=False` adds `--no-protel-ext` to argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(
        PcbExportGerbersInput(pcb_path=pcb, use_protel_extensions=False)
    )

    argv = _read_argv(stub)
    assert "--no-protel-ext" in argv


@pytest.mark.asyncio
async def test_argv_omits_no_protel_when_enabled(tmp_path: Path) -> None:
    """Default `use_protel_extensions=True` → no `--no-protel-ext` flag."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert "--no-protel-ext" not in argv


@pytest.mark.asyncio
async def test_argv_inverts_x2_when_disabled(tmp_path: Path) -> None:
    """`use_x2_format=False` adds `--no-x2`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb, use_x2_format=False))

    argv = _read_argv(stub)
    assert "--no-x2" in argv


@pytest.mark.asyncio
async def test_argv_carries_precision(tmp_path: Path) -> None:
    """`precision` is forwarded as `--precision <N>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb, precision=5))

    argv = _read_argv(stub)
    idx = argv.index("--precision")
    assert argv[idx + 1] == "5"


@pytest.mark.asyncio
async def test_argv_carries_variant(tmp_path: Path) -> None:
    """A non-empty `variant` adds `--variant <name>`; empty omits it."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb, variant="prod"))

    argv = _read_argv(stub)
    idx = argv.index("--variant")
    assert argv[idx + 1] == "prod"


@pytest.mark.asyncio
async def test_argv_omits_variant_when_blank(tmp_path: Path) -> None:
    """Blank `variant` → no `--variant` flag (kicad-cli rejects empty value)."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    argv = _read_argv(stub)
    assert "--variant" not in argv


@pytest.mark.asyncio
async def test_argv_carries_check_zones(tmp_path: Path) -> None:
    """`check_zones=True` adds `--check-zones`."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(PcbExportGerbersInput(pcb_path=pcb, check_zones=True))

    argv = _read_argv(stub)
    assert "--check-zones" in argv


@pytest.mark.asyncio
async def test_argv_carries_exclude_flags(tmp_path: Path) -> None:
    """`exclude_refdes` / `exclude_value` / `subtract_soldermask` map straight through."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])

    tool = _make_tool(stub)
    await tool.run(
        PcbExportGerbersInput(
            pcb_path=pcb,
            exclude_refdes=True,
            exclude_value=True,
            subtract_soldermask=True,
        )
    )

    argv = _read_argv(stub)
    assert "--exclude-refdes" in argv
    assert "--exclude-value" in argv
    assert "--subtract-soldermask" in argv


# -- output dir handling ---------------------------------------------------


@pytest.mark.asyncio
async def test_custom_output_dir_is_used(tmp_path: Path) -> None:
    """A caller-supplied `output_dir` is honored (not the default)."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [{"name": "board-F_Cu.gbr"}])
    custom = tmp_path / "fab_outputs" / "rev_a"

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportGerbersInput(pcb_path=pcb, output_dir=custom)
    )

    assert out.status == "ok"
    assert out.output_dir == str(custom.resolve())
    # Stub got the same path on the command line.
    argv = _read_argv(stub)
    idx = argv.index("--output")
    assert argv[idx + 1] == str(custom.resolve())


@pytest.mark.asyncio
async def test_dirty_output_dir_emits_warning_but_succeeds(tmp_path: Path) -> None:
    """Pre-existing files trigger a meta.warnings entry but don't crash.

    Pins the contract: we never silently mix old and new gerbers in the
    file list. The warning is the loud signal; generated_files reports
    only artifacts new to *this* call.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out_dir = tmp_path / "gerbers_dirty"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("from a previous build", encoding="utf-8")
    _stage_files(stub, [{"name": "board-F_Cu.gbr", "contents": "fresh"}])

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportGerbersInput(pcb_path=pcb, output_dir=out_dir)
    )

    assert out.status == "ok"
    # Only the freshly-produced file is in generated_files; stale.txt is
    # left on disk untouched.
    names = [Path(f.path).name for f in out.generated_files]
    assert names == ["board-F_Cu.gbr"]
    assert (out_dir / "stale.txt").exists()
    # And the warning fired.
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
    out_dir = tmp_path / "gerbers_dry"

    tool = _make_tool(stub)
    out = await tool.run(
        PcbExportGerbersInput(pcb_path=pcb, output_dir=out_dir, dry_run=True)
    )

    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "pcb" in out.cli_argv and "gerbers" in out.cli_argv
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
        PcbExportGerbersInput(pcb_path=tmp_path / "nope.kicad_pcb")
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
    out = await tool.run(PcbExportGerbersInput(pcb_path=sch))

    assert out.status == "pcb_not_found"
    assert out.pcb_path == str(sch.resolve())
    assert out.note is not None and ".kicad_pcb" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 3)

    tool = _make_tool(stub)
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    assert out.status == "cli_failed"
    assert out.pcb_path == str(pcb.resolve())
    assert out.cli_argv is not None  # populated even on failure
    assert out.note is not None
    assert "exited 3" in out.note
    assert "synthetic kicad-cli failure" in out.note


@pytest.mark.asyncio
async def test_status_no_files_produced_when_stub_creates_nothing(
    tmp_path: Path,
) -> None:
    """Exit 0 but no files appeared → status='no_files_produced'.

    Defensive verdict — kicad-cli usually fails loudly when nothing
    plots, but a layer list that resolves to "nothing on this board"
    can still produce this state. We surface it explicitly so callers
    don't read an empty `generated_files` as success.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_files(stub, [])  # stub creates no files

    tool = _make_tool(stub)
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))

    assert out.status == "no_files_produced"
    assert out.generated_files == []
    assert out.cli_argv is not None
    assert out.note is not None and "no new files" in out.note


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: ``run`` must build its own backend lazily.

    Same DI contract as ``pcb_drc``: the server normally injects, but
    tools loaded via entry points before dispatcher wiring (or driven
    from tests directly) must still produce a valid envelope. The
    default backend won't find any kicad-cli on the test tmpdir, so
    we assert only the *shape*.
    """
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportGerbersTool()  # no set_cli_backend call
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb))
    # Either dry-runnable code path resolves cleanly OR cli_failed when
    # the lazy backend can't find kicad-cli on the test host.
    assert out.status in {"ok", "cli_failed", "no_files_produced"}
    assert out.pcb_path == str(pcb.resolve())


@pytest.mark.asyncio
async def test_tool_without_injection_dry_run_returns_argv(tmp_path: Path) -> None:
    """Dry-run never needs a CLI binary — it must succeed under any host."""
    pcb = _touch_pcb(tmp_path)
    tool = PcbExportGerbersTool()  # no set_cli_backend call
    out = await tool.run(PcbExportGerbersInput(pcb_path=pcb, dry_run=True))

    assert out.status == "dry_run"
    assert out.cli_argv is not None
    assert "gerbers" in out.cli_argv

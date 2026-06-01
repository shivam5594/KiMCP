"""Unit tests for the ``sch_export_netlist`` built-in tool (M10).

Mirrors the ``test_tool_pcb_export_drill.py`` shape: a Python shell stub
masquerading as ``kicad-cli`` handles ``version`` + ``sch export netlist …``,
reading whatever ``<stub>.payload`` the test staged and writing it to
whatever ``-o <path>`` the tool passes. Exit code comes from
``<stub>.exit`` (default 0), letting each test shape the failure mode
without a real ``kicad-cli`` on $PATH.

Test coverage spans the full status matrix (ok / dry_run / sch_not_found
/ cli_failed / no_file_produced) plus netlist-specific paths:

* All eight formats round-trip through argv.
* Default output_path honors the format → extension mapping.
* Overwrite-warning fires when output_path already existed.

The stub is a local copy rather than a shared fixture — same rationale
as the sibling schematic/PCB tests: it's cheap, tests read
top-to-bottom, and the structure diverges slightly per tool (argv shape,
status enum).
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
from kimcp.tools.builtin.sch_export_netlist import (
    SchExportNetlistInput,
    SchExportNetlistTool,
)

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``sch export netlist ...`` — records argv to ``<stub>.argv``,
      reads ``<stub>.payload`` and writes it to whatever ``-o <path>``
      was passed, and exits with ``<stub>.exit`` (0 if absent). If no
      payload is staged the stub is silent — exercises the
      no_file_produced path.
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

            out_path = None
            for i, a in enumerate(argv):
                if a == "-o" and i + 1 < len(argv):
                    out_path = argv[i + 1]
                    break

            payload_file = here.parent / (here.name + ".payload")
            if out_path is not None and payload_file.exists():
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(
                    payload_file.read_text(encoding="utf-8"), encoding="utf-8"
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


def _stage_payload(stub: Path, payload: str) -> None:
    (stub.parent / (stub.name + ".payload")).write_text(payload, encoding="utf-8")


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> SchExportNetlistTool:
    tool = SchExportNetlistTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_sch(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    sch = tmp_path / name
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return sch


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_with_default_format_and_path(tmp_path: Path) -> None:
    """Default format=kicadsexpr → writes `<sch_stem>.net` next to the schematic."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "(export (version D) (design))\n")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))

    assert out.status == "ok"
    expected = sch.with_suffix(".net")
    assert out.output_path == str(expected)
    assert expected.is_file()
    assert out.size_bytes > 0
    assert out.format == "kicadsexpr"
    assert out.sch_path == str(sch.resolve())
    assert out.note is None
    assert out.meta.warnings == []


@pytest.mark.asyncio
async def test_status_ok_with_explicit_output_path(tmp_path: Path) -> None:
    """Caller-provided `output_path` overrides the format-derived default."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    target = tmp_path / "artifacts" / "custom.netlist"
    _stage_payload(stub, "(export (design))\n")

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportNetlistInput(sch_path=sch, output_path=target)
    )

    assert out.status == "ok"
    assert out.output_path == str(target.resolve())
    assert target.is_file()
    # Parent dir was created on demand.
    assert target.parent.is_dir()


# -- format → extension mapping (netlist-specific) -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "expected_ext"),
    [
        ("kicadsexpr", "net"),
        ("kicadxml", "xml"),
        ("cadstar", "frp"),
        ("orcadpcb2", "net"),
        ("spice", "cir"),
        ("spicemodel", "lib"),
        ("pads", "asc"),
        ("allegro", "net"),
    ],
)
async def test_default_output_path_uses_format_extension(
    tmp_path: Path, fmt: str, expected_ext: str
) -> None:
    """Default output_path honors format → extension mapping."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "payload\n")

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportNetlistInput(sch_path=sch, format=fmt)  # type: ignore[arg-type]
    )

    assert out.status == "ok"
    expected = sch.with_suffix("." + expected_ext)
    assert out.output_path == str(expected)
    # And the format echo in the envelope matches the request.
    assert out.format == fmt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt",
    [
        "kicadsexpr",
        "kicadxml",
        "cadstar",
        "orcadpcb2",
        "spice",
        "spicemodel",
        "pads",
        "allegro",
    ],
)
async def test_argv_carries_format(tmp_path: Path, fmt: str) -> None:
    """All 8 formats round-trip through `--format <value>` in argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "payload\n")

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportNetlistInput(sch_path=sch, format=fmt)  # type: ignore[arg-type]
    )
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "netlist"]
    fmt_idx = argv.index("--format")
    assert argv[fmt_idx + 1] == fmt


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_shape(tmp_path: Path) -> None:
    """Default argv: `sch export netlist --format kicadsexpr -o <out> <sch>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "payload\n")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "netlist"]
    assert "--format" in argv
    assert "-o" in argv
    # Schematic is the last positional — tool appends it after the flags.
    assert argv[-1] == str(sch.resolve())
    # cli_argv on envelope matches what the stub saw (order + values).
    assert out.cli_argv == argv


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_sch_not_found_when_path_missing(tmp_path: Path) -> None:
    """Missing file short-circuits BEFORE kicad-cli is invoked."""
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)

    out = await tool.run(
        SchExportNetlistInput(sch_path=tmp_path / "nope.kicad_sch")
    )

    assert out.status == "sch_not_found"
    assert out.sch_path is None
    assert out.note is not None and "no such file" in out.note
    # Stub was never invoked.
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_sch_not_found_when_wrong_extension(tmp_path: Path) -> None:
    """Non-.kicad_sch input is rejected — guards against passing the PCB."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=pcb))

    assert out.status == "sch_not_found"
    assert out.sch_path == str(pcb.resolve())
    assert out.note is not None and ".kicad_sch" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_exit(stub, 3)

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))

    assert out.status == "cli_failed"
    assert out.sch_path == str(sch.resolve())
    assert out.note is not None
    assert "exited 3" in out.note
    assert "synthetic kicad-cli failure" in out.note
    # argv still populated on failure so callers can reproduce the invocation.
    assert out.cli_argv is not None and out.cli_argv[:3] == ["sch", "export", "netlist"]


@pytest.mark.asyncio
async def test_status_no_file_produced_when_cli_didnt_write(tmp_path: Path) -> None:
    """kicad-cli exits 0 but output_path is absent → status='no_file_produced'.

    Staging no `<stub>.payload` makes the stub a no-op writer; the
    absence of the output file is the trigger for this defensive status.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    # No payload staged.

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))

    assert out.status == "no_file_produced"
    assert out.output_path == str(sch.with_suffix(".net"))
    assert out.note is not None and "no file appeared" in out.note
    assert out.size_bytes == 0


# -- dry-run (ADR-0008) ----------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_short_circuits_without_writing(tmp_path: Path) -> None:
    """`dry_run=True` returns argv + resolved output_path, no subprocess."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    # Stage a payload — if dry_run ever leaked through to the stub we'd
    # see the file appear, which would fail the is_file() check below.
    _stage_payload(stub, "payload\n")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch, dry_run=True))

    assert out.status == "dry_run"
    assert out.output_path == str(sch.with_suffix(".net"))
    assert out.cli_argv is not None
    assert out.cli_argv[:3] == ["sch", "export", "netlist"]
    # CLI was never invoked — no argv sidecar, no output file.
    assert not (stub.parent / (stub.name + ".argv")).exists()
    assert not sch.with_suffix(".net").exists()


# -- overwrite warning -----------------------------------------------------


@pytest.mark.asyncio
async def test_overwrite_warning_when_output_path_existed(tmp_path: Path) -> None:
    """Pre-existing file at output_path → meta.warnings entry on overwrite.

    Pins the LLM-safety contract: re-exporting in place is allowed (we
    don't fail) but the fact that we clobbered something is surfaced on
    the envelope so hosts can show it in audit trails.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "fresh payload\n")

    # Pre-create the target file.
    target = sch.with_suffix(".net")
    target.write_text("stale prior export\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))

    assert out.status == "ok"
    assert any("already existed" in w for w in out.meta.warnings), out.meta.warnings
    # Content was replaced.
    assert target.read_text(encoding="utf-8") == "fresh payload\n"


@pytest.mark.asyncio
async def test_no_overwrite_warning_for_fresh_export(tmp_path: Path) -> None:
    """No pre-existing file → no overwrite warning (clean audit trail)."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "payload\n")

    tool = _make_tool(stub)
    out = await tool.run(SchExportNetlistInput(sch_path=sch))

    assert out.status == "ok"
    assert not any("already existed" in w for w in out.meta.warnings)


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: `run` must build its own backend lazily.

    The server normally injects via `set_cli_backend` but tools discovered
    via entry points before dispatcher wiring (or exercised from tests
    directly) must still produce a valid envelope. Here the default
    backend won't find any kicad-cli on the test tmpdir, so we assert
    only the *shape* — a status envelope and a `sch_path` mirror.
    """
    sch = _touch_sch(tmp_path)
    tool = SchExportNetlistTool()  # no set_cli_backend call
    out = await tool.run(SchExportNetlistInput(sch_path=sch))
    assert out.status in {"ok", "cli_failed", "no_file_produced"}
    assert out.sch_path == str(sch.resolve())

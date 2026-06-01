"""Unit tests for the ``sch_export_bom`` built-in tool (M11).

Mirrors ``test_tool_sch_export_netlist.py`` — a Python shell stub acts
as ``kicad-cli``, handling ``version`` + ``sch export bom …``. Covers
the full status matrix (ok / dry_run / sch_not_found / cli_failed /
no_file_produced) and BOM-specific paths:

* All four formats round-trip through ``--format-preset``.
* Default output_path honors the format → extension mapping.
* ``preset`` + ``exclude_dnp`` argv plumbing.
* Overwrite-warning fires when output_path already existed.

The stub is a local copy rather than a shared fixture — same rationale
as the sibling tests: it's cheap, tests read top-to-bottom, and the
structure diverges slightly per tool.
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
from kimcp.tools.builtin.sch_export_bom import SchExportBomInput, SchExportBomTool

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``sch export bom ...`` — records argv to ``<stub>.argv``, reads
      ``<stub>.payload`` and writes it to whatever ``-o <path>`` was
      passed, and exits with ``<stub>.exit`` (0 if absent). If no
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


def _make_tool(stub: Path) -> SchExportBomTool:
    tool = SchExportBomTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_sch(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    sch = tmp_path / name
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return sch


# A minimal CSV BOM payload — just a header plus one row, enough for
# size_bytes > 0 checks without making the stub know what a BOM is.
_SAMPLE_CSV = "Reference,Value,Footprint\nR1,10k,R_0603\n"


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_with_default_format_and_path(tmp_path: Path) -> None:
    """Default format=csv → writes `<sch_stem>.csv` next to the schematic."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))

    assert out.status == "ok"
    expected = sch.with_suffix(".csv")
    assert out.output_path == str(expected)
    assert expected.is_file()
    assert out.size_bytes > 0
    assert out.format == "csv"
    assert out.sch_path == str(sch.resolve())
    assert out.note is None
    assert out.meta.warnings == []


@pytest.mark.asyncio
async def test_status_ok_with_explicit_output_path(tmp_path: Path) -> None:
    """Caller-provided `output_path` overrides the format-derived default."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    target = tmp_path / "artifacts" / "custom.bom"
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch, output_path=target))

    assert out.status == "ok"
    assert out.output_path == str(target.resolve())
    assert target.is_file()
    # Parent dir was created on demand.
    assert target.parent.is_dir()


# -- format → extension mapping (BOM-specific) -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "expected_ext"),
    [
        ("csv", "csv"),
        ("tsv", "tsv"),
        ("html", "html"),
        ("xml", "xml"),
    ],
)
async def test_default_output_path_uses_format_extension(
    tmp_path: Path, fmt: str, expected_ext: str
) -> None:
    """Default output_path honors format → extension mapping."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportBomInput(sch_path=sch, format=fmt)  # type: ignore[arg-type]
    )

    assert out.status == "ok"
    expected = sch.with_suffix("." + expected_ext)
    assert out.output_path == str(expected)
    assert out.format == fmt


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["csv", "tsv", "html", "xml"])
async def test_argv_carries_format_preset(tmp_path: Path, fmt: str) -> None:
    """All 4 formats round-trip through `--format-preset <value>` in argv.

    Note: kicad-cli 9.x takes the format as `--format-preset`, not
    `--format` — the flag name is part of the contract we pin here.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportBomInput(sch_path=sch, format=fmt)  # type: ignore[arg-type]
    )
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "bom"]
    fmt_idx = argv.index("--format-preset")
    assert argv[fmt_idx + 1] == fmt


# -- optional knobs (preset / exclude_dnp) --------------------------------


@pytest.mark.asyncio
async def test_argv_omits_preset_by_default(tmp_path: Path) -> None:
    """Without `preset`, `--preset` must NOT appear in argv.

    Pins the None-vs-empty-string contract: passing an empty preset to
    kicad-cli would be a real command, not the same as omitting the
    flag. The tool's None check has to actually suppress the flag.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert "--preset" not in argv


@pytest.mark.asyncio
async def test_argv_carries_preset_when_set(tmp_path: Path) -> None:
    """`preset='Grouped By Value'` flows through as `--preset <name>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportBomInput(sch_path=sch, preset="Grouped By Value")
    )
    assert out.status == "ok"

    argv = _read_argv(stub)
    idx = argv.index("--preset")
    assert argv[idx + 1] == "Grouped By Value"


@pytest.mark.asyncio
async def test_argv_omits_exclude_dnp_by_default(tmp_path: Path) -> None:
    """Default `exclude_dnp=False` → `--exclude-dnp` absent from argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert "--exclude-dnp" not in argv


@pytest.mark.asyncio
async def test_argv_carries_exclude_dnp_when_true(tmp_path: Path) -> None:
    """`exclude_dnp=True` → `--exclude-dnp` flag appears in argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch, exclude_dnp=True))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert "--exclude-dnp" in argv


# -- argv shape ------------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_shape(tmp_path: Path) -> None:
    """Default argv: `sch export bom --format-preset csv -o <out> <sch>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:3] == ["sch", "export", "bom"]
    assert "--format-preset" in argv
    assert "-o" in argv
    # Schematic is the last positional — tool appends it after the flags.
    assert argv[-1] == str(sch.resolve())
    assert out.cli_argv == argv


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_sch_not_found_when_path_missing(tmp_path: Path) -> None:
    """Missing file short-circuits BEFORE kicad-cli is invoked."""
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)

    out = await tool.run(SchExportBomInput(sch_path=tmp_path / "nope.kicad_sch"))

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
    out = await tool.run(SchExportBomInput(sch_path=pcb))

    assert out.status == "sch_not_found"
    assert out.sch_path == str(pcb.resolve())
    assert out.note is not None and ".kicad_sch" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_exit(stub, 4)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))

    assert out.status == "cli_failed"
    assert out.sch_path == str(sch.resolve())
    assert out.note is not None
    assert "exited 4" in out.note
    assert "synthetic kicad-cli failure" in out.note
    assert out.cli_argv is not None and out.cli_argv[:3] == ["sch", "export", "bom"]


@pytest.mark.asyncio
async def test_status_no_file_produced_when_cli_didnt_write(tmp_path: Path) -> None:
    """kicad-cli exits 0 but output_path is absent → status='no_file_produced'."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    # No payload staged → stub is a no-op writer.

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))

    assert out.status == "no_file_produced"
    assert out.output_path == str(sch.with_suffix(".csv"))
    assert out.note is not None and "no file appeared" in out.note
    assert out.size_bytes == 0


# -- dry-run (ADR-0008) ----------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_short_circuits_without_writing(tmp_path: Path) -> None:
    """`dry_run=True` returns argv + resolved output_path, no subprocess.

    Also verifies that `preset` + `exclude_dnp` survive into the
    previewed argv — callers should see exactly what we would invoke.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(
        SchExportBomInput(
            sch_path=sch,
            dry_run=True,
            preset="Fabrication",
            exclude_dnp=True,
        )
    )

    assert out.status == "dry_run"
    assert out.output_path == str(sch.with_suffix(".csv"))
    assert out.cli_argv is not None
    assert "--preset" in out.cli_argv
    assert "Fabrication" in out.cli_argv
    assert "--exclude-dnp" in out.cli_argv
    # CLI was never invoked — no argv sidecar, no output file.
    assert not (stub.parent / (stub.name + ".argv")).exists()
    assert not sch.with_suffix(".csv").exists()


# -- overwrite warning -----------------------------------------------------


@pytest.mark.asyncio
async def test_overwrite_warning_when_output_path_existed(tmp_path: Path) -> None:
    """Pre-existing file at output_path → meta.warnings entry on overwrite."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    target = sch.with_suffix(".csv")
    target.write_text("stale prior BOM\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))

    assert out.status == "ok"
    assert any("already existed" in w for w in out.meta.warnings), out.meta.warnings
    # Content was replaced.
    assert target.read_text(encoding="utf-8") == _SAMPLE_CSV


@pytest.mark.asyncio
async def test_no_overwrite_warning_for_fresh_export(tmp_path: Path) -> None:
    """No pre-existing file → no overwrite warning (clean audit trail)."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, _SAMPLE_CSV)

    tool = _make_tool(stub)
    out = await tool.run(SchExportBomInput(sch_path=sch))

    assert out.status == "ok"
    assert not any("already existed" in w for w in out.meta.warnings)


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: `run` must build its own backend lazily."""
    sch = _touch_sch(tmp_path)
    tool = SchExportBomTool()  # no set_cli_backend call
    out = await tool.run(SchExportBomInput(sch_path=sch))
    assert out.status in {"ok", "cli_failed", "no_file_produced"}
    assert out.sch_path == str(sch.resolve())

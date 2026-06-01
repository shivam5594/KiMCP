"""Unit tests for the ``pcb_drc`` built-in tool (M6).

Exercises the full status matrix (ok / violations / pcb_not_found /
cli_failed / parse_failed) by pointing an injected ``CliBackend`` at a
Python shell stub that pretends to be ``kicad-cli``. The stub handles
two subcommands:

* ``version`` — so ``CliBackend.probe()`` succeeds (min_version gate).
* ``pcb drc … -o <path> <board>`` — parses argv, records it to a
  sibling file for test inspection, and writes whatever "DRC JSON"
  payload the test staged into ``<stub>.payload``. Exit code comes from
  ``<stub>.exit`` (default 0), letting each test shape the failure mode
  without spinning up a real ``kicad-cli``.

The stub is intentionally minimal — just enough shape for KiCAD's DRC
JSON schema as documented in ``pcb_drc.py`` (lists ``violations`` /
``unconnected_items`` / ``schematic_parity`` plus scalars
``coordinate_units`` / ``kicad_version``). Richer fields ride in via
``extra="allow"`` on the Pydantic models, so the stub doesn't have to
track every upstream schema bump.
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
from kimcp.tools.builtin.pcb_drc import PcbDrcInput, PcbDrcTool

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``pcb drc ...`` — records the invocation to ``<stub>.argv`` (JSON),
      reads ``<stub>.payload`` and writes it to whatever ``-o <path>`` was
      passed, and exits with the integer stored in ``<stub>.exit`` (0 if
      the file doesn't exist).

    Tests stage ``<stub>.payload`` / ``<stub>.exit`` before running.
    """
    stub = tmp_path / "kicad-cli-stub"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path

            here = Path(__file__).resolve()
            argv = sys.argv[1:]

            if argv[:1] == ["version"]:
                sys.stdout.write("Application: kicad-cli\\n")
                sys.stdout.write("Version: 9.0.1, release build\\n")
                sys.exit(0)

            # Record the invocation for test assertions.
            (here.parent / (here.name + ".argv")).write_text(
                json.dumps(argv), encoding="utf-8"
            )

            # Resolve the -o <path> target for the DRC report.
            out_path = None
            for i, a in enumerate(argv):
                if a == "-o" and i + 1 < len(argv):
                    out_path = argv[i + 1]
                    break

            payload_file = here.parent / (here.name + ".payload")
            if out_path is not None and payload_file.exists():
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


def _stage_payload(stub: Path, payload: dict[str, object] | str) -> None:
    """Write a DRC JSON payload (or raw string for the parse-fail case)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (stub.parent / (stub.name + ".payload")).write_text(text, encoding="utf-8")


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> PcbDrcTool:
    tool = PcbDrcTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_when_no_violations(tmp_path: Path) -> None:
    """Clean DRC run → status='ok', empty lists, total_count=0."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.total_count == 0
    assert out.violations == []
    assert out.unconnected_items == []
    assert out.schematic_parity_issues == []
    assert out.kicad_version == "9.0.1"
    assert out.coordinate_units == "mm"
    assert out.pcb_path == str(pcb.resolve())
    assert out.note is None


@pytest.mark.asyncio
async def test_status_violations_when_findings_present(tmp_path: Path) -> None:
    """Findings present → status='violations', rule_id mirrors kicad-cli 'type'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {
                    "type": "clearance",
                    "severity": "error",
                    "description": "Clearance violation (track to pad)",
                    "items": [
                        {"description": "track on F.Cu", "uuid": "abc-123"},
                        {"description": "pad 5 of U1", "uuid": "def-456"},
                    ],
                }
            ],
            "unconnected_items": [
                {
                    "type": "unconnected_items",
                    "severity": "error",
                    "description": "Missing connection: /SDA",
                    "items": [],
                }
            ],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "violations"
    assert out.total_count == 2
    assert len(out.violations) == 1
    v = out.violations[0]
    assert v.rule_id == "clearance"
    assert v.severity == "error"
    assert len(v.items) == 2
    assert v.items[0].uuid == "abc-123"
    assert len(out.unconnected_items) == 1


# -- severity filter -------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_floor_warning_filters_info(tmp_path: Path) -> None:
    """Default floor='warning' drops 'info' entries but keeps warnings + errors."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "clearance", "severity": "error", "description": "", "items": []},
                {"type": "track_width", "severity": "warning", "description": "", "items": []},
                {"type": "courtyards", "severity": "info", "description": "", "items": []},
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert {v.severity for v in out.violations} == {"error", "warning"}
    assert out.total_count == 2


@pytest.mark.asyncio
async def test_severity_floor_error_filters_warnings(tmp_path: Path) -> None:
    """Floor='error' drops warnings — the CI-gate default for 'block on errors'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "clearance", "severity": "error", "description": "", "items": []},
                {"type": "track_width", "severity": "warning", "description": "", "items": []},
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb, severity_floor="error"))

    assert out.total_count == 1
    assert out.violations[0].severity == "error"


@pytest.mark.asyncio
async def test_unknown_severity_is_kept(tmp_path: Path) -> None:
    """Unknown severities rank at 99 and are kept.

    Pins the forward-compat contract in ``_parse_violations``: if KiCAD
    adds a new severity tier upstream, we surface it rather than silently
    dropping it.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "future_rule", "severity": "critical", "description": "", "items": []},
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.total_count == 1
    assert out.violations[0].severity == "critical"


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_carries_schematic_parity_flag(tmp_path: Path) -> None:
    """``schematic_parity=True`` appends ``--schematic-parity`` to kicad-cli argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb, schematic_parity=True))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:2] == ["pcb", "drc"]
    assert "--schematic-parity" in argv
    # Board path is the last positional — the tool appends it after flags.
    assert argv[-1] == str(pcb.resolve())


@pytest.mark.asyncio
async def test_argv_carries_units_input(tmp_path: Path) -> None:
    """Input ``units`` is forwarded verbatim as ``--units <value>``."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "in",
            "kicad_version": "9.0.1",
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb, units="in"))
    assert out.status == "ok"

    argv = _read_argv(stub)
    idx = argv.index("--units")
    assert argv[idx + 1] == "in"
    # And no --schematic-parity when the caller didn't opt in.
    assert "--schematic-parity" not in argv


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_pcb_not_found_when_path_missing(tmp_path: Path) -> None:
    """Missing file short-circuits BEFORE kicad-cli is invoked."""
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)

    out = await tool.run(PcbDrcInput(pcb_path=tmp_path / "nope.kicad_pcb"))

    assert out.status == "pcb_not_found"
    assert out.pcb_path is None
    assert out.note is not None and "no such file" in out.note
    # Stub was never invoked — no argv sidecar.
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_pcb_not_found_when_wrong_extension(tmp_path: Path) -> None:
    """Non-.kicad_pcb input is rejected with a hint — guards against
    passing the schematic by accident."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108))\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=sch))

    assert out.status == "pcb_not_found"
    assert out.pcb_path == str(sch.resolve())
    assert out.note is not None and ".kicad_pcb" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_exit(stub, 2)

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "cli_failed"
    assert out.pcb_path == str(pcb.resolve())
    assert out.note is not None
    assert "exited 2" in out.note
    assert "synthetic kicad-cli failure" in out.note


@pytest.mark.asyncio
async def test_status_parse_failed_on_invalid_json(tmp_path: Path) -> None:
    """kicad-cli exit 0 but garbage on disk → status='parse_failed'."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(stub, "this is not json {{{")

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "parse_failed"
    assert out.pcb_path == str(pcb.resolve())
    assert out.note is not None and "not parseable" in out.note


@pytest.mark.asyncio
async def test_status_parse_failed_when_top_level_not_object(tmp_path: Path) -> None:
    """Top-level JSON is an array → defensive type check fires before parsing."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(stub, "[]")

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "parse_failed"
    assert out.note is not None and "top-level" in out.note


# -- schema-drift warnings (KiCAD 10 hardening) ---------------------------


@pytest.mark.asyncio
async def test_meta_warning_when_kicad_version_missing(tmp_path: Path) -> None:
    """Missing `kicad_version` in DRC JSON surfaces a meta.warnings entry.

    Pins the forward-compat contract added for KiCAD 10: the tool stays
    functional when kicad-cli drops or renames audit-trail scalars, but
    downstream dashboards see the drift via the envelope's warnings list
    rather than in silently-empty fields.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            # kicad_version intentionally absent
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "ok"
    assert out.kicad_version == ""
    assert any("kicad_version" in w for w in out.meta.warnings), out.meta.warnings


@pytest.mark.asyncio
async def test_meta_warning_when_rule_id_empty(tmp_path: Path) -> None:
    """A finding without a `type` field raises a drift warning.

    `type` → `rule_id` is the one rename we apply; losing it is the
    loudest possible signal that kicad-cli's schema moved, so we count
    and surface the empty rule_ids explicitly.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                # No `type` field — simulate upstream rename.
                {"severity": "error", "description": "???", "items": []},
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(PcbDrcInput(pcb_path=pcb))

    assert out.status == "violations"
    assert out.violations[0].rule_id == ""
    assert any("rule_id" in w for w in out.meta.warnings), out.meta.warnings


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: ``run`` must build its own backend lazily.

    The server normally injects via ``set_cli_backend`` but tools discovered
    via entry points before dispatcher wiring (or exercised from tests
    directly) must still produce a valid envelope. Here the default backend
    won't find any kicad-cli on the test tmpdir, so we assert only the
    *shape* — a status envelope and a ``pcb_path`` mirror.
    """
    pcb = _touch_pcb(tmp_path)
    tool = PcbDrcTool()  # no set_cli_backend call
    out = await tool.run(PcbDrcInput(pcb_path=pcb))
    assert out.status in {"ok", "violations", "cli_failed", "parse_failed"}
    assert out.pcb_path == str(pcb.resolve())

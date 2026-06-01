"""Unit tests for the ``sch_erc`` built-in tool (M9).

Mirrors the ``test_tool_pcb_drc.py`` shape: a Python shell stub masquerading
as ``kicad-cli`` handles ``version`` + ``sch erc …`` subcommands, staging an
ERC JSON payload into whatever ``-o <path>`` the tool passes and controlling
the exit code via a sidecar file. The stub is intentionally minimal — just
enough to exercise the full status matrix (ok / violations / sch_not_found /
cli_failed / parse_failed) plus ERC-specific paths the DRC tool doesn't have:

* Hierarchical JSON flattening (``sheets[].violations[]`` → flat list with
  ``sheet_path`` preserved).
* ``--units mils`` forwarding (ERC accepts mils; DRC does not).

The stub is a local copy rather than a shared fixture — same rationale as
the sibling PCB tests: it's cheap, tests read top-to-bottom, and the
structure diverges slightly per tool (argv shape, status enum).
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
from kimcp.tools.builtin.sch_erc import SchErcInput, SchErcTool

# -- stub helpers ----------------------------------------------------------


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    """Install a fake ``kicad-cli`` at ``tmp_path/kicad-cli-stub``.

    Handles two subcommands:

    * ``version`` — prints a parseable version line so
      ``CliBackend.probe()`` succeeds.
    * ``sch erc ...`` — records the invocation to ``<stub>.argv`` (JSON),
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
            import json, sys
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

            # Resolve the -o <path> target for the ERC report.
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
    """Write an ERC JSON payload (or raw string for the parse-fail case)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (stub.parent / (stub.name + ".payload")).write_text(text, encoding="utf-8")


def _stage_exit(stub: Path, exit_code: int) -> None:
    (stub.parent / (stub.name + ".exit")).write_text(str(exit_code), encoding="utf-8")


def _read_argv(stub: Path) -> list[str]:
    raw = (stub.parent / (stub.name + ".argv")).read_text(encoding="utf-8")
    return cast(list[str], json.loads(raw))


def _make_tool(stub: Path) -> SchErcTool:
    tool = SchErcTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))
    return tool


def _touch_sch(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    sch = tmp_path / name
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return sch


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_when_no_violations_flat(tmp_path: Path) -> None:
    """Clean ERC run via the flat JSON shape → status='ok', empty violations."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "ok"
    assert out.total_count == 0
    assert out.violations == []
    assert out.kicad_version == "9.0.1"
    assert out.coordinate_units == "mm"
    assert out.sch_path == str(sch.resolve())
    assert out.note is None


@pytest.mark.asyncio
async def test_status_violations_flat_shape(tmp_path: Path) -> None:
    """Findings via the flat shape → status='violations', sheet_path=''."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Input pin not connected",
                    "items": [
                        {"description": "pin 3 of U1", "uuid": "abc-123"},
                    ],
                },
                {
                    "type": "hier_label_mismatch",
                    "severity": "warning",
                    "description": "Hierarchical label type mismatch",
                    "items": [],
                },
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "violations"
    assert out.total_count == 2
    rule_ids = [v.rule_id for v in out.violations]
    assert rule_ids == ["pin_not_connected", "hier_label_mismatch"]
    # Flat shape → sheet_path empty on every entry.
    assert all(v.sheet_path == "" for v in out.violations)
    assert out.violations[0].items[0].uuid == "abc-123"


# -- hierarchical JSON shape (ERC-specific) --------------------------------


@pytest.mark.asyncio
async def test_hierarchical_shape_flattens_with_sheet_path(tmp_path: Path) -> None:
    """Hierarchical ``sheets[]`` shape flattens to one list with sheet_path set.

    Pins the key ERC-vs-DRC divergence: schematics are hierarchical, so
    kicad-cli can group findings per-sheet. The tool flattens into a
    single violations feed but preserves ``sheet_path`` on each entry so
    provenance isn't lost.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "Root sheet pin",
                            "items": [],
                        }
                    ],
                },
                {
                    "path": "/power/",
                    "violations": [
                        {
                            "type": "power_pin_no_driver",
                            "severity": "error",
                            "description": "VCC has no driver",
                            "items": [],
                        },
                        {
                            "type": "hier_label_mismatch",
                            "severity": "warning",
                            "description": "label type differs from parent",
                            "items": [],
                        },
                    ],
                },
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "violations"
    assert out.total_count == 3
    by_sheet = {v.sheet_path for v in out.violations}
    assert by_sheet == {"/", "/power/"}
    # Order within the flattened list preserves sheet traversal order.
    paths = [v.sheet_path for v in out.violations]
    assert paths == ["/", "/power/", "/power/"]


@pytest.mark.asyncio
async def test_flat_shape_wins_when_both_keys_present(tmp_path: Path) -> None:
    """If both ``violations`` and ``sheets`` are present, flat takes priority.

    Pins the order-of-checks contract documented in
    ``_parse_all_violations``: the flat key is preferred so a kicad-cli
    build that emits both shapes (migration period) isn't double-counted.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "flat_rule", "severity": "error", "description": "", "items": []},
            ],
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {"type": "nested_rule", "severity": "error", "description": "", "items": []},
                    ],
                }
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.total_count == 1
    assert out.violations[0].rule_id == "flat_rule"


@pytest.mark.asyncio
async def test_hierarchical_shape_tolerates_malformed_sheets(tmp_path: Path) -> None:
    """Non-dict entries or missing violations arrays are skipped, not fatal.

    Pins the defensive parsing: the tool should keep going past junk
    entries rather than returning ``parse_failed`` for a partial payload
    — the overall JSON is still valid, just one sheet is off.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "sheets": [
                "not_an_object",
                {"path": "/", "violations": "not_a_list"},
                {
                    "path": "/ok/",
                    "violations": [
                        {"type": "pin_not_connected", "severity": "error", "description": "", "items": []},
                    ],
                },
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "violations"
    assert out.total_count == 1
    assert out.violations[0].sheet_path == "/ok/"


# -- severity filter -------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_floor_warning_filters_info(tmp_path: Path) -> None:
    """Default floor='warning' drops 'info' entries but keeps warnings + errors."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "pin_not_connected", "severity": "error", "description": "", "items": []},
                {"type": "label_dangling", "severity": "warning", "description": "", "items": []},
                {"type": "similar_label", "severity": "info", "description": "", "items": []},
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert {v.severity for v in out.violations} == {"error", "warning"}
    assert out.total_count == 2


@pytest.mark.asyncio
async def test_severity_floor_error_filters_warnings(tmp_path: Path) -> None:
    """Floor='error' drops warnings — CI-gate default for 'block on errors only'."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "pin_not_connected", "severity": "error", "description": "", "items": []},
                {"type": "label_dangling", "severity": "warning", "description": "", "items": []},
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch, severity_floor="error"))

    assert out.total_count == 1
    assert out.violations[0].severity == "error"


@pytest.mark.asyncio
async def test_unknown_severity_is_kept(tmp_path: Path) -> None:
    """Unknown severities rank at -1 and pass every floor — forward-compat."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                {"type": "future_erc_rule", "severity": "critical", "description": "", "items": []},
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.total_count == 1
    assert out.violations[0].severity == "critical"


# -- argv plumbing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_default_shape(tmp_path: Path) -> None:
    """Default argv: `sch erc --format json --units mm -o <tmp> <sch>`."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))
    assert out.status == "ok"

    argv = _read_argv(stub)
    assert argv[:2] == ["sch", "erc"]
    assert "--format" in argv
    fmt_idx = argv.index("--format")
    assert argv[fmt_idx + 1] == "json"
    units_idx = argv.index("--units")
    assert argv[units_idx + 1] == "mm"
    assert "-o" in argv
    # Schematic path is the last positional — tool appends it after flags.
    assert argv[-1] == str(sch.resolve())


@pytest.mark.asyncio
async def test_argv_carries_units_mils(tmp_path: Path) -> None:
    """`units='mils'` is forwarded verbatim — ERC supports it, DRC doesn't."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mils",
            "kicad_version": "9.0.1",
            "violations": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch, units="mils"))
    assert out.status == "ok"
    assert out.coordinate_units == "mils"

    argv = _read_argv(stub)
    idx = argv.index("--units")
    assert argv[idx + 1] == "mils"


@pytest.mark.asyncio
async def test_argv_carries_units_inches(tmp_path: Path) -> None:
    """Input `units='in'` round-trips through argv."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {"coordinate_units": "in", "kicad_version": "9.0.1", "violations": []},
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch, units="in"))
    assert out.status == "ok"

    argv = _read_argv(stub)
    idx = argv.index("--units")
    assert argv[idx + 1] == "in"


# -- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_sch_not_found_when_path_missing(tmp_path: Path) -> None:
    """Missing file short-circuits BEFORE kicad-cli is invoked."""
    stub = _write_kicad_cli_stub(tmp_path)
    tool = _make_tool(stub)

    out = await tool.run(SchErcInput(sch_path=tmp_path / "nope.kicad_sch"))

    assert out.status == "sch_not_found"
    assert out.sch_path is None
    assert out.note is not None and "no such file" in out.note
    # Stub was never invoked — no argv sidecar.
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_sch_not_found_when_wrong_extension(tmp_path: Path) -> None:
    """Non-.kicad_sch input is rejected — guards against passing the PCB."""
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=pcb))

    assert out.status == "sch_not_found"
    assert out.sch_path == str(pcb.resolve())
    assert out.note is not None and ".kicad_sch" in out.note
    assert not (stub.parent / (stub.name + ".argv")).exists()


@pytest.mark.asyncio
async def test_status_cli_failed_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero kicad-cli exit → status='cli_failed', stderr excerpt in note."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_exit(stub, 2)

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "cli_failed"
    assert out.sch_path == str(sch.resolve())
    assert out.note is not None
    assert "exited 2" in out.note
    assert "synthetic kicad-cli failure" in out.note


@pytest.mark.asyncio
async def test_status_parse_failed_on_invalid_json(tmp_path: Path) -> None:
    """kicad-cli exit 0 but garbage on disk → status='parse_failed'."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "this is not json {{{")

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "parse_failed"
    assert out.sch_path == str(sch.resolve())
    assert out.note is not None and "not parseable" in out.note


@pytest.mark.asyncio
async def test_status_parse_failed_when_top_level_not_object(tmp_path: Path) -> None:
    """Top-level JSON is an array → defensive type check fires before parsing."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(stub, "[]")

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "parse_failed"
    assert out.note is not None and "top-level" in out.note


# -- schema-drift warnings (KiCAD 10 hardening) ---------------------------


@pytest.mark.asyncio
async def test_meta_warning_when_kicad_version_missing(tmp_path: Path) -> None:
    """Missing `kicad_version` in ERC JSON surfaces a meta.warnings entry."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            # kicad_version intentionally absent
            "violations": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "ok"
    assert out.kicad_version == ""
    assert any("kicad_version" in w for w in out.meta.warnings), out.meta.warnings


@pytest.mark.asyncio
async def test_meta_warning_when_coordinate_units_missing(tmp_path: Path) -> None:
    """Missing `coordinate_units` surfaces a drift warning."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            # coordinate_units intentionally absent
            "kicad_version": "9.0.1",
            "violations": [],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.coordinate_units == ""
    assert any("coordinate_units" in w for w in out.meta.warnings), out.meta.warnings


@pytest.mark.asyncio
async def test_meta_warning_when_rule_id_empty(tmp_path: Path) -> None:
    """A finding without a `type` field raises a drift warning."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    _stage_payload(
        stub,
        {
            "coordinate_units": "mm",
            "kicad_version": "9.0.1",
            "violations": [
                # No `type` field — simulate upstream rename.
                {"severity": "error", "description": "???", "items": []},
            ],
        },
    )

    tool = _make_tool(stub)
    out = await tool.run(SchErcInput(sch_path=sch))

    assert out.status == "violations"
    assert out.violations[0].rule_id == ""
    assert any("rule_id" in w for w in out.meta.warnings), out.meta.warnings


# -- dependency-injection shape -------------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_does_not_crash(tmp_path: Path) -> None:
    """Bare entry-point load: `run` must build its own backend lazily.

    The server normally injects via `set_cli_backend` but tools discovered
    via entry points before dispatcher wiring (or exercised from tests
    directly) must still produce a valid envelope. Here the default backend
    won't find any kicad-cli on the test tmpdir, so we assert only the
    *shape* — a status envelope and a `sch_path` mirror.
    """
    sch = _touch_sch(tmp_path)
    tool = SchErcTool()  # no set_cli_backend call
    out = await tool.run(SchErcInput(sch_path=sch))
    assert out.status in {"ok", "violations", "cli_failed", "parse_failed"}
    assert out.sch_path == str(sch.resolve())

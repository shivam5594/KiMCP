"""End-to-end: JSON-RPC tools/call → sch_export_bom against a stub.

Exercises the full M11 path: Server constructs ``CliBackend`` from config,
injects it into ``SchExportBomTool``, dispatcher gates on
``Backend.CLI`` availability, the client calls ``tools/call`` via the
in-memory transport, the tool shells out to our fake kicad-cli, the stub
writes a fake BOM payload to the requested ``-o`` path, and the envelope
comes back with status='ok' + sized file details.

Also pins:
* BOM-specific knobs (``preset``, ``exclude_dnp``) round-trip through
  JSON-RPC and land in the argv.
* ``dry_run=True`` round-trips and doesn't write.
* ``Backend.CLI`` unavailable → ``BACKEND_UNAVAILABLE`` before tool runs.

Stub is a local copy — same rationale as the sibling tests.
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.sch_export_bom import SchExportBomTool

pytestmark = [pytest.mark.e2e]


# -- stub (local copy — see module docstring for rationale) ----------------


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
                if a == "-o" and i + 1 < len(argv):
                    out_path = argv[i + 1]
                    break

            payload_file = here.parent / (here.name + ".payload")
            if out_path is not None and payload_file.exists():
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(
                    payload_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
            sys.exit(0)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _touch_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return sch


_SAMPLE_CSV = "Reference,Value,Footprint\nR1,10k,R_0603\n"


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_export_bom_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Full round-trip: format=tsv + preset='Fabrication' + exclude_dnp=True."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    (stub.parent / (stub.name + ".payload")).write_text(_SAMPLE_CSV, encoding="utf-8")

    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(stub),
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            }
        },
    )
    server = Server(config=config)
    server.register_tool(SchExportBomTool())
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_export_bom",
                    "arguments": {
                        "sch_path": str(sch),
                        "format": "tsv",
                        "preset": "Fabrication",
                        "exclude_dnp": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    assert len(transport.sent) == 1
    msg = transport.sent[0]
    assert "result" in msg, msg
    assert msg["result"]["isError"] is False
    result = msg["result"]["structuredContent"]
    assert result["status"] == "ok"
    # tsv → .tsv derived extension.
    expected = sch.with_suffix(".tsv")
    assert result["output_path"] == str(expected)
    assert expected.is_file()
    assert result["size_bytes"] > 0
    assert result["format"] == "tsv"
    # Optional knobs survived the JSON-RPC → pydantic → argv pipeline.
    assert "--preset" in result["cli_argv"]
    assert "Fabrication" in result["cli_argv"]
    assert "--exclude-dnp" in result["cli_argv"]
    # Envelope: dispatcher ran, so backend_used is stamped.
    assert result["meta"]["backend_used"] == Backend.CLI.value


# -- dry-run round-trips through JSON-RPC ---------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_export_bom_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    """`dry_run=True` returns the planned argv without invoking the CLI."""
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)

    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(stub),
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            }
        },
    )
    server = Server(config=config)
    server.register_tool(SchExportBomTool())
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_export_bom",
                    "arguments": {"sch_path": str(sch), "dry_run": True},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "result" in msg, msg
    assert msg["result"]["isError"] is False
    result = msg["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["cli_argv"] is not None
    assert "bom" in result["cli_argv"]
    # CLI was never invoked — no argv sidecar, no output file.
    assert not (stub.parent / (stub.name + ".argv")).exists()
    assert not sch.with_suffix(".csv").exists()


# -- dispatcher gate fires when CLI is unavailable -------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_export_bom_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when Backend.CLI isn't marked."""
    sch = _touch_sch(tmp_path)
    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nonexistent-cli"),
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            }
        },
    )
    server = Server(config=config)
    server.register_tool(SchExportBomTool())
    # Do NOT mark CLI available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_export_bom",
                    "arguments": {"sch_path": str(sch)},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "error" in msg, msg
    err = msg["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert err["data"]["preferred"] == [Backend.CLI.value]

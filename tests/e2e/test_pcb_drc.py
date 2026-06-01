"""End-to-end: JSON-RPC tools/call → pcb_drc against a kicad-cli stub.

Exercises the full M6 path: Server constructs ``CliBackend`` from config,
injects it into ``PcbDrcTool``, dispatcher gates on ``Backend.CLI``
availability, the client calls ``tools/call`` via the in-memory transport,
the tool shells out to our fake kicad-cli, parses the JSON report, and
the envelope comes back with status='violations' plus a populated
violations list.

Also pins the negative: when ``Backend.CLI`` isn't marked available the
dispatcher raises ``BACKEND_UNAVAILABLE`` *before* the tool runs — the
architectural mirror of the ipc_get_version e2e test, since pcb_drc is
another dispatcher-gated "real backend" tool (``preferred_backends=(CLI,)``).

The shell stub is a near-duplicate of the one in
``tests/unit/test_tool_pcb_drc.py``. Keeping a local copy instead of
hoisting to ``conftest.py`` matches the same rationale already documented
in ``test_tool_ipc_get_version.py`` — the stub is cheap, tests read
top-to-bottom, and a premature fixture abstraction hides what the test
actually drives.
"""

from __future__ import annotations

import json
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
from kimcp.tools.builtin.pcb_drc import PcbDrcTool

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
                Path(out_path).write_text(
                    payload_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
            sys.exit(0)
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _touch_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8")
    return pcb


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_pcb_drc_ok(tmp_path: Path, memory_transport_factory) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    (stub.parent / (stub.name + ".payload")).write_text(
        json.dumps(
            {
                "coordinate_units": "mm",
                "kicad_version": "9.0.1",
                "violations": [
                    {
                        "type": "clearance",
                        "severity": "error",
                        "description": "Clearance violation (track to pad)",
                        "items": [{"description": "track on F.Cu", "uuid": "abc-123"}],
                    }
                ],
                "unconnected_items": [],
                "schematic_parity": [],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(stub),
                # IPC path is irrelevant to this tool but must be a string.
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            }
        },
    )
    server = Server(config=config)
    server.register_tool(PcbDrcTool())
    # Simulate a completed probe: dispatcher gates on Backend.CLI.
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_drc",
                    "arguments": {"pcb_path": str(pcb)},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    assert len(transport.sent) == 1
    msg = transport.sent[0]
    assert "result" in msg, msg
    # MCP 2025-06-18: tool envelope lives under structuredContent.
    assert msg["result"]["isError"] is False
    result = msg["result"]["structuredContent"]
    assert result["status"] == "violations"
    assert result["total_count"] == 1
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule_id"] == "clearance"
    assert v["severity"] == "error"
    assert v["items"][0]["uuid"] == "abc-123"
    assert result["kicad_version"] == "9.0.1"
    assert result["pcb_path"] == str(pcb.resolve())
    # Envelope: dispatcher ran, so backend_used is stamped.
    assert result["meta"]["backend_used"] == Backend.CLI.value


# -- dispatcher gate fires when CLI is unavailable -------------------------


@pytest.mark.asyncio
async def test_tools_call_pcb_drc_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when Backend.CLI isn't marked.

    Pins the architectural choice: pcb_drc is backend-gated — required_backends
    includes CLI, so availability must be marked or the dispatcher rejects the
    call before the tool runs. In-tool cli_failed envelopes cover the
    mid-session race (CLI up at probe, gone by call), not "server started
    without kicad-cli installed".
    """
    pcb = _touch_pcb(tmp_path)
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
    server.register_tool(PcbDrcTool())
    # Do NOT mark CLI available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_drc",
                    "arguments": {"pcb_path": str(pcb)},
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

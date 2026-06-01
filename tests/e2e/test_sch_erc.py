"""End-to-end: JSON-RPC tools/call → sch_erc against a kicad-cli stub.

Exercises the full M9 path: Server constructs ``CliBackend`` from config,
injects it into ``SchErcTool``, dispatcher gates on ``Backend.CLI``
availability, the client calls ``tools/call`` via the in-memory transport,
the tool shells out to our fake kicad-cli, parses the JSON report, and
the envelope comes back with status='violations' plus a populated
violations list with ``sheet_path`` preserved from the hierarchical shape.

Also pins the negative: when ``Backend.CLI`` isn't marked available the
dispatcher raises ``BACKEND_UNAVAILABLE`` *before* the tool runs — the
architectural mirror of the ``pcb_drc`` / ``pcb_export_gerbers`` /
``pcb_export_drill`` e2e tests, since ``sch_erc`` is another
dispatcher-gated "real backend" tool (``preferred_backends=(CLI,)``).

The shell stub is a near-duplicate of the one in
``tests/unit/test_tool_sch_erc.py``. Keeping a local copy instead of
hoisting to ``conftest.py`` matches the rationale documented across the
sibling tests: the stub is cheap, tests read top-to-bottom, and a
premature fixture abstraction hides what the test actually drives.
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
from kimcp.tools.builtin.sch_erc import SchErcTool

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


def _touch_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n", encoding="utf-8")
    return sch


# -- happy path: hierarchical shape round-trips through JSON-RPC -----------


@pytest.mark.asyncio
async def test_tools_call_sch_erc_ok(tmp_path: Path, memory_transport_factory) -> None:
    """Hierarchical ERC JSON → flattened violations list with sheet_path preserved.

    Pins both the end-to-end wiring (config → server → dispatcher → tool →
    stub → parse → envelope) and the ERC-specific sheet-flattening contract
    through the JSON-RPC layer.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    sch = _touch_sch(tmp_path)
    (stub.parent / (stub.name + ".payload")).write_text(
        json.dumps(
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
                                "description": "Input pin not connected",
                                "items": [{"description": "pin 3 of U1", "uuid": "abc-123"}],
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
                            }
                        ],
                    },
                ],
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
    server.register_tool(SchErcTool())
    # Simulate a completed probe: dispatcher gates on Backend.CLI.
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_erc",
                    "arguments": {"sch_path": str(sch)},
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
    assert result["total_count"] == 2
    rule_ids = [v["rule_id"] for v in result["violations"]]
    assert rule_ids == ["pin_not_connected", "power_pin_no_driver"]
    paths = [v["sheet_path"] for v in result["violations"]]
    assert paths == ["/", "/power/"]
    assert result["violations"][0]["items"][0]["uuid"] == "abc-123"
    assert result["kicad_version"] == "9.0.1"
    assert result["sch_path"] == str(sch.resolve())
    # Envelope: dispatcher ran, so backend_used is stamped.
    assert result["meta"]["backend_used"] == Backend.CLI.value


# -- dispatcher gate fires when CLI is unavailable -------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_erc_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when Backend.CLI isn't marked.

    Pins the architectural choice: sch_erc is backend-gated —
    required_backends includes CLI, so availability must be marked or the
    dispatcher rejects the call before the tool runs. The in-tool
    `cli_failed` envelope covers the mid-session race (CLI up at probe,
    gone by call), not "server started without kicad-cli installed".
    """
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
    server.register_tool(SchErcTool())
    # Do NOT mark CLI available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_erc",
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

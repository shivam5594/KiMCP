"""End-to-end: JSON-RPC tools/call → pcb_export_gerbers against a kicad-cli stub.

Exercises the full M7 path: Server constructs ``CliBackend`` from config,
injects it into ``PcbExportGerbersTool``, dispatcher gates on
``Backend.CLI`` availability, the client calls ``tools/call`` via the
in-memory transport, the tool shells out to our fake kicad-cli, the
stub writes fake gerber files into the requested ``--output`` dir, and
the envelope comes back with status='ok' plus a populated
``generated_files`` list.

Also pins the negative: when ``Backend.CLI`` isn't marked available the
dispatcher raises ``BACKEND_UNAVAILABLE`` *before* the tool runs — same
architectural mirror as the ``pcb_drc`` and ``ipc_get_version`` e2e
tests, since ``pcb_export_gerbers`` declares ``preferred_backends=(CLI,)``.

The shell stub is a near-duplicate of the one in
``tests/unit/test_tool_pcb_export_gerbers.py`` and the sibling
``tests/e2e/test_pcb_drc.py``. Keeping a local copy instead of hoisting
to ``conftest.py`` matches the rationale already documented in
``test_tool_ipc_get_version.py`` — the stub is cheap, the test reads
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
from kimcp.tools.builtin.pcb_export_gerbers import PcbExportGerbersTool

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
async def test_tools_call_pcb_export_gerbers_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out_dir = tmp_path / "gerbers_out"
    (stub.parent / (stub.name + ".files")).write_text(
        json.dumps(
            [
                {"name": "board-F_Cu.gbr", "contents": "G04 F.Cu*\n"},
                {"name": "board-B_Cu.gbr", "contents": "G04 B.Cu*\n"},
                {"name": "board-Edge_Cuts.gbr", "contents": "G04 Edge.Cuts*\n"},
                {"name": "board-job.gbrjob", "contents": "{}\n"},
            ]
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
    server.register_tool(PcbExportGerbersTool())
    # Simulate a completed probe: dispatcher gates on Backend.CLI.
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_gerbers",
                    "arguments": {
                        "pcb_path": str(pcb),
                        "output_dir": str(out_dir),
                        "layers": ["F.Cu", "B.Cu", "Edge.Cuts"],
                    },
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
    assert result["status"] == "ok"
    assert result["pcb_path"] == str(pcb.resolve())
    assert result["output_dir"] == str(out_dir.resolve())
    assert result["total_files"] == 4
    assert result["total_bytes"] > 0
    # Files come back sorted by filename — alphabetic order on the basenames.
    names = [Path(f["path"]).name for f in result["generated_files"]]
    assert names == sorted(names)
    # Layer hint extraction round-trips through JSON serialization.
    by_name = {Path(f["path"]).name: f for f in result["generated_files"]}
    assert by_name["board-F_Cu.gbr"]["layer_hint"] == "F.Cu"
    assert by_name["board-Edge_Cuts.gbr"]["layer_hint"] == "Edge.Cuts"
    assert by_name["board-job.gbrjob"]["layer_hint"] is None
    # Envelope: dispatcher ran, so backend_used is stamped.
    assert result["meta"]["backend_used"] == Backend.CLI.value


# -- dry-run round-trips through the JSON-RPC envelope --------------------


@pytest.mark.asyncio
async def test_tools_call_pcb_export_gerbers_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    """`dry_run=True` returns the planned argv without invoking the CLI.

    Pins the safety contract end-to-end: the MCP host can preview a
    mutating call's argv via JSON-RPC before approving the live run.
    Since dry-run never invokes kicad-cli, the test doesn't even need
    a working stub binary on disk — but we still wire one up so the
    dispatcher's CLI availability gate passes.
    """
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out_dir = tmp_path / "gerbers_dry_e2e"

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
    server.register_tool(PcbExportGerbersTool())
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_gerbers",
                    "arguments": {
                        "pcb_path": str(pcb),
                        "output_dir": str(out_dir),
                        "dry_run": True,
                    },
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
    assert "gerbers" in result["cli_argv"]
    assert result["generated_files"] == []
    # CLI was never invoked — no .argv sidecar from the stub.
    assert not (stub.parent / (stub.name + ".argv")).exists()
    # And the output dir must NOT have been created.
    assert not out_dir.exists()


# -- dispatcher gate fires when CLI is unavailable -------------------------


@pytest.mark.asyncio
async def test_tools_call_pcb_export_gerbers_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when Backend.CLI isn't marked.

    Pins the architectural choice: pcb_export_gerbers is backend-gated —
    required_backends includes CLI, so availability must be marked or the
    dispatcher rejects the call before the tool runs. The in-tool
    `cli_failed` envelope covers the mid-session race (CLI up at probe,
    gone by call), not "server started without kicad-cli installed".
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
    server.register_tool(PcbExportGerbersTool())
    # Do NOT mark CLI available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_gerbers",
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

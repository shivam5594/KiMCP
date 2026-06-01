"""E2E: JSON-RPC tools/call → pcb_export_pos."""

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
from kimcp.tools.builtin.pcb_export_pos import PcbExportPosTool

pytestmark = [pytest.mark.e2e]


def _write_kicad_cli_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "kicad-cli-stub"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            from pathlib import Path

            argv = sys.argv[1:]
            if argv[:1] == ["version"]:
                sys.stdout.write("Application: kicad-cli\\n")
                sys.stdout.write("Version: 9.0.1, release build\\n")
                sys.exit(0)

            out_path = None
            for i, a in enumerate(argv):
                if a == "--output" and i + 1 < len(argv):
                    out_path = argv[i + 1]
                    break
            if out_path is not None:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(
                    "Ref,Val,Package,PosX,PosY,Rot,Side\\nU1,MCU,QFN48,0,0,0,top\\n",
                    encoding="utf-8",
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


def _cfg(tmp_path: Path, cli_exe: str):
    return load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": cli_exe,
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            }
        },
    )


@pytest.mark.asyncio
async def test_tools_call_pos_ok(tmp_path: Path, memory_transport_factory) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)
    out_path = tmp_path / "fab" / "pos.csv"

    server = Server(config=_cfg(tmp_path, str(stub)))
    server.register_tool(PcbExportPosTool())
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_pos",
                    "arguments": {
                        "pcb_path": str(pcb),
                        "output_path": str(out_path),
                        "exclude_dnp": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["output_path"] == str(out_path.resolve())
    assert result["size_bytes"] > 0
    assert result["meta"]["backend_used"] == Backend.CLI.value


@pytest.mark.asyncio
async def test_tools_call_pos_dry_run(tmp_path: Path, memory_transport_factory) -> None:
    stub = _write_kicad_cli_stub(tmp_path)
    pcb = _touch_pcb(tmp_path)

    server = Server(config=_cfg(tmp_path, str(stub)))
    server.register_tool(PcbExportPosTool())
    server.availability.mark(Backend.CLI, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_pos",
                    "arguments": {"pcb_path": str(pcb), "dry_run": True},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["cli_argv"] is not None
    assert "pos" in result["cli_argv"]


@pytest.mark.asyncio
async def test_tools_call_pos_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    pcb = _touch_pcb(tmp_path)
    server = Server(config=_cfg(tmp_path, str(tmp_path / "nonexistent-cli")))
    server.register_tool(PcbExportPosTool())

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "pcb_export_pos",
                    "arguments": {"pcb_path": str(pcb)},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE

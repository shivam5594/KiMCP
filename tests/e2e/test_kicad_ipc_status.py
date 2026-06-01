"""End-to-end: JSON-RPC tools/call → kicad_ipc_status with a live socket.

Exercises the whole M3 path: Server constructs IpcBackend from config,
injects into KiCadIpcStatusTool, client calls tools/call via the
in-memory transport, envelope comes back with status=reachable when an
ephemeral Unix server is listening. The closest approximation to
"kimcp-cli tool run kicad_ipc_status" without shelling out.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kimcp.config import load_config
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.kicad_ipc_status import KiCadIpcStatusTool

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="Unix-socket-based e2e; Windows pipe path is platform-gated.",
    ),
]


async def _start_accepting_server(path: Path) -> asyncio.AbstractServer:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    return await asyncio.start_unix_server(_handle, path=str(path))


@pytest.mark.asyncio
async def test_tools_call_kicad_ipc_status_reachable(
    tmp_path: Path, socket_tmp_path: Path, memory_transport_factory
) -> None:
    # Socket bind needs a short path (AF_UNIX ~104-char limit on macOS);
    # unrelated config-file paths can stay on regular `tmp_path`.
    sock_path = socket_tmp_path / "e2e.sock"
    peer = await _start_accepting_server(sock_path)
    try:
        # Pin ipc_socket to our fake peer; neuter cli_exe so the test is
        # host-independent (we don't care about cli in this test).
        config = load_config(
            user_global=tmp_path / "__nope_user.toml",
            project_local=tmp_path / "__nope_project.toml",
            session_overrides={
                "kicad": {
                    "cli_exe": str(tmp_path / "nonexistent-cli"),
                    "ipc_socket": str(sock_path),
                }
            },
        )
        server = Server(config=config)
        server.register_tool(KiCadIpcStatusTool())

        transport = memory_transport_factory(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "kicad_ipc_status", "arguments": {}},
                },
            ]
        )
        await dispatch_loop(transport, server.handler)

        assert len(transport.sent) == 1
        resp = transport.sent[0]
        assert "result" in resp, resp
        # MCP 2025-06-18: tool envelope lives under structuredContent.
        assert resp["result"]["isError"] is False
        result = resp["result"]["structuredContent"]
        assert result["status"] == "reachable"
        assert result["socket_path"] == str(sock_path.resolve())
        assert result["note"] is None
        assert result["probe_timeout_sec"] > 0
        # Envelope is populated.
        assert "meta" in result
        assert result["meta"]["live_sync"] is True
    finally:
        peer.close()
        await peer.wait_closed()


@pytest.mark.asyncio
async def test_tools_call_kicad_ipc_status_not_found(
    tmp_path: Path, memory_transport_factory
) -> None:
    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nonexistent-cli"),
                "ipc_socket": str(tmp_path / "definitely-missing.sock"),
            }
        },
    )
    server = Server(config=config)
    server.register_tool(KiCadIpcStatusTool())

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "kicad_ipc_status", "arguments": {}},
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    resp = transport.sent[0]
    assert "result" in resp, resp
    # MCP 2025-06-18: tool envelope lives under structuredContent.
    assert resp["result"]["isError"] is False
    result = resp["result"]["structuredContent"]
    assert result["status"] == "not_found"
    assert result["socket_path"] is None
    assert result["note"]  # user-actionable string

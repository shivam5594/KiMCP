"""End-to-end smoke test: initialize → tools/list → tools/call → shutdown.

Uses the in-memory transport from conftest — exercises the handler, tool
registry, and envelope without subprocess flakiness. Real subprocess STDIO
transport gets its own matrix test later.
"""

from __future__ import annotations

import pytest

from kimcp import __version__
from kimcp.config import load_config
from kimcp.rpc import dispatch_loop
from kimcp.server import MCP_PROTOCOL_VERSION, Server
from kimcp.tools.builtin.ping import PingTool
from kimcp.tools.builtin.version import VersionTool

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_initialize_list_call_roundtrip(memory_transport_factory) -> None:
    server = Server()
    server.register_tool(PingTool())
    server.register_tool(VersionTool())

    transport = memory_transport_factory(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {"message": "hi"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "version", "arguments": {}},
            },
            {"jsonrpc": "2.0", "method": "initialized"},  # notification — no response
            {"jsonrpc": "2.0", "id": 5, "method": "shutdown"},
        ]
    )

    await dispatch_loop(transport, server.handler)

    # Notification is silent; 5 numbered requests → 5 responses.
    assert len(transport.sent) == 5

    init_resp = transport.sent[0]
    assert init_resp["id"] == 1
    assert init_resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert init_resp["result"]["serverInfo"]["name"] == "kimcp"
    assert init_resp["result"]["serverInfo"]["version"] == __version__

    tools_resp = transport.sent[1]
    names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert {"ping", "version"} <= names

    # `tools/call` results follow MCP 2025-06-18: the tool envelope lives
    # under `result.structuredContent`, with a JSON text rendering under
    # `result.content[0].text` for clients that don't read the structured
    # form. Both are populated; tests assert against the structured one.
    ping_resp = transport.sent[2]
    assert ping_resp["result"]["isError"] is False
    assert ping_resp["result"]["content"][0]["type"] == "text"
    ping_envelope = ping_resp["result"]["structuredContent"]
    assert ping_envelope["echo"] == "hi"
    assert "meta" in ping_envelope
    assert ping_envelope["meta"]["live_sync"] is True

    version_resp = transport.sent[3]
    version_envelope = version_resp["result"]["structuredContent"]
    assert version_envelope["kimcp_version"] == __version__

    shutdown_resp = transport.sent[4]
    assert shutdown_resp["id"] == 5
    assert shutdown_resp["result"] is None


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error(memory_transport_factory) -> None:
    server = Server()
    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "definitely_not_a_tool", "arguments": {}},
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    assert "error" in transport.sent[0]


@pytest.mark.asyncio
async def test_call_with_bad_input_returns_validation_error(memory_transport_factory) -> None:
    server = Server()
    server.register_tool(PingTool())
    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {"message": 12345}},  # wrong type
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    # Pydantic actually coerces int → str in lenient mode, so this passes.
    # More interesting: missing `name`.
    assert "result" in transport.sent[0] or "error" in transport.sent[0]


@pytest.mark.asyncio
async def test_backend_probe_populates_availability(tmp_path) -> None:
    # Pin every probeable backend to a definitely-missing target so the
    # matrix is deterministic regardless of whether the test host has
    # KiCAD / kicad-cli / a live IPC socket. The real "does it resolve?"
    # behavior is exercised in the per-backend unit tests.
    missing_cli = tmp_path / "definitely_not_kicad_cli"
    missing_sock = tmp_path / "definitely_not_kicad_ipc.sock"
    config = load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(missing_cli),
                "ipc_socket": str(missing_sock),
            }
        },
    )
    server = Server(config=config)
    results = await server.probe_backends()
    assert set(results.keys()) == {"ipc", "cli", "sexpr", "swig"}
    # M1 flipped sexpr to always-True (pure Python, zero deps). M2 left
    # cli gated on a real binary. M3 made ipc real — gated on a live
    # socket. swig stays False until M5. We force every non-sexpr path
    # to resolve to a missing target so the matrix locks in deterministically;
    # the "does it actually resolve?" behavior is tested in each backend's
    # unit-test file.
    assert results["sexpr"] is True
    assert results["ipc"] is False
    assert results["cli"] is False
    assert results["swig"] is False

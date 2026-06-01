"""End-to-end: JSON-RPC tools/call → ipc_get_version against a live stub.

Exercises the whole M5 path: Server constructs IpcBackend from config,
injects into IpcGetVersionTool, dispatcher gates on Backend.IPC
availability, client calls tools/call via the in-memory transport, the
tool dials the stub over pynng, round-trips a GetVersion RPC, and the
envelope comes back with status='ok' + a populated version string.

Also pins the negative: when availability isn't marked, the dispatcher
raises BACKEND_UNAVAILABLE *before* the tool runs — a deliberate
contract for "real" IPC-backed tools and the reason the sibling
diagnostic ``kicad_ipc_status`` uses ``preferred_backends=()`` instead
(the two diverge for a reason; see their module docstrings).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.ipc_get_version import IpcGetVersionTool

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="pynng+kipy Unix-socket e2e; Windows pipe path is separate.",
    ),
]

# Skip cleanly on a minimal install (no `[ipc]` extra).
pynng = pytest.importorskip("pynng")
kipy_envelope = pytest.importorskip("kipy.proto.common.envelope_pb2")
kipy_base_commands = pytest.importorskip("kipy.proto.common.commands.base_commands_pb2")
kipy_base_types = pytest.importorskip("kipy.proto.common.types.base_types_pb2")


# -- stub copy (kept local — see rationale in the unit test module) --------


class StubKicad:
    def __init__(self, url: str) -> None:
        self.url = url
        self.received: list[Any] = []
        self.responder: Any = None
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = pynng.Rep0()
        self._server.listen(self.url)
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _serve(self) -> None:
        assert self._server is not None
        while True:
            try:
                msg = await self._server.arecv_msg()
            except pynng.exceptions.Closed:
                return
            except pynng.exceptions.NNGException:
                return
            req = kipy_envelope.ApiRequest()
            req.ParseFromString(msg.bytes)
            self.received.append(req)
            assert self.responder is not None
            resp = self.responder(req)
            try:
                await self._server.asend(resp.SerializeToString())
            except pynng.exceptions.Closed:
                return
            except pynng.exceptions.NNGException:
                return


def _ok_response(inner_message: Any, *, token: str = "") -> Any:
    resp = kipy_envelope.ApiResponse()
    resp.status.status = kipy_envelope.ApiStatusCode.AS_OK
    resp.message.Pack(inner_message)
    resp.header.kicad_token = token
    return resp


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_ipc_get_version_ok(
    tmp_path: Path, socket_tmp_path: Path, memory_transport_factory
) -> None:
    sock_path = socket_tmp_path / "e2e.sock"
    stub = StubKicad(url=f"ipc://{sock_path}")

    fake_version = kipy_base_types.KiCadVersion(
        major=10, minor=0, patch=1, full_version="10.0.1-stub"
    )
    get_version_resp = kipy_base_commands.GetVersionResponse()
    get_version_resp.version.CopyFrom(fake_version)
    stub.responder = lambda _req: _ok_response(get_version_resp, token="session")
    await stub.start()

    try:
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
        server.register_tool(IpcGetVersionTool())
        # Simulate a completed startup probe: the dispatcher gates on
        # availability, and this tool declares `preferred_backends=(IPC,)`.
        # Marking manually keeps the test fast and isolated from the
        # real CLI / SexprBackend probes that `probe_backends()` would
        # also fire.
        server.availability.mark(Backend.IPC, True)

        transport = memory_transport_factory(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ipc_get_version", "arguments": {}},
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
        assert result["version"] == "10.0.1"
        assert result["version_raw"] == "10.0.1-stub"
        assert result["major"] == 10
        assert result["minor"] == 0
        assert result["patch"] == 1
        assert result["note"] is None
        assert result["socket_path"] == str(sock_path.resolve())
        # Envelope is populated — dispatcher ran, so backend_used is stamped.
        assert result["meta"]["backend_used"] == Backend.IPC.value
        assert result["meta"]["live_sync"] is True
    finally:
        await stub.stop()
        # Close the long-lived pynng socket the server-owned backend opened,
        # so fd accounting stays clean across tests.
        await server._ipc_backend.aclose()


# -- dispatcher gate fires when IPC is unavailable -------------------------


@pytest.mark.asyncio
async def test_tools_call_ipc_get_version_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when IPC isn't marked available.

    Pins the architectural choice: ipc_get_version is backend-gated on
    purpose. The graceful in-tool status envelopes are for *mid-session*
    races (IPC up at probe time, dies between probe and call), not for
    "server came up without IPC". That case produces a JSON-RPC error so
    the client knows to run ``kicad_ipc_status`` for diagnostics.
    """
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
    server.register_tool(IpcGetVersionTool())
    # Do NOT mark IPC available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ipc_get_version", "arguments": {}},
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "error" in msg, msg
    err = msg["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    # Data payload carries the preferred / available diagnostic.
    assert err["data"]["preferred"] == [Backend.IPC.value]
